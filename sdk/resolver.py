"""运行期偏移自动发现与校准。

设计原则：**一个硬编码偏移都不写**。游戏更新、换编译器、换 UE 小版本，只要游戏本身
还在用标准的 UE 对象模型，这套逻辑就仍然成立。

流程：
    find_globals(proc)
        ├─ resolve_fname_pool()   找 GNames(FNamePool) 并校准其内部布局
        └─ resolve_uobject_array() 找 GObjects(FUObjectArray) 并校准其内部布局
    resolve_world_chain()   从 UObject 数组反推 World / GameState / Level.Actors
    resolve_location_offsets() 反推 RootComponent / RelativeLocation
    resolve_camera_offsets()   反推 PlayerCameraManager / POV
    （坐标字段的实际定位交给 autoloc.py 的“运动反推”，比静态特征码稳）

所有结论都以 JSON 落盘，下次启动直接复用；地址失效时自动重扫。
"""

from __future__ import annotations

import json
import os
import struct
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import util
from .mem import Process
from .ue import Ue, UeOffsets

Q = struct.Struct("<Q")

# 任意 UE 构建里都必然存在的名字，用来给候选池打分
KNOWN_NAMES = ("None", "Object", "Package", "Function", "Class", "IntProperty",
               "Actor", "World", "Component", "SceneComponent", "PlayerController",
               "BoolProperty", "Struct", "Enum", "ScriptStruct", "BlueprintGeneratedClass")

Span = Tuple[int, int]  # (base, size)


# =========================================================================
# 通用工具
# =========================================================================
def _is_module_ptr(value: int, base: int, size: int) -> bool:
    return base < value < base + size


class RegionMap:
    """已提交可读区域的区间表，用来快速判定任意指针是否可能有效。

    为什么需要：FNamePool 的块、UObject 实例都分配在进程堆上（MEM_PRIVATE），
    不在模块镜像里；只按“是否落在模块范围内”筛指针会把真正的候选全部漏掉。
    """

    def __init__(self, regions: Sequence[Tuple[int, int, int]]):
        self.starts: List[int] = []
        self.ends: List[int] = []
        pairs = sorted((int(b), int(b) + int(s)) for b, s, _p in regions if s > 0)
        merged: List[Tuple[int, int]] = []
        for b, e in pairs:
            if merged and b <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((b, e))
        for b, e in merged:
            self.starts.append(b)
            self.ends.append(e)

    def contains(self, addr: int) -> bool:
        import bisect
        if addr <= 0x10000 or addr >= (1 << 47):
            return False
        i = bisect.bisect_right(self.starts, addr) - 1
        return i >= 0 and self.starts[i] < addr < self.ends[i]

    def __len__(self) -> int:
        return len(self.starts)


def image_writable_spans(proc: Process, base: int, size: int) -> List[Span]:
    """主模块里可写的已提交区域（.data / .bss），全局变量都在这里。"""
    out: List[Span] = []
    for rbase, rsize, prot in proc.regions(include_image=True):
        if rbase + rsize <= base or rbase >= base + size:
            continue
        if prot & 0xCC:  # 含写权限
            out.append((rbase, rsize))
    return out


def any_writable_spans(proc: Process, max_region: int = 64 * 1024 * 1024,
                       limit_total: int = 1536 * 1024 * 1024) -> List[Span]:
    """全地址空间里可写的已提交区域（含堆）。镜像扫描失败时的兜底路径。"""
    out: List[Span] = []
    total = 0
    for rbase, rsize, prot in proc.regions(include_image=True, include_mapped=True):
        if not (prot & 0xCC):
            continue
        if rsize > max_region:
            continue
        out.append((rbase, rsize))
        total += rsize
        if total >= limit_total:
            break
    return out


def spans_for(proc: Process, exe_base: int, exe_size: int, scope: str) -> List[Span]:
    if scope == "image":
        return image_writable_spans(proc, exe_base, exe_size)
    spans = any_writable_spans(proc)
    return spans


