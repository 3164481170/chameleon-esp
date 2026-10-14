"""配置读写（JSON）。命令行参数优先于配置文件。"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

DEFAULT: Dict[str, Any] = {
    "process": {
        "pid": 0,
        "window_title": "MECCHA CHAMELEON",
        "exe": "PenguinHotel-Win64-Shipping.exe"
    },
    "overlay": {
        "mode": "dc",
        "fps": 144,
        "font": "Consolas",
        "font_size": 13,
        "color_key": "#000000"
    },
    "read": {
        "interval_ms": 16,
        "full_scan_interval": 5.0,
        "max_distance": 12000.0,
        "max_objects": 0
    },
    "draw": {
        "box": True,
        "name": True,
        "distance": True,
        "tracer": False,
        "radar": True,
        "crosshair": True,
        "stats": True,
        "offscreen_marker": True,
        "skeleton": False
    },
    "esp": {
        "show_hider": True,
        "show_hunter": True,
        "show_other": False,
        "show_dummy": False,
        "hide_self": True,
        "tracer_origin": "bottom"
    },
    "keys": {
        "toggle": "F4",
        "rescan": "F5",
        "toggle_radar": "F6",
        "toggle_other": "F7",
        "toggle_stats": "F8",
        "exit": "END"
    },
    "class_patterns": ["FirstPersonCharacter", "Character", "Pawn", "PlayerState"],
    "colors": {
        "hider": "#00FF3C",
        "hunter": "#FF2D2D",
        "other": "#00E5FF",
        "dummy": "#FF00E5",
        "radar": "#3CA0FF",
        "text": "#FFFFFF"
    },
    "offsets_file": "offsets.json",
    "dump_dir": "dumps",
    "log_level": "info"
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: Dict[str, Any]):
        self.data = _merge(DEFAULT, data or {})

    def __getitem__(self, item: str) -> Any:
        return self.data[item]

    def get(self, item: str, default: Any = None) -> Any:
        return self.data.get(item, default)

    def set(self, path: str, value: Any) -> None:
        cur = self.data
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    @classmethod
    def load(cls, path: str) -> "Config":
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return cls(json.load(f))
            except Exception:
                pass
        return cls({})

    def save(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def ensure_file(self, path: str) -> None:
        if not os.path.exists(path):
            self.save(path)
