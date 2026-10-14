"""目标采集与持续跟踪。

分两级：
    full_scan()   全量枚举 UObject 数组，用类名规则筛出候选（慢，秒级，做一次后缓存）
    light_scan()  只对缓存候选读坐标（快，亚毫秒级，每帧可用）

候选刷新策略：
    * 启动时做一次全量；
    * 之后按 refresh_interval 秒在读取线程里后台刷新；
    * 按下刷新热键时立即重扫。

“移动作弊”检测：相邻两次扫描位置发生变化的对象标记为 moving，可用于把静止的
场景装饰（桌椅、树）和真正的玩家/伪装道具区分开。
"""

from __future__ import annotations

import math
import struct
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import util
from .filters import Filter, HIDER, HUNTER, OTHER, SKIP, DUMMY
from .ue import Ue

CHARACTER_HEIGHT = 175.0
PROP_HEIGHT = 60.0
CHARACTER_HALF_WIDTH = 42.0
PROP_HALF_WIDTH = 45.0

DEFAULT_CLASS_PATTERNS = (
    "FirstPersonCharacter", "Character", "Pawn", "PlayerState",
)


class Candidate:
    __slots__ = ("obj", "cls", "class_name", "object_name", "kind", "pos", "last_pos",
                 "moving", "last_seen", "height")

    def __init__(self, obj: int, cls: int, class_name: str, object_name: str, kind: str):
        self.obj = obj
        self.cls = cls
        self.class_name = class_name
        self.object_name = object_name
        self.kind = kind
        self.pos: Optional[Tuple[float, float, float]] = None
        self.last_pos: Optional[Tuple[float, float, float]] = None
        self.moving = False
        self.last_seen = 0.0
        self.height = CHARACTER_HEIGHT if "Character" in class_name or "Pawn" in class_name else PROP_HEIGHT

    def __repr__(self) -> str:
        return "<%s %s @0x%X #%s>" % (self.kind, self.object_name or self.class_name,
                                      self.obj, self.pos)