def iter_span_hits(proc: Process, spans: Sequence[Span], pattern: bytes,
                   before: int = 0, after: int = 0,
                   chunk: int = 1 << 22) -> Iterable[Tuple[int, bytes]]:
    tail = b""
    tail_addr = 0
    for rbase, rsize in spans:
        off = 0
        while off < rsize:
            take = min(chunk, rsize - off)
            data = proc.read(rbase + off, take)
            if data:
                blob = tail + data
                blob_base = tail_addr if tail else rbase + off
                idx = 0
                while True:
                    hit = blob.find(pattern, idx)
                    if hit < 0:
                        break
                    yield blob_base + hit, blob[hit:hit + len(pattern) + after]
                    idx = hit + 1
                tail = blob[-before:] if before else b""
                tail_addr = blob_base + len(blob) - len(tail)
            off += take


# =========================================================================
# 1) FNamePool (GNames)
# =========================================================================
def find_pool_candidates(proc: Process, spans: Sequence[Span], rmap: RegionMap,
                         min_run: int = 3) -> List[Tuple[int, int]]:
    """找 Blocks[8192]：一段连续若干个指向“已提交可读内存”的指针。"""
    runs: List[Tuple[int, int]] = []
    for rbase, rsize in spans:
        data = proc.read(rbase, rsize)
        if not data:
            continue
        count = len(data) // 8
        run_start = -1
        run_len = 0
        for i in range(count):
            v = Q.unpack_from(data, i * 8)[0]
            if rmap.contains(v):
                if run_start < 0:
                    run_start = i
                run_len += 1
            else:
                if run_len >= min_run:
                    runs.append((rbase + run_start * 8, run_len))
                run_start, run_len = -1, 0
        if run_len >= min_run:
            runs.append((rbase + run_start * 8, run_len))
    return runs


def _score_pool(proc: Process, pool: int, header: int, layout: str, extra: int,
                stride: int, ids: Sequence[int]) -> Tuple[int, List[str]]:
    ue = Ue(proc)
    ue.pool = pool
    ue.fname_header, ue.fname_layout, ue.fname_extra, ue.fname_stride = header, layout, extra, stride
    names: List[str] = []
    score = 0
    for nid in ids:
        s = ue.name_at(nid)
        if not s:
            continue
        names.append(s)
        if all(32 <= ord(ch) < 127 for ch in s) and 1 <= len(s) <= 96:
            score += 1
            if s in KNOWN_NAMES:
                score += 6
    return score, names


QUICK_IDS: Tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
                           14, 15, 16, 17, 18, 19, 20, 24, 28, 32, 40, 48, 56, 64)
FULL_IDS: Tuple[int, ...] = QUICK_IDS + tuple(range(0x40, 0x4000, 0x20)) + \
    tuple(range(0x4000, 0x20000, 0x400))


