"""本地玩家识别：把 PlayerController / PlayerCameraManager / Pawn 串起来。

思路（不写死偏移）：
    1. 枚举所有 PlayerController 实例；
    2. 在它们的字段区间里找指向 PlayerCameraManager 实例的指针 -> 得到 camera_manager 偏移；
    3. 同一区间里找指向 Character/Pawn 实例的指针 -> 得到 pawn 偏移；
    4. 观察一段时间，POV 真正在动（或指向本地）的那个就是本地 PlayerController。

这样即使在观战/回放场景里有多个 PlayerController，也能挑到本地那一个。
"""

from __future__ import annotations

import struct
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import util

Q = struct.Struct("<Q")


def find_by_class(ue, needle: str, limit: int = 32) -> List[int]:
    out: List[int] = []
    for _idx, obj in ue.iter_items():
        cn = ue.class_name_of(obj)
        if cn and needle.lower() in cn.lower():
            out.append(obj)
            if len(out) >= limit:
                break
    return out


def link_fields(ue, pcs: Sequence[int], pcm_hint: str = "PlayerCameraManager",
                pawn_hint: str = "Pawn") -> Dict[str, int]:
    """在 PlayerController 上找 camera_manager / pawn 字段偏移。"""
    votes: Dict[str, Dict[int, int]] = {"camera_manager": {}, "pawn": {}}
    for pc in pcs[:8]:
        raw = ue.proc.read(pc, 0x900)
        if not raw:
            continue
        for off in range(0, len(raw) - 8, 8):
            ptr = Q.unpack_from(raw, off)[0]
            if not (0x10000 < ptr < (1 << 47)):
                continue
            cn = ue.class_name_of(ptr)
            if not cn:
                continue
            low = cn.lower()
            if pcm_hint.lower() in low:
                votes["camera_manager"][off] = votes["camera_manager"].get(off, 0) + 1
            elif pawn_hint.lower() in low or "character" in low:
                votes["pawn"][off] = votes["pawn"].get(off, 0) + 1
    out: Dict[str, int] = {}
    for key, tally in votes.items():
        if tally:
            off, count = max(tally.items(), key=lambda kv: kv[1])
            if count >= 1:
                out[key] = off
    return out


def pick_local_pc(ue, pcs: Sequence[int], cam_mgr_off: int, pov_off: int,
                  log, window: float = 0.30, dtype: str = "f64") -> int:
    """挑本地 PlayerController：相机位移最大的那个。"""
    if not pcs:
        return 0
    if len(pcs) == 1:
        return pcs[0]
    snap: Dict[int, Tuple[float, Tuple[float, float, float]]] = {}
    for pc in pcs:
        mgr = ue.proc.u64(pc + cam_mgr_off) if cam_mgr_off else 0
        if not mgr:
            continue
        v = ue.proc.vec3(mgr + pov_off, dtype)
        if v:
            snap[pc] = (mgr, v)
    if not snap:
        return pcs[0]
    time.sleep(window)
    best: Optional[Tuple[float, int]] = None
    for pc, (mgr, old) in snap.items():
        v = ue.proc.vec3(mgr + pov_off, dtype)
        if not v:
            continue
        d = util.dist3(v, old)
        if best is None or d > best[0]:
            best = (d, pc)
    if best:
        log.info("本地 PlayerController 选定：0x%X（%.2fs 内相机位移 %.1fcm）"
                 % (best[1], window, best[0]))
        return best[1]
    return pcs[0]


class LocalPlayer:
    """本地玩家状态：PC / PCM / Pawn / 相机。"""

    def __init__(self, ue, log):
        self.ue = ue
        self.log = log
        self.pc = 0
        self.pcm = 0
        self.pawn = 0
        self.offs: Dict[str, int] = {}
        self.camera: Dict[str, float] = {"x": 0.0, "y": 0.0, "z": 0.0,
                                         "pitch": 0.0, "yaw": 0.0, "roll": 0.0,
                                         "fov": 90.0}
        self.valid = False

    def init(self) -> bool:
        ue = self.ue
        pcs = find_by_class(ue, "PlayerController", limit=16)
        pcms = find_by_class(ue, "PlayerCameraManager", limit=16)
        self.log.info("PlayerController=%d PlayerCameraManager=%d" % (len(pcs), len(pcms)))
        if not pcms:
            self.log.warn("当前没有 PlayerCameraManager（还没进入对局？）")
            return False
        from . import autoloc
        pov = autoloc.discover_pov_offset(ue, pcms, self.log)
        if not pov:
            self.log.warn("POV 字段没找到，相机读取不可用")
            return False
        self.offs.update(pov)
        self.offs["camera_manager"] = 0
        self.offs["pawn"] = 0
        if pcs:
            link = link_fields(ue, pcs)
            self.offs.update(link)
            if "camera_manager" in link:
                self.pc = pick_local_pc(ue, pcs, link["camera_manager"],
                                        int(pov["pov"]), self.log,
                                        dtype=str(pov.get("camera_dtype") or "f64"))
            else:
                self.pc = pcs[0]
            if self.pc:
                mgr = ue.proc.u64(self.pc + link.get("camera_manager", 0)) if link.get("camera_manager") else 0
                self.pcm = mgr or pcms[0]
                if link.get("pawn"):
                    self.pawn = ue.proc.u64(self.pc + link["pawn"])
        else:
            self.pcm = autoloc.pick_live_pcm(ue, pcms, pov["pov"], self.log)
        if not self.pcm:
            self.pcm = pcms[0]
        self.log.info("本地玩家绑定：PC=0x%X PCM=0x%X Pawn=0x%X 偏移=%s"
                      % (self.pc, self.pcm, self.pawn,
                         ", ".join("%s=0x%X" % (k, v) for k, v in sorted(self.offs.items()))))
        return True

    def refresh(self) -> bool:
        """重新绑定（换局、复活后调用）。"""
        self.pc = self.pcm = self.pawn = 0
        return self.init()

    def read_camera(self) -> Optional[Dict[str, float]]:
        ue = self.ue
        pcm = self.pcm
        if not pcm:
            return None
        if not ue.proc.u64(pcm):  # 对象被回收
            return None
        pov = self.offs.get("pov")
        if pov is None:
            return None
        dtype = str(self.offs.get("camera_dtype") or "f64")
        loc = ue.proc.vec3(pcm + pov, dtype)
        rot = ue.proc.vec3(pcm + int(self.offs.get("camera_rotation", pov + 0x18)))
        fov = ue.proc.f32(pcm + int(self.offs.get("camera_fov", pov + 0x24)), 90.0)
        if not loc or not rot:
            return None
        if not util.plausible_vec(loc) or not util.is_finite(*rot):
            return None
        cam = {
            "x": float(loc[0]), "y": float(loc[1]), "z": float(loc[2]),
            "pitch": float(rot[0]), "yaw": float(rot[1]), "roll": float(rot[2]),
            "fov": float(fov) if 25.0 <= fov <= 150.0 else 90.0,
        }
        self.camera = cam
        self.valid = True
        return cam

    def head_position(self) -> Optional[Tuple[float, float, float]]:
        if not self.pawn:
            return None
        from .esp import Scanner
        # 用玩家自身的相机位置当作头部参考
        return (self.camera["x"], self.camera["y"], self.camera["z"])