class Scanner:
    def __init__(self, ue: Ue, filt: Filter, log,
                 refresh_interval: float = 5.0,
                 include_other: bool = True,
                 max_distance: float = 12000.0,
                 patterns: Sequence[str] = DEFAULT_CLASS_PATTERNS,
                 item_limit: int = 0):
        self.ue = ue
        self.filt = filt
        self.log = log
        self.refresh_interval = refresh_interval
        self.include_other = include_other
        self.max_distance = max_distance
        self.patterns = tuple(p.lower() for p in patterns)
        self.item_limit = item_limit
        self.candidates: Dict[int, Candidate] = {}
        self.class_hist: Dict[str, int] = {}
        self.last_full = 0.0
        self.full_scans = 0
        self.scan_seconds = 0.0
        self.self_obj = 0
        self.self_override = ""

    # ------------------------------------------------------------------
    def _interesting(self, class_name: str, object_name: str) -> bool:
        cn = class_name.lower()
        on = object_name.lower()
        if not cn:
            return False
        if any(p in cn for p in self.patterns):
            return True
        # 伪装道具/可交互物件：类名带这些关键词的也收
        for key in ("craftedobject", "randomobject", "spawner", "prop", "decoy"):
            if key in cn or key in on:
                return True
        return False

    def full_scan(self) -> Dict[str, int]:
        t0 = time.perf_counter()
        ue = self.ue
        self.class_hist.clear()
        found: Dict[int, Candidate] = {}
        seen = 0
        total = ue.num()
        if self.item_limit:
            total = min(total, self.item_limit)
        for idx, obj in ue.iter_items(max_objects=self.item_limit or None):
            seen += 1
            cls = ue.class_of(obj)
            if not cls:
                continue
            cn = ue.name_of(cls)
            if not cn:
                continue
            self.class_hist[cn] = self.class_hist.get(cn, 0) + 1
            on = ue.name_of(obj)
            if not self._interesting(cn, on):
                continue
            kind = self.filt.classify(cn, on)
            if kind == SKIP:
                continue
            if kind == OTHER and not self.include_other:
                continue
            found[obj] = Candidate(obj, cls, cn, on, kind)
        # 合并：保留仍在场的旧候选（保留 moving 状态历史）
        merged: Dict[int, Candidate] = {}
        for obj, cand in found.items():
            old = self.candidates.get(obj)
            if old is not None:
                cand.last_pos = old.last_pos
                cand.moving = old.moving
                cand.last_seen = old.last_seen
            merged[obj] = cand
        for obj, old in self.candidates.items():
            if obj not in merged and (time.perf_counter() - old.last_seen) < 2.0:
                merged[obj] = old
        self.candidates = merged
        self.last_full = time.perf_counter()
        self.full_scans += 1
        self.scan_seconds = self.last_full - t0
        if self.self_override:
            for cand in self.candidates.values():
                if self.self_override.lower() in (cand.class_name + " " + cand.object_name).lower():
                    self.self_obj = cand.obj
                    break
        self.log.info("全量扫描：对象 %d，候选 %d，耗时 %.2fs（累计 %d 次）"
                      % (seen, len(self.candidates), self.scan_seconds, self.full_scans))
        return self.class_hist

    # ------------------------------------------------------------------
    def read_location(self, obj: int, offs: Dict[str, int],
                      dtype: str = "f32") -> Optional[Tuple[float, float, float]]:
        """优先用“运动反推”得到的直接坐标偏移，其次退回 RootComponent 链。"""
        ue = self.ue
        pos_off = offs.get("position")
        if pos_off:
            use = str(offs.get("position_dtype") or dtype or "f32")
            v = ue.proc.vec3(obj + int(pos_off), "f64" if use == "f64" else "f32")
            if util.plausible_vec(v):
                return v
            return None
        root_off = offs.get("root_component") or 0
        loc_off = offs.get("relative_location") or 0
        if root_off and loc_off:
            root = ue.proc.u64(obj + root_off)
            if root:
                if dtype == "f64":
                    v = ue.proc.vec3(root + loc_off, "f64")
                else:
                    v = ue.proc.vec3(root + loc_off)
                if util.plausible_vec(v):
                    return v
        return None

    def light_scan(self, offs: Dict[str, int], dtype: str = "f32") -> List[Candidate]:
        now = time.perf_counter()
        out: List[Candidate] = []
        for cand in list(self.candidates.values()):
            pos = self.read_location(cand.obj, offs, dtype)
            if pos is None:
                continue
            cand.last_pos = cand.pos
            cand.pos = pos
            cand.last_seen = now
            if cand.last_pos:
                d = util.dist3(cand.pos, cand.last_pos)
                if d > 1.0:
                    cand.moving = True
            out.append(cand)
        return out

    # ------------------------------------------------------------------
    def pick_self(self, camera: Dict[str, float], targets: Sequence[Candidate]) -> int:
        """本地玩家 = 离相机最近、类名像主角的那个候选。"""
        if self.self_obj:
            for cand in targets:
                if cand.obj == self.self_obj:
                    return self.self_obj
        best = None
        for cand in targets:
            if not cand.pos:
                continue
            if "spectate" in cand.class_name.lower():
                continue
            d = util.dist3(cand.pos, (camera["x"], camera["y"], camera["z"]))
            score = d
            cn = cand.class_name.lower()
            if "main" in cn or "firstperson" in cn:
                score -= 200.0
            if best is None or score < best[0]:
                best = (score, cand.obj)
        if best:
            self.self_obj = best[1]
        return self.self_obj

    # ------------------------------------------------------------------
    def to_render_list(self, camera: Dict[str, float], targets: Sequence[Candidate],
                       max_distance: Optional[float] = None) -> List[Dict[str, object]]:
        limit = max_distance or self.max_distance
        cam = (camera["x"], camera["y"], camera["z"])
        out: List[Dict[str, object]] = []
        for cand in targets:
            if not cand.pos:
                continue
            if cand.obj == self.self_obj:
                continue
            cn = cand.class_name.lower()
            if "spectate" in cn:
                continue
            d = util.dist3(cand.pos, cam)
            if d > limit:
                continue
            is_char = "character" in cn or "pawn" in cn
            h = CHARACTER_HEIGHT if is_char else PROP_HEIGHT
            hw = CHARACTER_HALF_WIDTH if is_char else PROP_HALF_WIDTH
            x, y, z = cand.pos
            label = cand.object_name or cand.class_name
            if label.endswith("_C"):
                label = label[:-2]
            out.append({
                "obj": cand.obj,
                "kind": cand.kind,
                "class": cand.class_name,
                "label": label,
                "distance": d,
                "moving": cand.moving,
                "pos": (x, y, z),
                "head": (x, y, z + h),
                "box": (x - hw, y - hw, z, x + hw, y + hw, z + h),
            })
        out.sort(key=lambda t: float(t["distance"]))
        return out

    # ------------------------------------------------------------------
    def dump_report(self, path: str, top: int = 60) -> str:
        lines: List[str] = []
        lines.append("# 对象类名统计（full_scan 结果）")
        lines.append("")
        lines.append("扫描时间：%.2fs，候选数：%d" % (self.scan_seconds, len(self.candidates)))
        lines.append("")
        lines.append("## 数量最多的类")
        lines.append("")
        lines.append("| 类名 | 实例数 | 判定 |")
        lines.append("| --- | --- | --- |")
        for cn, cnt in sorted(self.class_hist.items(), key=lambda kv: -kv[1])[:top]:
            lines.append("| %s | %d | %s |" % (cn, cnt, self.filt.classify(cn, "")))
        lines.append("")
        lines.append("## 候选目标")
        lines.append("")
        lines.append("| 类名 | 对象名 | 类别 | 高度 | 备注 |")
        lines.append("| --- | --- | --- | --- | --- |")
        seen = set()
        for cand in sorted(self.candidates.values(), key=lambda c: c.class_name):
            key = (cand.class_name, cand.kind)
            note = "重复" if key in seen else "首次"
            seen.add(key)
            lines.append("| %s | %s | %s | %.0f | %s |"
                         % (cand.class_name, cand.object_name, cand.kind, cand.height, note))
        text = "\n".join(lines)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as exc:
            self.log.warn("写报告失败：%s" % exc)
        return path