def resolve_fname_pool(proc: Process, spans: Sequence[Span], rmap: RegionMap,
                       log) -> Tuple[int, Dict[str, object]]:
    """两阶段搜索：先用少量 id 快筛全部候选，再对前几名做全量打分。"""
    runs = find_pool_candidates(proc, spans, rmap)
    # FNamePool 的块以 64KB 对齐分配：数组里连续多个指针都以 0x10000 结尾才可能是它
    aligned: List[Tuple[int, int]] = []
    for arr, run_len in runs:
        ok = 0
        for i in range(min(run_len, 8)):
            p = proc.u64(arr + i * 8)
            if p and (p & 0xFFFF) == 0:
                ok += 1
        if ok >= 3:
            aligned.append((arr, run_len))
    if aligned:
        runs = aligned
    runs.sort(key=lambda r: -r[1])
    if len(runs) > 256:
        runs = runs[:256]
    log.debug("FNamePool 区块表候选：%d 个（最长连续 %s）"
              % (len(runs), [r[1] for r in runs][:8]))
    quick: List[Tuple[int, int, str, int, int, int]] = []
    for arr, _run_len in runs:
        for header in (0x10, 0x0, 0x8, 0x18, 0x20, 0x28):
            pool = arr - header
            if pool <= 0:
                continue
            for layout, extra in (("u16", 0), ("u16", 2), ("u32", 0), ("u32", 4)):
                for stride in (1, 2):
                    try:
                        score, _names = _score_pool(proc, pool, header, layout, extra, stride, QUICK_IDS)
                    except Exception:
                        continue
                    if score > 0:
                        quick.append((score, pool, layout, header, extra, stride))
    if not quick:
        return 0, {}
    quick.sort(key=lambda t: -t[0])
    best: Optional[Tuple[int, int, str, int, int, int, List[str]]] = None
    for _qscore, pool, layout, header, extra, stride in quick[:8]:
        try:
            score, names = _score_pool(proc, pool, header, layout, extra, stride, FULL_IDS)
        except Exception:
            continue
        if best is None or score > best[0]:
            best = (score, pool, layout, header, extra, stride, names)
    if best is None:
        return 0, {}
    score, pool, layout, header, extra, stride, names = best
    known = [n for n in names if n in KNOWN_NAMES]
    log.info("FNamePool @ 0x%X  header=0x%X layout=%s extra=%d stride=%d 得分=%d 已知名字命中=%d"
             % (pool, header, layout, extra, stride, score, len(known)))
    log.debug("池样例：%s" % ", ".join(names[:12]))
    return pool, {"header": header, "layout": layout, "extra": extra,
                  "stride": stride, "score": score}


# =========================================================================
# 2) FUObjectArray (GObjects)
# =========================================================================
def _make_ue(proc: Process, pool: int, pool_cfg: Dict[str, object], fname_off: int,
             prefetch: bool = True) -> Ue:
    ue = Ue(proc)
    ue.pool = pool
    ue.fname_header = int(pool_cfg["header"])
    ue.fname_layout = str(pool_cfg["layout"])
    ue.fname_extra = int(pool_cfg["extra"])
    ue.fname_stride = int(pool_cfg["stride"])
    ue.off.fname = fname_off
    if prefetch:
        ue.prefetch_blocks(limit=256, max_empty=4)
    return ue


def _looks_like_chunked_array(proc: Process, rmap: RegionMap, arr_addr: int,
                              objects_off: int, item_hint: int = 0x10) -> bool:
    """廉价先验：数组中存着一个 Chunk 指针，Chunk 里放着若干个可读的 UObject*。

    真正的 FUObjectArray 一定是“分块数组”：结构体里存的是 Chunk 指针，
    Chunk 里存的才是 UObject*。UObject 的前 0x40 字节里必然有 vtable / Class /
    Outer 这些指向已提交内存的指针。这个先验能把绝大多数 (mx,num) 假命中踢掉，
    免得对每个候选都做昂贵的名字解析。
    """
    chunk = proc.u64(arr_addr + objects_off)
    if not chunk or not rmap.contains(chunk):
        return False
    obj0 = proc.u64(chunk)
    if not obj0 or not rmap.contains(obj0):
        return False
    ptrs = 0
    for off in (0x00, 0x08, 0x10, 0x20, 0x28, 0x30):
        if rmap.contains(proc.u64(obj0 + off)):
            ptrs += 1
    if ptrs < 2:
        return False
    for item_size in (item_hint, 0x10, 0x18):
        good = 0
        for i in range(1, 4):
            if rmap.contains(proc.u64(chunk + i * item_size)):
                good += 1
        if good >= 2:
            return True
    return False


