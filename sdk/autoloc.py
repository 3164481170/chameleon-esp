"""从“运动 + 一致性”里反推坐标字段，不依赖任何硬编码偏移。

为什么不用特征码：UE 每次更新都会重排指令、重排结构；但有一条性质永远成立——
**角色的世界坐标会随移动而改变**。据此可以纯统计地把坐标字段找出来。

UE5 的重要细节：UE5 起启用 Large World Coordinates，FVector 是 3 个 **double**
（24 字节），FRotator 仍是 3 个 float。所以这里 f32 / f64 两种布局都要试。

三层降级策略：
    1. motion_vote      —— 首选：采多帧，找“变化量最大、且多对象同时成立”的字段
    2. static_vote      —— 兜底：没人动的时候，找“在绝大多数对象上都读出合理世界坐标”的字段
    3. root_component   —— 再兜底：走 RootComponent -> RelativeLocation 组件链（见 resolver）
"""

from __future__ import annotations

import math
import struct
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import util

Vec3 = Tuple[float, float, float]

POS_LIMIT_XY = 60000.0     # 世界 XY 半径上限（厘米）
POS_LIMIT_Z = 12000.0      # 高度上限（厘米）
MIN_ABS = 0.01             # 分量要么是 0，要么不该是这种极小数（用来过滤 double 被当 float 读的噪声）


def _plausible_world(v: Sequence[float], limit_xy: float = POS_LIMIT_XY,
                     limit_z: float = POS_LIMIT_Z) -> bool:
    x, y, z = float(v[0]), float(v[1]), float(v[2])
    if not util.is_finite(x, y, z):
        return False
    if abs(x) > limit_xy or abs(y) > limit_xy or abs(z) > limit_z:
        return False
    for c in (x, y, z):
        if c != 0.0 and abs(c) < MIN_ABS:
            return False
    if x == 0.0 and y == 0.0 and z == 0.0:
        return False
    return True


def _rot_plausible(v: Sequence[float]) -> bool:
    p, y, r = float(v[0]), float(v[1]), float(v[2])
    if not util.is_finite(p, y, r):
        return False
    if not (-90.5 <= p <= 90.5):
        return False
    if abs(y) > 360.5 or abs(r) > 360.5:
        return False
    return True


def candidate_fields(ue, obj: int, lo: int, hi: int) -> List[Tuple[int, str]]:
    """列出对象内存里“像世界坐标”的 (偏移, 数据类型) 候选。"""
    raw = ue.proc.read(obj + lo, hi - lo)
    out: List[Tuple[int, str]] = []
    if not raw:
        return out
    # float 布局：任意 4 字节对齐
    for off in range(0, len(raw) - 12, 4):
        if _plausible_world(struct.unpack_from("<3f", raw, off)):
            out.append((lo + off, "f32"))
    # double 布局（UE5 LWC）：8 字节对齐
    for off in range(0, len(raw) - 24, 8):
        if _plausible_world(struct.unpack_from("<3d", raw, off)):
            out.append((lo + off, "f64"))
    return out


def _read_vec(ue, addr: int, dtype: str) -> Optional[Vec3]:
    v = ue.proc.vec3(addr, dtype)
    if v is None:
        return None
    return (float(v[0]), float(v[1]), float(v[2]))


# =========================================================================
# 1) 运动反推
# =========================================================================
def motion_vote(ue, objects: Sequence[int], lo: int = 0x80, hi: int = 0x700,
                samples: int = 8, interval: float = 0.07,
                min_movers: int = 1) -> Dict[str, object]:
    objects = [o for o in objects if o][:24]
    if not objects:
        return {}
    base: Dict[int, Dict[Tuple[int, str], Vec3]] = {}
    for o in objects:
        base[o] = {}
        for off, dtype in candidate_fields(ue, o, lo, hi):
            v = _read_vec(ue, o + off, dtype)
            if v:
                base[o][(off, dtype)] = v

    deltas: Dict[Tuple[int, str], List[float]] = {}
    for _ in range(max(1, samples - 1)):
        time.sleep(interval)
        for o in list(base.keys()):
            for key in list(base[o].keys()):
                v = _read_vec(ue, o + key[0], key[1])
                if not v:
                    continue
                d = util.dist3(v, base[o][key])
                if util.is_finite(d) and d < 1e7:
                    deltas.setdefault(key, []).append(d)

    scored: List[Tuple[float, int, str, float, int]] = []
    for (off, dtype), ds in deltas.items():
        if not ds:
            continue
        movers = sum(1 for d in ds if d > 0.5)
        peak = max(ds)
        if movers < min_movers or peak < 0.5:
            continue
        # 同分优先 double（UE5 LWC 的真实布局），其次偏大位移
        bonus = 0.5 if dtype == "f64" else 0.0
        scored.append((movers + bonus + min(peak, 500.0) / 1000.0, off, dtype, peak, movers))
    scored.sort(reverse=True)
    if not scored:
        return {}
    best = scored[0]
    return {
        "offset": best[1],
        "dtype": best[2],
        "peak_delta": best[3],
        "movers": best[4],
        "alternatives": [{"offset": s[1], "dtype": s[2], "peak": s[3], "movers": s[4]}
                         for s in scored[1:6]],
    }


