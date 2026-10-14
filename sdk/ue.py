"""UE5 运行期对象模型：FUObjectArray / FNamePool / UObject 访问。

不依赖任何 UE 头文件，全部通过内存读取实现。所有偏移都在 UeOffsets 里，
由 resolver 在运行期自动校准（游戏更新后不用重新编译）。

关键结构（UE5 x64 常见布局，均为默认值，可被 resolver 覆盖）：
    UObject   : +0x00 vtable, +0x08 ObjectFlags, +0x0C InternalIndex,
                +0x10 ClassPrivate, +0x18 NamePrivate(FName, 8 字节), +0x20 OuterPrivate
    FUObjectItem : +0x00 UObject*, +0x08 Flags(int32), +0x0C ClusterRootIndex,
                   +0x10 SerialNumber
    FNamePool : +0x00 Lock/SRWLock(8 字节), +0x08 CurrentBlock, +0x0C CurrentByteCursor,
                +0x10 Blocks[8192]，条目地址 = Blocks[block] + (id & 0xFFFF)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from . import util
from .mem import Process

MASK32 = 0xFFFFFFFF


@dataclass
class UeOffsets:
    """全部相对偏移（十进制/十六进制等价，直接写 hex 更直观）。"""

    # --- 世界链 ---
    uworld: int = 0x0
    game_instance: int = 0x0
    persistent_level: int = 0x0
    local_players: int = 0x0
    game_state: int = 0x0
    player_array: int = 0x0
    player_controller: int = 0x0
    player_state: int = 0x0
    pawn_private: int = 0x0

    # --- 坐标链 ---
    root_component: int = 0x0
    relative_location: int = 0x0
    component_to_world: int = 0x0

    # --- 视角链 ---
    player_camera_manager: int = 0x0
    camera_cache: int = 0x0
    camera_location: int = 0x0
    fov: int = 0x0
    pov: int = 0x0

    # --- 通用 ---
    fname: int = 0x18
    internal_index: int = 0x0C
    class_private: int = 0x10
    outer_private: int = 0x20
    objects: int = 0x20
    num: int = 0x28

    # --- 解释方式 ---
    chunked: bool = True
    item_size: int = 16
    fname_size: int = 8
    vec_is_f64: bool = False
    uworld_is_pointer: bool = True

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "UeOffsets":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {}
        for k, v in (data or {}).items():
            if k in known and v is not None:
                clean[k] = v
        return cls(**clean)  # type: ignore[arg-type]


class Ue:
    CHUNK_ITEMS = 65536

    def __init__(self, proc: Process, offsets: Optional[UeOffsets] = None, log=None):
        self.proc = proc
        self.off = offsets or UeOffsets()
        self.log = log or util.Log()
        self.uobject_array = 0
        self.pool = 0
        self.fname_header = 0x8
        self.fname_layout = "u16"      # u16 / u32
        self.fname_extra = 0           # 头之后到字符串的额外字节
        self.fname_stride = 1          # id 单位：1=字节游标（大小写敏感池），2=条目序号
        self.fname_wide_bit = True
        self._name_cache: Dict[int, str] = {}
        self._obj_name_cache: Dict[int, str] = {}
        self._class_cache: Dict[int, int] = {}
        self._block_ptr: Dict[int, int] = {}
        self._block_data: Dict[int, bytes] = {}
        self._stats = {"read": 0, "fail": 0}

    # =====================================================================
    # 绑定与校准
    # =====================================================================
    def attach(self, uobject_array: int, fname_pool: int) -> None:
        self.uobject_array = uobject_array
        self.pool = fname_pool
        self._calibrate_fname()
        self.prefetch_blocks()
        if not self._calibrate_objects():
            raise RuntimeError("对象数组结构校准失败：地址 0x%X 不是有效的 FUObjectArray" % uobject_array)

    # --- 名字块预取 -------------------------------------------------------
    def prefetch_blocks(self, limit: int = 512, block_size: int = 65536,
                        max_empty: int = 8) -> int:
        """把 FNamePool 的每个块整块读进内存。

        FName 条目按 64KB 块分配，一个块里通常有几百到几千个名字。整块读一次，
        后面解析任何名字都变成纯内存操作——遍历几百万个 UObject 也能秒级完成。
        """
        self._block_ptr.clear()
        self._block_data.clear()
        empty = 0
        for b in range(limit):
            ptr = self.proc.u64(self.pool + self.fname_header + b * 8)
            if not ptr:
                empty += 1
                if empty >= max_empty:
                    break
                continue
            data = self.proc.read(ptr, block_size)
            if data:
                self._block_ptr[b] = ptr
                self._block_data[b] = data
        return len(self._block_data)

    # --- FNamePool -------------------------------------------------------
    def _name_probe_score(self, header: int, layout: str, extra: int, stride: int) -> int:
        """对一批 FName id 试读，返回能读成可打印字符串的数量。"""
        ok = 0
        total = 0
        for block in range(2):
            for cursor in range(0, 0x6000, 0x40):
                idx = (block << 16) + cursor
                s = self._read_name_raw(idx, header, layout, extra, stride)
                total += 1
                if s and 1 <= len(s) <= 96 and all(32 <= ord(ch) < 127 for ch in s):
                    ok += 1
        return ok

    def _read_name_raw(self, name_id: int, header: int, layout: str, extra: int,
                       stride: int, max_len: int = 96) -> str:
        block = (name_id >> 16) & 0xFFFF
        cursor = name_id & 0xFFFF
        if stride == 0:
            return ""
        head_len = 2 if layout == "u16" else 4
        # 1) 优先走整块缓存（attach 之后一直有效）
        blob = self._block_data.get(block)
        if blob is not None:
            entry = cursor * stride
            if stride == 2:
                entry = (entry >> 1) * 2
            if entry + extra + head_len > len(blob):
                return ""
            if layout == "u16":
                h = struct.unpack_from("<H", blob, entry + extra)[0]
                length = (h >> 1) & 0x7FFF
            else:
                h = struct.unpack_from("<I", blob, entry + extra)[0]
                length = (h >> 1) & 0x7FFFFFFF
            wide = bool(h & 1)
            if length <= 0 or length > max_len:
                return ""
            start = entry + extra + head_len
            end = start + length * (2 if wide else 1)
            if end > len(blob):
                return ""
            raw = blob[start:end]
            return raw.decode("utf-16-le", "replace") if wide else raw.decode("utf-8", "replace")
        # 2) 未缓存：直接按地址读（校准阶段用）
        block_ptr = self._block_ptr.get(block) or self.proc.u64(self.pool + header + block * 8)
        if not block_ptr:
            return ""
        entry_addr = block_ptr + cursor * stride
        head = self.proc.read(entry_addr, extra + head_len)
        if len(head) < extra + head_len:
            return ""
        if layout == "u16":
            h = struct.unpack_from("<H", head, extra)[0]
            length = (h >> 1) & 0x7FFF
        else:
            h = struct.unpack_from("<I", head, extra)[0]
            length = (h >> 1) & 0x7FFFFFFF
        wide = bool(h & 1)
        if length <= 0 or length > max_len:
            return ""
        data = self.proc.read(entry_addr + extra + head_len, length * (2 if wide else 1))
        if not data:
            return ""
        return data.decode("utf-16-le", "replace") if wide else data.decode("utf-8", "replace")

    def _calibrate_fname(self) -> None:
        best = None
        for header in (0x8, 0x10, 0x0, 0x4, 0x18):
            for layout, extra in (("u16", 0), ("u16", 2), ("u32", 0), ("u32", 4)):
                for stride in (1, 2):
                    score = self._name_probe_score(header, layout, extra, stride)
                    if score and (best is None or score > best[0]):
                        best = (score, header, layout, extra, stride)
        if not best:
            self.log.error("FNamePool 校准失败：pool=0x%X 读不出任何条目，检查 AOB 特征码是否过期" % self.pool)
            return
        score, header, layout, extra, stride = best
        self.fname_header, self.fname_layout = header, layout
        self.fname_extra, self.fname_stride = extra, stride
        self._name_cache.clear()
        sample = self.name_at(1)
        self.log.info("FNamePool 校准：header=0x%X layout=%s extra=%d stride=%d 命中=%d 样例=%r"
                      % (header, layout, extra, stride, score, sample))

    # --- FUObjectArray ---------------------------------------------------
    def _candidate_objects_cfg(self) -> List[Tuple[int, int, bool]]:
        """(chunked 数组字段偏移, NumElements 偏移, 是否分块) 候选。"""
        base = self.off.objects
        out: List[Tuple[int, int, bool]] = []
        for items in (base, base + 8, base - 8, base + 0x10, 0x20, 0x18, 0x28):
            if items < 0:
                continue
            for num in (0x28, 0x2C, 0x24, 0x30, items + 8, items + 4, 0x38):
                if num <= items:
                    continue
                out.append((items, num, True))
        out.append((base, 0, False))
        seen = set()
        uniq = []
        for cfg in out:
            if cfg not in seen:
                seen.add(cfg)
                uniq.append(cfg)
        return uniq

    def _score_objects_cfg(self, items: int, num: int, chunked: bool,
                           item_size: int = 0x10) -> int:
        """按 (字段偏移, 元素大小) 组合打分：能解析出合法类名的条目占比越高越可信。"""
        if item_size <= 0:
            return -1
        if chunked:
            chunk0 = self.proc.u64(self.uobject_array + items)
            if not chunk0:
                return -1
            raw = self.proc.read(chunk0, item_size * 96)
        else:
            raw = self.proc.read(self.uobject_array + items, item_size * 96)
        if not raw or len(raw) < item_size * 8:
            return -1
        n = len(raw) // item_size
        good = 0
        valid = 0
        for i in range(min(96, n)):
            obj = struct.unpack_from("<Q", raw, i * item_size)[0]
            if not obj:
                continue
            cls = self.proc.u64(obj + self.off.class_private)
            if not cls:
                continue
            cname = self.name_at(self._fname_id(cls))
            if not cname:
                continue
            valid += 1
            if cname.endswith("Class") or cname in ("Class", "Function", "ScriptStruct", "Package"):
                good += 3
            if self.name_at(self._fname_id(obj)):
                good += 2
        if valid < max(2, n // 4):
            return -1
        return good

    def _calibrate_objects(self) -> bool:
        """用元素大小 + 字段偏移的组合搜索，选出能真正遍历出对象的那一组。"""
        best = None
        for items, num, chunked in self._candidate_objects_cfg():
            for item_size in (self.off.item_size, 0x10, 0x18, 0x20):
                if item_size <= 0:
                    continue
                try:
                    score = self._score_objects_cfg(items, num, chunked, item_size)
                except Exception:
                    continue
                if score > 0 and (best is None or score > best[0]):
                    best = (score, items, num, chunked, item_size)
        if not best:
            self.log.error("FUObjectArray 校准失败：uobject_array=0x%X 无法解析对象数组" % self.uobject_array)
            return False
        score, items, num, chunked, item_size = best
        self.off.objects, self.off.num, self.off.chunked = items, num, chunked
        self.off.item_size = item_size
        self.log.info("FUObjectArray 校准：items=0x%X num=0x%X item=0x%X chunked=%s 得分=%d NumElements=%d"
                      % (items, num, item_size, chunked, score, self.num()))
        return self.num() > 0

    # =====================================================================
    # 名字
    # =====================================================================
    def _fname_id(self, obj: int) -> int:
        if self.off.fname_size == 8:
            return self.proc.u64(obj + self.off.fname) & MASK32
        return self.proc.u32(obj + self.off.fname) & MASK32

    def name_at(self, name_id: int, allow_cache: bool = True) -> str:
        if allow_cache:
            hit = self._name_cache.get(name_id)
            if hit is not None:
                return hit
        s = self._read_name_raw(name_id, self.fname_header, self.fname_layout,
                                self.fname_extra, self.fname_stride)
        if allow_cache and s:
            self._name_cache[name_id] = s
        return s

    def name_of(self, obj: int) -> str:
        if not obj:
            return ""
        hit = self._obj_name_cache.get(obj)
        if hit is not None:
            return hit
        s = self.name_at(self._fname_id(obj))
        if s:
            self._obj_name_cache[obj] = s
        return s

    def class_of(self, obj: int) -> int:
        hit = self._class_cache.get(obj)
        if hit is not None:
            return hit
        cls = self.proc.u64(obj + self.off.class_private)
        if cls:
            self._class_cache[obj] = cls
        return cls

    def class_name_of(self, obj: int) -> str:
        return self.name_of(self.class_of(obj))

    def outer_of(self, obj: int) -> int:
        return self.proc.u64(obj + self.off.outer_private)

    def full_name(self, obj: int, depth: int = 6) -> str:
        parts: List[str] = []
        cur = obj
        while cur and len(parts) < depth:
            n = self.name_of(cur)
            if not n:
                break
            parts.append(n)
            cur = self.outer_of(cur)
        return ".".join(reversed(parts))

    def is_a(self, obj: int, class_name: str) -> bool:
        """沿 Class->Super 链判定类名（大小写不敏感的子串匹配）。"""
        needle = class_name.lower()
        cls = self.class_of(obj)
        seen = 0
        while cls and seen < 24:
            cn = self.name_of(cls).lower()
            if needle == cn or needle in cn:
                return True
            cls = self.proc.u64(cls + self.off.outer_private)
            if cls == self.proc.u64(self.class_of(cls) + 0x0) :
                break
            seen += 1
        return False

    # =====================================================================
    # 对象遍历
    # =====================================================================
    def num(self) -> int:
        n = self.proc.u32(self.uobject_array + self.off.num)
        if 0 < n < 8_000_000:
            return n
        return 0

    def item_ptr(self, index: int) -> int:
        if self.off.chunked:
            chunk = self.proc.u64(self.uobject_array + self.off.objects
                                  + 8 * (index // self.CHUNK_ITEMS))
            if not chunk:
                return 0
            return chunk + self.off.item_size * (index % self.CHUNK_ITEMS)
        return self.uobject_array + self.off.objects + self.off.item_size * index

    def object_at(self, index: int) -> int:
        p = self.item_ptr(index)
        return self.proc.u64(p) if p else 0

    def iter_items(self, max_objects: Optional[int] = None,
                   batch: bool = True) -> Iterator[Tuple[int, int]]:
        """遍历 (index, UObject*)。batch=True 时按 chunk 批量读取，速度提升一个量级。"""
        total = self.num()
        if max_objects:
            total = min(total, max_objects)
        if total <= 0:
            return
        if not batch or not self.off.chunked:
            for i in range(total):
                obj = self.object_at(i)
                if obj:
                    yield i, obj
            return
        per_chunk = self.CHUNK_ITEMS
        num_chunks = (total + per_chunk - 1) // per_chunk
        chunk_table = self.proc.read(self.uobject_array + self.off.objects, 8 * num_chunks)
        for c in range(num_chunks):
            if len(chunk_table) < 8 * (c + 1):
                break
            chunk_ptr = struct.unpack_from("<Q", chunk_table, 8 * c)[0]
            if not chunk_ptr:
                continue
            count = min(per_chunk, total - c * per_chunk)
            raw = self.proc.read(chunk_ptr, count * self.off.item_size)
            if not raw:
                continue
            for i in range(len(raw) // self.off.item_size):
                obj = struct.unpack_from("<Q", raw, i * self.off.item_size)[0]
                if obj:
                    yield c * per_chunk + i, obj

    def find_objects_by_class(self, class_substrings: Sequence[str],
                              max_objects: Optional[int] = None,
                              name_substrings: Optional[Sequence[str]] = None) -> List[Tuple[int, int, str, str]]:
        """按类名/对象名子串筛选，返回 [(index, obj, class_name, object_name)]。"""
        cls_needles = [c.lower() for c in (class_substrings or [])]
        name_needles = [n.lower() for n in (name_substrings or [])]
        out: List[Tuple[int, int, str, str]] = []
        for idx, obj in self.iter_items(max_objects=max_objects):
            cn = self.class_name_of(obj)
            if cls_needles:
                low = cn.lower()
                if not any(n in low for n in cls_needles):
                    continue
            on = self.name_of(obj)
            if name_needles:
                low = on.lower()
                if not any(n in low for n in name_needles):
                    continue
            out.append((idx, obj, cn, on))
        return out

    def read_vec3(self, address: int) -> Optional[Tuple[float, float, float]]:
        if self.off.vec_is_f64:
            return self.proc.vec3(address, "f64")
        return self.proc.vec3(address, "f32")

    def stats(self) -> Dict[str, object]:
        return {
            "objects": self.num(),
            "uobject_array": "0x%X" % self.uobject_array,
            "fname_pool": "0x%X" % self.pool,
            "fname_cfg": "%s hdr=0x%X extra=%d stride=%d" % (
                self.fname_layout, self.fname_header, self.fname_extra, self.fname_stride),
            "names_cached": len(self._name_cache),
        }

    # =====================================================================
    # 自检
    # =====================================================================
    def self_check(self, sample: int = 4000) -> Dict[str, object]:
        """返回对象/名字解析成功率与若干关键类是否可见，用于端到端验证。"""
        total = 0
        named = 0
        classed = 0
        classes: Dict[str, int] = {}
        n = self.num()
        step = max(1, n // sample) if n else 1
        for i in range(0, n, step):
            obj = self.object_at(i)
            if not obj:
                continue
            total += 1
            nm = self.name_of(obj)
            if nm:
                named += 1
            cn = self.class_name_of(obj)
            if cn:
                classed += 1
                classes[cn] = classes.get(cn, 0) + 1
        top = sorted(classes.items(), key=lambda kv: -kv[1])[:15]
        interesting = {k: v for k, v in classes.items() if any(
            s in k for s in ("PlayerState", "Pawn", "Controller", "GameState", "World", "Character"))}
        return {
            "objects_total": n,
            "sampled": total,
            "named": named,
            "classed": classed,
            "top_classes": top,
            "interesting": sorted(interesting.items(), key=lambda kv: -kv[1])[:20],
        }


def resolve_chains(ue: Ue, module_base: int) -> Dict[str, int]:
    """从已校准的 UObject 数组里找 World / GameState，用作世界链入口。"""
    out: Dict[str, int] = {}
    for want in ("World", "GameStateBase", "GameState", "PlayerController", "PlayerState", "Pawn"):
        found = ue.find_objects_by_class([want], max_objects=None)
        if found:
            idx, obj, cn, on = found[0]
            out[want] = obj
    return out