def _validate_objects(proc: Process, pool: int, pool_cfg: Dict[str, object],
                      arr_addr: int, objects_off: int, fname_off: int,
                      num_off: Optional[int] = None, klass_off: int = 0x10,
                      item_size: int = 0x10, ue: Optional[Ue] = None) -> int:
    """强结构自证：数组第 0 个元素必须是“类是 Class 的 UClass”。

    这是 UE 的硬性质：GObjects[0] 是 UObject 的默认实例，其 ClassPrivate 指向
    UClass 单例，而 UClass 自身的 ClassPrivate 又指回自己。能同时满足这两条的
    内存布局就是 FUObjectArray。
    """
    try:
        chunk = proc.u64(arr_addr + objects_off)
    except Exception:
        return -1
    if not chunk:
        return -1
    obj0 = proc.u64(chunk)
    if not obj0:
        return -1
    cls = proc.u64(obj0 + klass_off)
    if not cls:
        return -1
    cls_cls = proc.u64(cls + klass_off)
    if not cls_cls:
        return -1
    if ue is None:
        ue = _make_ue(proc, pool, pool_cfg, fname_off, prefetch=False)
    name_clscls = ue.name_of(cls_cls)
    name_cls = ue.name_of(cls)
    name_obj = ue.name_of(obj0)
    score = 0
    if name_clscls == "Class":
        score += 12
    elif name_clscls and name_clscls.endswith("Class"):
        score += 2
    if name_cls in KNOWN_NAMES:
        score += 4
    # GObjects 的头几个元素永远是 None / Object / Package 这类根对象
    if name_obj in ("None", "Object", "Package"):
        score += 6
    elif name_obj and all(32 <= ord(c) < 127 for c in name_obj):
        score += 2
    if score < 12:
        return score
    # 用给定的 item_size 抽查一批条目：元素大小对了，命中率会显著更高
    good = 0
    total = 0
    try:
        raw = proc.read(chunk, item_size * 32)
    except Exception:
        raw = b""
    for i in range(1, min(32, len(raw) // item_size)):
        o = struct.unpack_from("<Q", raw, i * item_size)[0]
        if not o:
            continue
        total += 1
        if ue.name_of(o):
            good += 1
    score += good
    if total and good < max(2, total // 3):
        score -= 6
    if num_off is not None:
        n = proc.u32(arr_addr + num_off)
        score += 3 if 500 <= n <= 8_000_000 else -3
    return score


def find_num_elements_candidates(proc: Process, spans: Sequence[Span], log,
                                 num_lo: int = 500,
                                 num_hi: int = 4_000_000) -> List[Tuple[int, int, int]]:
    """在可写区域里找 (MaxElements, NumElements) 这对 int32。

    FUObjectArray 一定含有一对“容量 / 当前数量”的 int32，数量在几千到几百万之间，
    容量不小于数量。这个组合在数据段里极其罕见，作为预筛可以把候选从几十万个
    指针位置压缩到几十个，然后再逐个做结构自证——比按特征码扫描稳得多。
    返回 [(NumElements 所在地址, MaxElements, NumElements)]。
    """
    out: List[Tuple[int, int, int]] = []
    for rbase, rsize in spans:
        data = proc.read(rbase, rsize)
        if not data:
            continue
        n = len(data) - 8
        for off in range(0, n, 4):
            num = struct.unpack_from("<i", data, off + 4)[0]
            if num < num_lo or num > num_hi:
                continue
            mx = struct.unpack_from("<i", data, off)[0]
            if mx < num or mx > num_hi * 4:
                continue
            out.append((rbase + off + 4, mx, num))
    log.debug("(MaxElements, NumElements) 候选：%d 个" % len(out))
    return out


# (Objects 字段偏移, NumElements 字段偏移) —— 相对数组起点
# 第一位是 UE4.23~UE5 的标准布局：FChunkedFixedUObjectArray
#   +0x00 Objects(FUObjectItem**)  +0x08 PreAllocatedObjects  +0x0C MaxElements
#   +0x10 NumElements  +0x14 MaxChunks  +0x18 NumChunks
# 放在 FUObjectArray 里时整体后移 0x10（前面有 GC 相关字段），于是变成 (0x10, 0x20)。
_OBJECTS_FIELDS: Tuple[int, ...] = (0x10, 0x00, 0x08, 0x18, 0x20, 0x28, 0x30)
_NUM_FIELDS: Tuple[int, ...] = (0x24, 0x1C, 0x14, 0x0C, 0x20, 0x28, 0x2C, 0x30, 0x34, 0x38, 0x40)
ARRAY_LAYOUTS: Tuple[Tuple[int, int], ...] = tuple(
    (o, n) for o in _OBJECTS_FIELDS for n in _NUM_FIELDS if n > o + 8
)
NAME_FIELD_OFFSETS: Tuple[int, ...] = (0x18, 0x10, 0x20, 0x14, 0x1C)
# FUObjectItem 的元素大小随引擎版本变化（0x10 / 0x18），必须自校准
ITEM_SIZES: Tuple[int, ...] = (0x10, 0x18, 0x20)


GOOD_SCORE = 30


def resolve_uobject_array(proc: Process, spans: Sequence[Span], pool: int,
                          pool_cfg: Dict[str, object], log,
                          rmap: Optional[RegionMap] = None) -> Tuple[int, Dict[str, object]]:
    """定位 FUObjectArray：预筛 -> 分块数组先验 -> 结构自证 -> 打分排序。

    性能要点：名字池只预取一次（各候选共用），候选按对象数量降序排列，
    一旦拿到高分就直接收工——正常情况第一次命中就是答案。
    """
    if rmap is None:
        rmap = RegionMap(list(proc.regions(include_image=True, include_mapped=True)))
    ue = _make_ue(proc, pool, pool_cfg, 0x18, prefetch=True)
    best: Optional[Tuple[int, int, int, int, int]] = None
    cands = find_num_elements_candidates(proc, spans, log)
    cands.sort(key=lambda c: -c[2])
    tried = 0
    for num_addr, _mx, _num in cands[:4000]:
        for objects_off, num_off in ARRAY_LAYOUTS:
            addr = num_addr - num_off
            if addr <= 0:
                continue
            if not _looks_like_chunked_array(proc, rmap, addr, objects_off):
                continue
            tried += 1
            for item_size in ITEM_SIZES:
                for fname_off in NAME_FIELD_OFFSETS:
                    ue.off.fname = fname_off
                    score = _validate_objects(proc, pool, pool_cfg, addr, objects_off,
                                              fname_off, num_off, item_size=item_size, ue=ue)
                    if score >= 12 and (best is None or score > best[0]):
                        best = (score, addr, objects_off, fname_off, item_size)
                if best and best[0] >= GOOD_SCORE:
                    break
            if best and best[0] >= GOOD_SCORE:
                break
        if best and best[0] >= GOOD_SCORE:
            break
    if best is None:
        log.debug("通过分块数组先验的候选：%d 个，但都没通过结构自证" % tried)
        return 0, {}
    score, addr, objects_off, fname_off, item_size = best
    log.info("FUObjectArray @ 0x%X  Objects字段=0x%X NamePrivate字段=0x%X 元素大小=0x%X 得分=%d"
             % (addr, objects_off, fname_off, item_size, score))
    return addr, {"objects_off": objects_off, "fname_off": fname_off,
                  "item_size": item_size, "score": score}


def find_globals(proc: Process, log) -> Tuple[int, int, UeOffsets]:
    """返回 (GObjects, GNames, 已校准的对象层偏移)。"""
    exe_base, exe_size = proc.main_module()
    log.info("主模块基址 0x%X 大小 0x%X" % (exe_base, exe_size))
    rmap = RegionMap(list(proc.regions(include_image=True, include_mapped=True)))
    log.debug("已提交可读区域：%d 段" % len(rmap))

    result: Tuple[int, int, UeOffsets] = (0, 0, UeOffsets())
    for scope in ("image", "any"):
        spans = spans_for(proc, exe_base, exe_size, scope)
        log.debug("扫描范围 %s：%d 段，共 %.1f MB"
                  % (scope, len(spans), sum(s for _b, s in spans) / 1048576.0))
        pool, pool_cfg = resolve_fname_pool(proc, spans, rmap, log)
        if not pool:
            log.debug("范围 %s 内没找到 FNamePool" % scope)
            continue
        gobjects, obj_cfg = resolve_uobject_array(proc, spans, pool, pool_cfg, log, rmap)
        if not gobjects:
            log.debug("范围 %s 内找到名字池但没找到对象数组" % scope)
            continue
        off = UeOffsets()
        off.objects = int(obj_cfg["objects_off"])
        off.fname = int(obj_cfg["fname_off"])
        off.item_size = int(obj_cfg.get("item_size") or 0x10)
        ue = Ue(proc, off)
        ue.attach(gobjects, pool)
        result = (gobjects, pool, off)
        break
    else:
        log.error("定位失败。可尝试：1) 确认游戏已进入主菜单/对局；2) --no-cache 重扫；"
                  "3) python chameleon_esp.py dump 落盘后用 tools/analyze_dump.py 离线分析")
    return result


# =========================================================================
# 3) 世界链
# =========================================================================
def _class_matches(ue: Ue, obj: int, needle: str) -> bool:
    cn = ue.class_name_of(obj)
    return bool(cn) and needle.lower() in cn.lower()


def find_objects_slow(ue: Ue, class_needle: str, limit: int = 64) -> List[int]:
    out: List[int] = []
    for _idx, obj in ue.iter_items():
        if _class_matches(ue, obj, class_needle):
            out.append(obj)
            if len(out) >= limit:
                break
    return out


def resolve_world_chain(ue: Ue, log) -> Dict[str, int]:
    """反推 World -> PersistentLevel / GameState / Actors 的字段偏移。"""
    result: Dict[str, int] = {}
    worlds = find_objects_slow(ue, "World", limit=8)
    pcs = find_objects_slow(ue, "PlayerController", limit=32)
    pss = find_objects_slow(ue, "PlayerState", limit=64)
    pawns = find_objects_slow(ue, "Pawn", limit=32)
    log.info("对象抽样：World=%d PlayerController=%d PlayerState=%d Pawn=%d"
             % (len(worlds), len(pcs), len(pss), len(pawns)))
    if not worlds:
        return result
    w = worlds[0]
    for off in range(0x20, 0x400, 8):
        v = ue.proc.u64(w + off)
        if not v:
            continue
        if "game_state" not in result and _class_matches(ue, v, "GameState"):
            result["game_state"] = off
        if "persistent_level" not in result and _class_matches(ue, v, "Level"):
            result["persistent_level"] = off
            for aoff in range(off + 8, off + 0x400, 8):
                raw = ue.proc.read(w + aoff, 24)
                if len(raw) < 24:
                    continue
                arr, cnt, cap = struct.unpack("<QQQ", raw)
                if not arr or not (0 < cnt <= cap <= 500000):
                    continue
                first = ue.proc.u64(arr)
                if first and ue.class_name_of(first):
                    result["level_actors"] = aoff
                    log.debug("Level->Actors 数组：World+0x%X（%d 个 Actor）" % (aoff, cnt))
                    break
    # PC -> Pawn
    if pcs and "pawn" not in result:
        votes: Dict[int, int] = {}
        for pc in pcs[:6]:
            raw = ue.proc.read(pc + 0x100, 0x600)
            for off in range(0, len(raw) - 8, 8):
                v = Q.unpack_from(raw, off)[0]
                if v and _class_matches(ue, v, "Pawn"):
                    votes[0x100 + off] = votes.get(0x100 + off, 0) + 1
        if votes:
            off, cnt = max(votes.items(), key=lambda kv: kv[1])
            result["pawn"] = off
            log.debug("PlayerController->Pawn：+0x%X（%d 票）" % (off, cnt))
    log.info("世界链结果：%s" % (", ".join("%s=0x%X" % kv for kv in sorted(result.items())) or "无"))
    return result


def resolve_location_offsets(ue: Ue, log) -> Dict[str, int]:
    """反推 RootComponent / RelativeLocation（找组件对象里的坐标字段）。"""
    result: Dict[str, int] = {}
    samples: List[int] = []
    for needle in ("FirstPersonCharacter", "Character", "Pawn"):
        samples = find_objects_slow(ue, needle, limit=16)
        if len(samples) >= 2:
            log.debug("坐标链样本：%s（%d 个）" % (needle, len(samples)))
            break
    if len(samples) < 2:
        log.debug("Actor 样本不足，跳过 RootComponent 反推")
        return result
    root_votes: Dict[int, int] = {}
    loc_votes: Dict[int, int] = {}
    for obj in samples[:8]:
        raw = ue.proc.read(obj + 0x30, 0x400)
        if not raw:
            continue
        for off in range(0, len(raw) - 8, 8):
            p = Q.unpack_from(raw, off)[0]
            if not (0x10000 < p < (1 << 47)):
                continue
            cn = ue.class_name_of(p)
            if not cn or not any(k in cn for k in ("Component", "Mesh")):
                continue
            vecs = [o for o in range(0x80, 0x400, 4)
                    if util.plausible_vec(ue.proc.vec3(p + o))]
            if not vecs:
                continue
            root_votes[0x30 + off] = root_votes.get(0x30 + off, 0) + 1
            loc_votes[vecs[0]] = loc_votes.get(vecs[0], 0) + 1
    if root_votes:
        off, cnt = max(root_votes.items(), key=lambda kv: kv[1])
        if cnt >= 2:
            result["root_component"] = off
    if loc_votes:
        off, cnt = max(loc_votes.items(), key=lambda kv: kv[1])
        if cnt >= 2:
            result["relative_location"] = off
    if result:
        log.info("坐标链结果：%s" % ", ".join("%s=0x%X" % kv for kv in sorted(result.items())))
    return result


def resolve_camera_offsets(ue: Ue, log) -> Dict[str, int]:
    """反推 PlayerCameraManager 里的 FMinimalViewInfo（Location / Rotation / FOV）。"""
    cams = find_objects_slow(ue, "PlayerCameraManager", limit=8)
    if not cams:
        log.debug("没有 PlayerCameraManager 实例（可能还没进对局），跳过相机校准")
        return {}
    best: Optional[Tuple[float, int, Tuple[float, float, float], Tuple[float, float, float]]] = None
    for cam in cams[:4]:
        raw = ue.proc.read(cam, 0x1400)
        if not raw:
            continue
        for off in range(0, len(raw) - 0x40, 4):
            x, y, z = struct.unpack_from("<3f", raw, off)
            if not util.plausible_vec((x, y, z), 100000.0):
                continue
            try:
                pitch, yaw, roll = struct.unpack_from("<3f", raw, off + 0x0C)
                fov = struct.unpack_from("<f", raw, off + 0x18)[0]
            except Exception:
                continue
            if not (-90.001 <= pitch <= 90.001):
                continue
            if not (-180.001 <= yaw <= 180.001 and -180.001 <= roll <= 180.001):
                continue
            if not (util.is_finite(fov) and 25.0 <= fov <= 150.0):
                continue
            score = abs(fov - 90.0)
            if best is None or score < best[0]:
                best = (score, off, (x, y, z), (pitch, yaw, roll))
    if not best:
        return {}
    _score, off, loc, rot = best
    log.info("相机 POV（f32 布局）：+0x%X 样例 loc=%s rot=%s"
             % (off, tuple(round(v) for v in loc), tuple(round(v, 1) for v in rot)))
    return {"pov": off, "camera_location": off, "camera_rotation": off + 0x0C,
            "camera_fov": off + 0x18, "camera_dtype": "f32"}


# =========================================================================
# 落盘 / 读盘
# =========================================================================
def save_offsets(path: str, data: Dict[str, object], source: str = "") -> None:
    db: Dict[str, object] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            db = {}
    fields = db.setdefault("fields", {})
    if isinstance(fields, dict):
        for k, v in data.items():
            if isinstance(v, bool):
                fields[k] = {"value": v, "source": source}
            elif isinstance(v, (int, float)):
                fields[k] = {"value": int(v), "source": source}
    db["updated"] = util.now()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_offsets(path: str) -> Dict[str, int]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        return {}
    out: Dict[str, int] = {}
    for k, v in (db.get("fields") or {}).items():
        if isinstance(v, dict) and "value" in v:
            out[k] = int(v["value"])
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = int(v)
    for k in ("uobject_array", "fname_pool"):
        if k in db:
            out[k] = int(db[k])
    return out
