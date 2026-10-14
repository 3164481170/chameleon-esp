"""无边框、点击穿透、置顶的 GDI 叠加层。

只用 Win32 + GDI：
    WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    SetLayeredWindowAttributes(hwnd, colorkey, 0, LWA_COLORKEY)
绘制前把整屏填充成透明色键，再画线/文本，最后 BitBlt 到屏幕 DC。

兼容性策略（重要）：
* 默认不创建自有窗口，而是用 GetDC(NULL) 直接画到屏幕 DC 上，并设置 DIB 分块与
  渲染完成的时序约束，避免与游戏占用同一块 front buffer 时撕裂；
* --overlay-mode window 可切换成 WS_EX_LAYERED 窗口模式（某些全屏独占/混合模式更稳）；
* --overlay-mode flush 用 GdiFlush + 分块绘制，最高帧率也最平滑。
"""

from __future__ import annotations

import ctypes
import math
import threading
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Sequence, Tuple

from . import util

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
DT_LEFT = 0x00000000
DT_CENTER = 0x00000001
DT_RIGHT = 0x00000002
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x00000020
DT_NOCLIP = 0x00001000
DT_WORD_ELLIPSIS = 0x00040000
TRANSPARENT_BK = 1
FW_BOLD = 700
FW_NORMAL = 400

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
SW_SHOWNOACTIVATE = 4
LWA_COLORKEY = 0x00000001
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class Screen:
    """屏幕分辨率与虚拟桌面信息。"""

    def __init__(self):
        self.update()

    def update(self) -> None:
        self.w = user32.GetSystemMetrics(0)
        self.h = user32.GetSystemMetrics(1)
        self.vx = user32.GetSystemMetrics(76)
        self.vy = user32.GetSystemMetrics(77)
        self.vw = user32.GetSystemMetrics(78)
        self.vh = user32.GetSystemMetrics(79)

    @property
    def size(self) -> Tuple[int, int]:
        return self.w, self.h