# =========================================================================
# 2) 静态一致性投票（没人动时的兜底）
# =========================================================================
def static_vote(ue, objects: Sequence[int], lo: int = 0x80, hi: int = 0x700,
                min_ratio: float = 0.75) -> Dict[str, object]:
    objects = [o for o in objects if o][:32]
    if len(objects) < 3:
        return {}
    tally: Dict[Tuple[int, str], List[Vec3]] = {}
    for o in objects:
        for off, dtype in candidate_fields(ue, o, lo, hi):
            v = _read_vec(ue, o + off, dtype)
            if v:
                tally.setdefault((off, dtype), []).append(v)
    best: Optional[Tuple[float, int, str, int]] = None
    for (off, dtype), vs in tally.items():
        ratio = len(vs) / float(len(objects))
        if ratio < min_ratio:
            continue
        # 位置要有分散度（所有对象同一个值说明是常量，不是坐标）
        xs = [v[0] for v in vs]
        ys = [v[1] for v in vs]
        zs = [v[2] for v in vs]
        spread = (max(xs) - min(xs)) + (max(ys) - min(ys)) + (max(zs) - min(zs))
        if spread < 5.0:
            continue
        score = ratio * 10.0 + min(spread / 1000.0, 5.0) + (0.5 if dtype == "f64" else 0.0)
        if best is None or score > best[0]:
            best = (score, off, dtype, len(vs))
    if not best:
        return {}
    return {"offset": best[1], "dtype": best[2], "hits": best[3],
            "ratio": best[3] / float(len(objects))}


def resolve_position_offset(ue, samples: Sequence[int], log,
                           lo: int = 0x80, hi: int = 0x700) -> Dict[str, object]:
    """主入口：返回 {"position": off, "position_dtype": "f32"|"f64"} 或 {}。"""
    mv = motion_vote(ue, samples, lo=lo, hi=hi)
    if mv:
        log.info("运动反推坐标字段：obj+0x%X (%s)，峰值位移 %.1fcm，命中对象 %d 个"
                 % (mv["offset"], mv["dtype"], mv["peak_delta"], mv["movers"]))
        alts = mv.get("alternatives") or []
        if alts:
            log.debug("备选：%s" % ", ".join(
                "0x%X/%s(%.0fcm)" % (a["offset"], a["dtype"], a["peak"]) for a in alts))
        return {"position": int(mv["offset"]), "position_dtype": str(mv["dtype"])}
    sv = static_vote(ue, samples, lo=lo, hi=hi)
    if sv:
        log.info("静态一致性反推坐标字段：obj+0x%X (%s)，%d 个对象命中（比例 %.0f%%）"
                 % (sv["offset"], sv["dtype"], sv["hits"], sv["ratio"] * 100))
        return {"position": int(sv["offset"]), "position_dtype": str(sv["dtype"])}
    log.warn("两轮反推都没找到坐标字段：样本没动或对象太少，将退回 RootComponent 组件链")
    return {}


# =========================================================================
# 3) 相机 POV
# =========================================================================
def discover_pov_offset(ue, pcm_objs: Sequence[int], log) -> Dict[str, object]:
    """在 PlayerCameraManager 里找 FMinimalViewInfo（Location / Rotation / FOV）。

    同时支持两种内存布局：
        UE5 LWC : Location = 3×double(+0x00)，Rotation = 3×float(+0x18)，FOV = float(+0x24)
        UE4/兼容: Location = 3×float(+0x00)， Rotation = 3×float(+0x0C)，FOV = float(+0x18)
    """
    best: Optional[Tuple[float, int, str, int, int]] = None
    for pcm in pcm_objs[:4]:
        raw = ue.proc.read(pcm, 0x1400)
        if not raw:
            continue
        for off in range(0, len(raw) - 0x40, 4):
            for dtype, rot_off, fov_off in (("f64", 0x18, 0x24), ("f32", 0x0C, 0x18)):
                if dtype == "f64":
                    if off % 8 or off + 24 + 0x28 > len(raw):
                        continue
                    loc = struct.unpack_from("<3d", raw, off)
                else:
                    loc = struct.unpack_from("<3f", raw, off)
                if not _plausible_world(loc, 200000.0, 60000.0):
                    continue
                try:
                    rot = struct.unpack_from("<3f", raw, off + rot_off)
                    fov = struct.unpack_from("<f", raw, off + fov_off)[0]
                except Exception:
                    continue
                if not _rot_plausible(rot):
                    continue
                if not (util.is_finite(fov) and 25.0 <= fov <= 150.0):
                    continue
                score = 100.0 - abs(fov - 90.0) + (5.0 if dtype == "f64" else 0.0)
                if best is None or score > best[0]:
                    best = (score, off, dtype, off + rot_off, off + fov_off)
    if not best:
        log.warn("没找到 FMinimalViewInfo：相机读取不可用（可先用 --mode window 检查）")
        return {}
    _score, off, dtype, rot_off, fov_off = best
    log.info("相机 POV：+0x%X (%s) rotation=+0x%X fov=+0x%X"
             % (off, dtype, rot_off, fov_off))
    return {"pov": off, "camera_rotation": rot_off, "camera_fov": fov_off,
            "camera_dtype": dtype}


def pick_live_pcm(ue, pcm_objs: Sequence[int], pov_off: int, log,
                  dtype: str = "f64", window: float = 0.35) -> int:
    """多个 PlayerCameraManager 时挑 POV 真正在更新的那个（= 本地玩家）。"""
    if len(pcm_objs) <= 1:
        return pcm_objs[0] if pcm_objs else 0
    snap: Dict[int, Vec3] = {}
    for pcm in pcm_objs:
        v = _read_vec(ue, pcm + pov_off, dtype)
        if v:
            snap[pcm] = v
    time.sleep(window)
    best: Optional[Tuple[float, int]] = None
    for pcm, old in snap.items():
        v = _read_vec(ue, pcm + pov_off, dtype)
        if not v:
            continue
        d = util.dist3(v, old)
        if best is None or d > best[0]:
            best = (d, pcm)
    if best and best[0] > 0.0:
        log.info("选定相机：0x%X（%.2fs 内移动 %.1fcm）" % (best[1], window, best[0]))
        return best[1]
    return pcm_objs[0]
