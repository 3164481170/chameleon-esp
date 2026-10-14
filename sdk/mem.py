"""Win32 进程内存访问：句柄、模块枚举、批量读写、AOB 扫描。

全部基于 ctypes，无第三方依赖。只使用有文档的 Win32 API。
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import time
from typing import Optional, Sequence
from ctypes import wintypes
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

MEM_COMMIT = 0x1000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE = 0x10
PAGE_EXECUTE_READ = 0x20
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80
PAGE_TARGETS_INVALID = 0x40000000

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
MAX_PATH = 260


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_char * 256),
        ("szExePath", ctypes.c_char * MAX_PATH),
    ]


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


READABLE = (PAGE_READONLY, PAGE_READWRITE, PAGE_WRITECOPY,
            PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY)


class MemoryError_(RuntimeError):
    pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_frozen() -> bool:
    """是否为 PyInstaller 打包后的 exe 运行。"""
    return bool(getattr(sys, "frozen", False))


def relaunch_elevated(extra_args: Optional[Sequence[str]] = None) -> bool:
    """用 UAC 重新以管理员身份拉起自己。仅用于“读写目标进程被拒绝”的场景。

    * 打包成 exe：直接重启 exe 本身；
    * 源码运行：用当前解释器 + 脚本路径重启。
    返回 True 表示已经成功发起重启（调用方应当随之退出）。
    """
    if is_admin():
        return False
    args = list(extra_args if extra_args is not None else sys.argv[1:])
    args = [a for a in args if a != "--elevated"] + ["--elevated"]
    if is_frozen():
        exe = sys.executable
        params = args
    else:
        exe = sys.executable
        params = [os.path.abspath(sys.argv[0])] + args
    quoted = " ".join('"%s"' % a if (" " in a or "\t" in a) else a for a in params)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, quoted, None, 1)
    except Exception:
        return False
    return int(rc) > 32


def find_pids(names: Sequence[str]) -> List[Tuple[int, str]]:
    """用 tasklist 枚举进程（避免依赖第三方库）。返回 [(pid, name)]。"""
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True,
                         creationflags=0x08000000).stdout
    wanted = {n.lower() for n in names}
    found: List[Tuple[int, str]] = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].strip('"')
        if name.lower() in wanted:
            try:
                found.append((int(parts[1]), name))
            except ValueError:
                continue
    return found


def find_pid_by_window(title_substr: str) -> Optional[int]:
    """按窗口标题子串找进程（匹配不到就返回 None）。"""
    if not title_substr:
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    target = title_substr.lower()
    hits: List[Tuple[int, str]] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if target in title.lower():
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                hits.append((int(pid.value), title))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not hits:
        return None
    return hits[0][0]


class Process:
    """目标进程句柄 + 模块信息 + 读写封装。"""

    def __init__(self, pid: int, name: str = ""):
        self.pid = int(pid)
        self.name = name
        self.handle = 0
        self.modules: Dict[str, Tuple[int, int]] = {}
        self.base = 0
        self.size = 0

    # --- 生命周期 ---------------------------------------------------------
    def open(self) -> "Process":
        access = (PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION
                  | PROCESS_VM_READ)
        h = _k32.OpenProcess(access, False, self.pid)
        if not h:
            err = ctypes.get_last_error()
            raise MemoryError_("OpenProcess(%d) 失败，错误码 %d（以管理员身份运行可解决大部分情况）"
                               % (self.pid, err))
        self.handle = h
        self.refresh_modules()
        return self

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = 0

    def __enter__(self) -> "Process":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def alive(self) -> bool:
        if not self.handle:
            return False
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE

    # --- 模块 -------------------------------------------------------------
    def refresh_modules(self) -> Dict[str, Tuple[int, int]]:
        snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
        mods: Dict[str, Tuple[int, int]] = {}
        if snap and snap != -1:
            entry = MODULEENTRY32()
            entry.dwSize = ctypes.sizeof(MODULEENTRY32)
            ok = _k32.Module32First(snap, ctypes.byref(entry))
            while ok:
                name = entry.szModule.decode("utf-8", "replace")
                mods[name.lower()] = (int(ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0),
                                      int(entry.modBaseSize))
                ok = _k32.Module32Next(snap, ctypes.byref(entry))
            _k32.CloseHandle(snap)
        self.modules = mods
        return mods

    def module(self, name: str) -> Tuple[int, int]:
        key = name.lower()
        if key not in self.modules:
            self.refresh_modules()
        if key not in self.modules:
            raise MemoryError_("找不到模块 %s" % name)
        return self.modules[key]

    def main_module(self) -> Tuple[int, int]:
        ks = [k for k in self.modules if k.endswith(".exe")]
        if not ks:
            raise MemoryError_("没有找到 exe 模块")
        return self.modules[ks[0]]

    # --- 读 ---------------------------------------------------------------
    def read(self, address: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = _k32.ReadProcessMemory(self.handle, ctypes.c_void_p(address),
                                    buf, ctypes.c_size_t(size), ctypes.byref(got))
        if not ok:
            return b""
        return buf.raw[:got.value]

    def read_into(self, address: int, buf, size: int) -> int:
        got = ctypes.c_size_t(0)
        ok = _k32.ReadProcessMemory(self.handle, ctypes.c_void_p(address),
                                    buf, ctypes.c_size_t(size), ctypes.byref(got))
        return got.value if ok else 0

    def u8(self, address: int, default: int = 0) -> int:
        b = self.read(address, 1)
        return b[0] if b else default

    def i32(self, address: int, default: int = 0) -> int:
        b = self.read(address, 4)
        return struct.unpack("<i", b)[0] if len(b) == 4 else default

    def u32(self, address: int, default: int = 0) -> int:
        b = self.read(address, 4)
        return struct.unpack("<I", b)[0] if len(b) == 4 else default

    def u64(self, address: int, default: int = 0) -> int:
        b = self.read(address, 8)
        return struct.unpack("<Q", b)[0] if len(b) == 8 else default

    def f32(self, address: int, default: float = 0.0) -> float:
        b = self.read(address, 4)
        return struct.unpack("<f", b)[0] if len(b) == 4 else default

    def f64(self, address: int, default: float = 0.0) -> float:
        b = self.read(address, 8)
        return struct.unpack("<d", b)[0] if len(b) == 8 else default

    def vec3(self, address: int, dtype: str = "f32") -> Optional[Tuple[float, float, float]]:
        size = 12 if dtype == "f32" else 24
        b = self.read(address, size)
        if len(b) != size:
            return None
        if dtype == "f32":
            return struct.unpack("<3f", b)
        return struct.unpack("<3d", b)

    def unicode_str(self, address: int, max_chars: int = 128) -> str:
        if not address:
            return ""
        raw = self.read(address, max_chars * 2)
        if not raw:
            return ""
        text = raw.decode("utf-16-le", "ignore")
        cut = text.find("\x00")
        return text[:cut] if cut >= 0 else text

    # --- 写 ---------------------------------------------------------------
    def write(self, address: int, data: bytes) -> bool:
        if not self.handle:
            return False
        buf = ctypes.create_string_buffer(data, len(data))
        done = ctypes.c_size_t(0)
        if not _k32.VirtualProtectEx(self.handle, ctypes.c_void_p(address),
                                     ctypes.c_size_t(len(data)), PAGE_EXECUTE_READWRITE,
                                     ctypes.byref(wintypes.DWORD())):
            return False
        ok = _k32.WriteProcessMemory(self.handle, ctypes.c_void_p(address),
                                     buf, ctypes.c_size_t(len(data)), ctypes.byref(done))
        return bool(ok) and done.value == len(data)

    def write_f32(self, address: int, value: float) -> bool:
        return self.write(address, struct.pack("<f", value))

    def write_i32(self, address: int, value: int) -> bool:
        return self.write(address, struct.pack("<i", value))

    # --- 区域 -------------------------------------------------------------
    def regions(self, include_image: bool = False, include_mapped: bool = False) -> Iterator[Tuple[int, int, int]]:
        """遍历可读内存区域，产出 (base, size, protect)。"""
        mbi = MEMORY_BASIC_INFORMATION64()
        addr = 0
        limit = 0x00007FFFFFFF0000
        while addr < limit:
            got = _k32.VirtualQueryEx(self.handle, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not got:
                addr += 0x1000
                continue
            base = int(mbi.BaseAddress)
            size = int(mbi.RegionSize)
            if size <= 0:
                break
            protect = int(mbi.Protect)
            is_image = (int(mbi.Type) == MEM_IMAGE)
            is_mapped = (int(mbi.Type) == MEM_MAPPED)
            ok_state = (int(mbi.State) == MEM_COMMIT)
            ok_prot = (protect & 0xFF) in READABLE
            ok_type = (not is_image or include_image) and (not is_mapped or include_mapped)
            if ok_state and ok_prot and ok_type and not (protect & PAGE_GUARD):
                yield base, size, protect
            addr = base + size

    # --- 扫描 -------------------------------------------------------------
    def find_bytes(self, pattern: bytes, mask: Optional[bytes] = None,
                   base: int = 0, size: int = 0, step: int = 1,
                   chunk: int = 8 * 1024 * 1024,
                   limit: int = 32,
                   include_image: bool = False) -> List[int]:
        """AOB 扫描。mask 为等长字节串，b'x' 表示必须匹配，b'?' 表示通配。"""
        if not pattern:
            return []
        if mask is None:
            mask = b"x" * len(pattern)
        assert len(mask) == len(pattern)
        hay = pattern.replace(b"\x00", b"")
        if not hay:
            return []
        first = pattern[min(i for i, m in enumerate(mask) if m == ord("x"))]
        results: List[int] = []
        spans: Iterable[Tuple[int, int, int]]
        if base and size:
            spans = [(base, size, 0)]
        else:
            spans = list(self.regions(include_image=include_image))
        plen = len(pattern)
        for rbase, rsize, _prot in spans:
            off = 0
            while off < rsize:
                take = min(chunk, rsize - off)
                data = self.read(rbase + off, take)
                if data:
                    start = 0
                    while True:
                        idx = data.find(bytes([first]), start)
                        if idx < 0:
                            break
                        if idx + plen <= len(data):
                            ok = True
                            for i in range(plen):
                                if mask[i] == ord("x") and data[idx + i] != pattern[i]:
                                    ok = False
                                    break
                            if ok:
                                results.append(rbase + off + idx)
                                if len(results) >= limit:
                                    return results
                        start = idx + step
                off += take - (plen if take == chunk else 0)
                if take < chunk:
                    break
        return results


def parse_aob(text: str) -> Tuple[bytes, bytes]:
    """解析 "48 8B ?? E8 ? ? ? ?" 形式的特征码，返回 (pattern, mask)。"""
    pat = bytearray()
    msk = bytearray()
    for tok in text.replace(",", " ").split():
        if tok in ("?", "??", "**"):
            pat.append(0)
            msk.append(ord("?"))
        else:
            if len(tok) == 2 and tok[0] in "?*":
                pat.append(int(tok[1], 16))
                msk.append(ord("?"))
            else:
                pat.append(int(tok, 16))
                msk.append(ord("x"))
    return bytes(pat), bytes(msk)


def process_cmdline(pid: int) -> str:
    """读取进程命令行（用于区分 Steam 启动的多个实例）。"""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "ProcessId=%d" % pid, "get", "CommandLine", "/value"],
            capture_output=True, text=True, creationflags=0x08000000).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.lower().startswith("commandline="):
                return line.split("=", 1)[1]
    except Exception:
        pass
    return ""