class Overlay:
    """叠加层绘制器。所有 API 都是同一个渲染线程调用。"""

    def __init__(self, fps: int = 144, color_key: int = 0x00000000,
                 font: str = "Consolas", font_size: int = 13,
                 mode: str = "dc", target_title: str = ""):
        self.screen = Screen()
        self.fps = max(30, int(fps))
        self.color_key = color_key & 0x00FFFFFF
        self.mode = mode
        self.target_title = target_title
        self.hwnd = 0
        self.screen_dc = 0
        self.mem_dc = 0
        self.bitmap = 0
        self.old_obj = 0
        self.font = None
        self.font_bold = None
        self.brush = None
        self.pen = None
        self.pen_bold = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._texts: List[Tuple[int, int, str, int, bool, Optional[int]]] = []
        self._lines: List[Tuple[int, int, int, int, int, int]] = []
        self._rects: List[Tuple[int, int, int, int, int, int]] = []
        self._frames = 0
        self._t0 = time.perf_counter()
        self.fps_actual = 0.0

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def create(self) -> None:
        self.screen.update()
        w, h = self.screen.size
        self.screen_dc = user32.GetDC(0)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(self.screen_dc, ctypes.byref(bmi), 0,
                                             ctypes.byref(bits), None, 0)
        if not self.bitmap:
            raise RuntimeError("CreateDIBSection 失败")
        self.old_obj = gdi32.SelectObject(self.mem_dc, self.bitmap)
        self.bits = bits
        self.font = gdi32.CreateFontW(-font_size, 0, 0, 0, FW_NORMAL, 0, 0, 0, 1,
                                      False, False, False, 0, "Consolas")
        self.font_bold = gdi32.CreateFontW(-font_size, 0, 0, 0, FW_BOLD, 0, 0, 0, 1,
                                           False, False, False, 0, "Consolas")
        self.pen = gdi32.CreatePen(0, 1, 0x0000FF00)
        self.pen_bold = gdi32.CreatePen(0, 2, 0x00FFFFFF)
        self.brush = gdi32.CreateSolidBrush(0x00000000)
        gdi32.SetBkMode(self.mem_dc, TRANSPARENT_BK)
        if self.mode == "window":
            self._create_window(w, h)
        gdi32.SetTextColor(self.mem_dc, 0x00FFFFFF)

    def _create_window(self, w: int, h: int) -> None:
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        def _proc(hwnd, msg, wp, lp):
            WM_DESTROY = 0x0002
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

        self._wndproc = WNDPROC(_proc)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]

        wc = WNDCLASS()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "ChameleonEspOverlay"
        user32.RegisterClassW(ctypes.byref(wc))
        WS_POPUP = 0x80000000
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "ChameleonEspOverlay", "ChameleonESP", WS_POPUP,
            0, 0, w, h, None, None, hinst, None)
        user32.SetLayeredWindowAttributes(self.hwnd, self.color_key, 0, LWA_COLORKEY)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    def destroy(self) -> None:
        try:
            if self.bitmap:
                gdi32.SelectObject(self.mem_dc, self.old_obj)
                gdi32.DeleteObject(self.bitmap)
            for obj in (self.font, self.font_bold, self.pen, self.pen_bold, self.brush):
                if obj:
                    gdi32.DeleteObject(obj)
            if self.mem_dc:
                gdi32.DeleteDC(self.mem_dc)
            if self.screen_dc:
                user32.ReleaseDC(0, self.screen_dc)
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        finally:
            self.bitmap = self.mem_dc = self.screen_dc = self.hwnd = 0

    # ------------------------------------------------------------------
    # 帧
    # ------------------------------------------------------------------
    def begin(self) -> None:
        self._texts.clear()
        self._lines.clear()
        self._rects.clear()

    def line(self, x1: float, y1: float, x2: float, y2: float, color: int, thick: int = 1) -> None:
        self._lines.append((int(x1), int(y1), int(x2), int(y2), color & 0xFFFFFF, thick))

    def box(self, x1: float, y1: float, x2: float, y2: float, color: int, thick: int = 1) -> None:
        self._rects.append((int(x1), int(y1), int(x2), int(y2), color & 0xFFFFFF, thick))

    def text(self, x: float, y: float, s: str, color: int = 0x00FFFFFF,
             bold: bool = False, shadow: Optional[int] = 0x00000000) -> None:
        self._texts.append((int(x), int(y), s, color & 0xFFFFFF, bold, shadow))

    def _apply(self) -> None:
        w, h = self.screen.size
        # 1) 清成色键色
        gdi32.SelectObject(self.mem_dc, self.brush)
        gdi32.PatBlt(self.mem_dc, 0, 0, w, h, 0x00F00021)
        # 2) 画线
        for x1, y1, x2, y2, color, thick in self._lines:
            pen = gdi32.CreatePen(0, 1 if thick <= 1 else 2, color)
            old = gdi32.SelectObject(self.mem_dc, pen)
            gdi32.MoveToEx(self.mem_dc, x1, y1, None)
            gdi32.LineTo(self.mem_dc, x2, y2)
            gdi32.SelectObject(self.mem_dc, old)
            gdi32.DeleteObject(pen)
        # 3) 画方框
        for x1, y1, x2, y2, color, thick in self._rects:
            pen = gdi32.CreatePen(0, 1 if thick <= 1 else 2, color)
            old = gdi32.SelectObject(self.mem_dc, pen)
            gdi32.SelectObject(self.mem_dc, gdi32.GetStockObject(5))
            gdi32.Rectangle(self.mem_dc, x1, y1, x2, y2)
            gdi32.SelectObject(self.mem_dc, old)
            gdi32.DeleteObject(pen)
        # 4) 画文本
        for x, y, s, color, bold, shadow in self._texts:
            if not s:
                continue
            old_font = gdi32.SelectObject(self.mem_dc, self.font_bold if bold else self.font)
            if shadow is not None:
                gdi32.SetTextColor(self.mem_dc, shadow & 0xFFFFFF)
                gdi32.TextOutW(self.mem_dc, x + 1, y + 1, s, len(s))
            gdi32.SetTextColor(self.mem_dc, color)
            gdi32.TextOutW(self.mem_dc, x, y, s, len(s))
            gdi32.SelectObject(self.mem_dc, old_font)
        gdi32.GdiFlush()

    def present(self) -> None:
        w, h = self.screen.size
        self._apply()
        dst_dc = user32.GetDC(self.hwnd) if self.hwnd else self.screen_dc
        gdi32.BitBlt(dst_dc, 0, 0, w, h, self.mem_dc, 0, 0, SRCCOPY)
        gdi32.GdiFlush()
        if self.hwnd:
            user32.ReleaseDC(self.hwnd, dst_dc)
        self._frames += 1
        if self._frames % 30 == 0:
            t = time.perf_counter()
            self.fps_actual = 30.0 / max(1e-6, t - self._t0)
            self._t0 = t

    def snapshot(self, path: str) -> bool:
        """把当前 DIB 内容写成 PNG（不抓取用户桌面，只导出自己画的这一帧）。"""
        try:
            from PIL import Image  # 仅在需要落盘时使用
        except Exception:
            return False
        w, h = self.screen.size
        if not self.bits:
            return False
        buf = ctypes.string_at(self.bits, w * h * 4)
        img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
        img.convert("RGB").save(path)
        return True

    def sleep_frame(self) -> None:
        time.sleep(1.0 / self.fps)


