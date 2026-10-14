"""日志与小工具。"""

from __future__ import annotations

import math
import sys
import threading
import time
from collections import deque
from typing import Any, Deque, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]


def now() -> float:
    return time.perf_counter()


class Log:
    """同时输出到控制台与内存环形缓冲（供 bridge 读取）。"""

    def __init__(self, level: str = "info", capacity: int = 400):
        self.levels = {"debug": 10, "info": 20, "warn": 30, "error": 40}
        self.level = self.levels.get(level, 20)
        self.ring: Deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def _emit(self, tag: str, msg: str) -> None:
        line = "[%s] %-5s %s" % (time.strftime("%H:%M:%S"), tag, msg)
        with self._lock:
            self.ring.append(line)
            try:
                print(line, flush=True)
            except Exception:
                pass

    def debug(self, msg: str) -> None:
        if self.level <= 10:
            self._emit("DEBUG", msg)

    def info(self, msg: str) -> None:
        if self.level <= 20:
            self._emit("INFO", msg)

    def warn(self, msg: str) -> None:
        if self.level <= 30:
            self._emit("WARN", msg)

    def error(self, msg: str) -> None:
        self._emit("ERROR", msg)

    def tail(self, n: int = 120) -> Sequence[str]:
        with self._lock:
            items = list(self.ring)
        return items[-n:]


def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else (hi if value > hi else value)


def is_finite(*values: float) -> bool:
    for v in values:
        if v != v or v in (float("inf"), float("-inf")):
            return False
        if not math.isfinite(v):
            return False
    return True


def plausible_vec(v: Optional[Sequence[float]], limit: float = 2.0e6) -> bool:
    if not v or len(v) < 3:
        return False
    x, y, z = float(v[0]), float(v[1]), float(v[2])
    if not is_finite(x, y, z):
        return False
    if abs(x) > limit or abs(y) > limit or abs(z) > limit:
        return False
    if x == 0.0 and y == 0.0 and z == 0.0:
        return False
    return True


def dist3(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def hex_to_colorref(text: str, default: int = 0x00FFFFFF) -> int:
    """把 "#RRGGBB" / "#AARRGGBB" 转成 GDI COLORREF (0x00BBGGRR)。"""
    if not text:
        return default
    t = text.strip().lstrip("#")
    try:
        if len(t) == 8:
            t = t[2:]
        if len(t) != 6:
            return default
        r = int(t[0:2], 16)
        g = int(t[2:4], 16)
        b = int(t[4:6], 16)
        return (b << 16) | (g << 8) | r
    except Exception:
        return default


def hex_alpha(text: str, default: int = 255) -> int:
    if not text:
        return default
    t = text.strip().lstrip("#")
    if len(t) == 8:
        try:
            return int(t[0:2], 16)
        except Exception:
            return default
    return default


VK_ALIASES = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "end": 0x23, "home": 0x24, "insert": 0x2D, "delete": 0x2E, "pgup": 0x21,
    "pgdn": 0x22, "tab": 0x09, "capslock": 0x14, "space": 0x20, "esc": 0x1B,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69,
}


def parse_vk(name: str) -> int:
    """把配置里的按键名转成虚拟键码。"""
    if not name:
        return 0
    n = str(name).strip().lower()
    if n in VK_ALIASES:
        return VK_ALIASES[n]
    if len(n) == 2 and n[0] == "f" and n[1:].isdigit():
        idx = int(n[1:])
        if 1 <= idx <= 24:
            return 0x70 + idx - 1
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    if n.startswith("0x"):
        try:
            return int(n, 16)
        except Exception:
            return 0
    if n.isdigit():
        return int(n)
    return 0