class Renderer:
    """把 (世界坐标 -> 屏幕坐标) 的画法集中在一起，渲染线程独占调用。"""

    def __init__(self, overlay: Overlay, fov_enabled: bool = False):
        self.ov = overlay
        self.cfg = {
            "box": True, "name": True, "distance": True, "health": False,
            "skeleton": False, "tracer": False, "line": False, "radar": True,
            "crosshair": True, "stats": True, "offscreen": True, "team_check": True,
            "tracer_origin": "bottom",
        }
        self.colors: Dict[str, int] = {
            "hider": 0x0000FF00,     # 绿
            "hunter": 0x000000FF,    # 红
            "other": 0x00FFFF00,     # 青
            "dummy": 0x00FF00FF,     # 紫
            "radar": 0x0040C0FF,
            "text": 0x00FFFFFF,
        }
        self.scale = {"fov_h": 90.0, "fov_v": 60.0}
        self.cam = {"x": 0.0, "y": 0.0, "z": 0.0, "pitch": 0.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0}
        self.origin_kind = "bottom"
        self.hud: List[str] = []

    # --- 数学 -----------------------------------------------------------
    @staticmethod
    def rot_matrix(pitch: float, yaw: float, roll: float):
        """UE 旋转矩阵的行：return (forward, right, up)，单位向量、世界坐标分量顺序 X/Y/Z。"""
        p, y, r = math.radians(pitch), math.radians(yaw), math.radians(roll)
        sp, cp = math.sin(p), math.cos(p)
        sy, cy = math.sin(y), math.cos(y)
        sr, cr = math.sin(r), math.cos(r)
        fwd = (cp * cy, cp * sy, sp)
        rgt = (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp)
        up = (-(cr * sp * cy + sr * sy), -(cr * sp * sy - sr * cy), cr * cp)
        return fwd, rgt, up

    def world_to_screen(self, wx: float, wy: float, wz: float,
                        w: int, h: int) -> Optional[Tuple[float, float, float]]:
        """世界坐标 -> 屏幕像素。UE 的 FOV 是水平视场角，竖直按窗口宽高比换算。"""
        fwd, rgt, up = self.rot_matrix(self.cam["pitch"], self.cam["yaw"], self.cam["roll"])
        dx, dy, dz = wx - self.cam["x"], wy - self.cam["y"], wz - self.cam["z"]
        forward = dx * fwd[0] + dy * fwd[1] + dz * fwd[2]
        if forward <= 0.05:          # 在相机后面
            return None
        hdot = dx * rgt[0] + dy * rgt[1] + dz * rgt[2]
        vdot = dx * up[0] + dy * up[1] + dz * up[2]
        fov_h = max(1.0, min(179.0, float(self.cam.get("fov") or 90.0)))
        tan_h = math.tan(math.radians(fov_h) * 0.5)
        aspect = (float(w) / float(h)) if h else (16.0 / 9.0)
        tan_v = tan_h / aspect
        if tan_h <= 1e-6 or tan_v <= 1e-6:
            return None
        sx = w * 0.5 + (hdot / forward) * (w * 0.5) / tan_h
        sy = h * 0.5 - (vdot / forward) * (h * 0.5) / tan_v
        return (sx, sy, forward)

    # --- 画一个目标 ------------------------------------------------------
    def draw_target(self, t: Dict[str, object], w: int, h: int, kind: str) -> None:
        box = t.get("box")  # [x1,y1,x2,y2] 世界坐标 AABB
        feet = t.get("pos")
        head = t.get("head")
        color_key = str(t.get("kind") or "other")
        color = self.colors.get(color_key, self.colors["other"])
        dist = float(t.get("distance") or 0.0)
        sk = self.cfg
        if not (sk["box"] or sk["name"] or sk["distance"] or sk["tracer"]):
            return

        # 计算投影
        corners: List[Tuple[float, float, float]] = []
        if isinstance(box, (list, tuple)) and len(box) == 6:
            b = [float(v) for v in box]
            for xi in (b[0], b[3]):
                for yi in (b[1], b[4]):
                    for zi in (b[2], b[5]):
                        p = self.world_to_screen(xi, yi, zi, w, h)
                        if p:
                            corners.append(p)
        sx = sy = None
        if corners:
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            x1, x2 = min(xs), max(xs)
            y1, y2 = min(ys), max(ys)
            if x2 - x1 < 4 or y2 - y1 < 6:
                # 太远，给一个最小框
                pad = 5
                x1, x2, y1, y2 = x1 - pad, x2 + pad, y1 - pad, y2 + pad
            if sk["box"]:
                self.ov.box(x1, y1, x2, y2, color, 1)
            if sk["name"] or sk["distance"]:
                label = str(t.get("label") or "")
                if sk["distance"]:
                    label = "%s [%dm]" % (label, int(dist)) if label else "%dm" % int(dist)
                if label:
                    self.ov.text(x1 + 2, y1 - 8, label, color, True)
            if sk["tracer"]:
                ox = w * 0.5
                oy = h if self.origin_kind == "bottom" else h * 0.5
                self.ov.line(ox, oy, (x1 + x2) * 0.5, y2, color, 1)
        elif feet is not None:
            # 退化路径：只有位置也能画个点/距离标签
            p = self.world_to_screen(float(feet[0]), float(feet[1]), float(feet[2]), w, h)
            if p and sk["distance"]:
                self.ov.text(p[0] + 4, p[1], "%dm" % int(dist), color, False)

    # --- 雷达 -----------------------------------------------------------
    def draw_radar(self, targets: Sequence[Dict[str, object]], w: int, h: int,
                   radius: int = 90, scale: float = 0.05) -> None:
        if not self.cfg["radar"] or not targets:
            return
        cx, cy = 120, h - 120
        self.ov.box(cx - radius, cy - radius, cx + radius, cy + radius, self.colors["radar"], 1)
        self.ov.line(cx - radius, cy, cx + radius, cy, self.colors["radar"], 1)
        self.ov.line(cx, cy - radius, cx, cy + radius, self.colors["radar"], 1)
        yaw = math.radians(self.cam["yaw"])
        cos_y, sin_y = math.cos(-yaw), math.sin(-yaw)
        for t in targets:
            pos = t.get("pos")
            if not pos:
                continue
            dx = (float(pos[0]) - self.cam["x"]) * scale
            dy = (float(pos[1]) - self.cam["y"]) * scale
            rx = dx * cos_y - dy * sin_y
            ry = dx * sin_y + dy * cos_y
            if rx * rx + ry * ry > radius * radius:
                norm = radius / math.sqrt(rx * rx + ry * ry)
                rx *= norm
                ry *= norm
            px, py = int(cx + rx), int(cy - ry)
            color = self.colors.get(str(t.get("kind") or "other"), self.colors["other"])
            self.ov.box(px - 2, py - 2, px + 2, py + 2, color, 1)

    def draw_offscreen(self, targets: Sequence[Dict[str, object]], w: int, h: int) -> None:
        """屏幕外目标的方向指示：按与视角的夹角投到屏幕边缘。"""
        if not self.cfg["offscreen"]:
            return
        cx, cy = w * 0.5, h * 0.5
        fov_h = float(self.cam.get("fov") or 90.0)
        fov_v = fov_h / max(1e-3, (float(w) / float(h))) if h else 60.0
        for t in targets:
            pos = t.get("pos")
            if not pos:
                continue
            dx = float(pos[0]) - self.cam["x"]
            dy = float(pos[1]) - self.cam["y"]
            dz = float(pos[2]) - self.cam["z"]
            horiz = math.hypot(dx, dy)
            bearing = math.degrees(math.atan2(dy, dx))
            rel = (bearing - self.cam["yaw"] + 540.0) % 360.0 - 180.0
            elev = math.degrees(math.atan2(dz, max(1.0, horiz)))
            rel_e = elev - self.cam["pitch"]
            p = self.world_to_screen(float(pos[0]), float(pos[1]), float(pos[2]), w, h)
            if p and 0 <= p[0] <= w and 0 <= p[1] <= h:
                continue  # 已经在屏幕里，交给方框画
            kx = rel / max(1e-3, fov_h * 0.5)
            ky = rel_e / max(1e-3, fov_v * 0.5)
            m = 26.0
            x = util.clamp(cx + kx * (w * 0.5 - m), m, w - m)
            y = util.clamp(cy - ky * (h * 0.5 - m), m, h - m)
            color = self.colors.get(str(t.get("kind") or "other"), self.colors["other"])
            self.ov.box(x - 3, y - 3, x + 3, y + 3, color, 1)
            label = "%s %dm" % (str(t.get("label") or "")[:12], int(float(t.get("distance") or 0)))
            self.ov.text(x + 5, y - 6, label.strip(), color, False)
            self.ov.line(cx, cy if abs(y - cy) < 4 else (m if y < cy else h - m), x, y, color, 1)

    def draw_crosshair(self, w: int, h: int) -> None:
        if not self.cfg["crosshair"]:
            return
        cx, cy = w // 2, h // 2
        self.ov.line(cx - 8, cy, cx - 3, cy, self.colors["text"], 1)
        self.ov.line(cx + 3, cy, cx + 8, cy, self.colors["text"], 1)
        self.ov.line(cx, cy - 8, cx, cy - 3, self.colors["text"], 1)
        self.ov.line(cx, cy + 3, cx, cy + 8, self.colors["text"], 1)
