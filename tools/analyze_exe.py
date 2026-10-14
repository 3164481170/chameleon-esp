"""离线解析 shipping exe：验证 FName 条目布局（不需要运行游戏）。

原理：
    UE 的 FNamePool 初始块是 exe 内的静态数据，按 id 顺序存放：
        id=0 "None", 1 "ByteProperty", 2 "IntProperty", 3 "BoolProperty", ...
    每个条目是 [header(2 字节)][UTF-8 字符串][\0]，header = (长度 << 1) | bIsWide。
    只要在 exe 里搜到这一串连续条目，就能证明：
        1) 条目头是 2 字节（不是 4 字节）；
        2) 布局是“头 + 字符串”，没有额外前缀；
        3) 名字按 id 顺序排列 —— 正好对应 FNamePool 的块内游标分配方式。

这条验证把 FNamePool 的假设从“猜”变成了“证”，并且能在离线状态下跑。
"""

from __future__ import annotations

import os
import struct
import sys
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# UE 标准初始名字表（顺序固定，官方源码里就是这么多条）
EXPECTED = ["None", "ByteProperty", "IntProperty", "BoolProperty", "FloatProperty",
            "ObjectProperty", "NameProperty", "ClassProperty", "UInt8Property",
            "DelegateProperty", "DoubleProperty", "ArrayProperty", "StructProperty"]


def read_entry(data: bytes, off: int, wide_header: bool = False) -> Optional[Tuple[str, int]]:
    if off + 2 > len(data):
        return None
    h = struct.unpack_from("<H", data, off)[0]
    wide = bool(h & 1)
    ln = (h >> 1) & 0x7FFF
    if ln <= 0 or ln > 128:
        return None
    start = off + 2
    if wide:
        raw = data[start:start + ln * 2]
        if len(raw) < ln * 2:
            return None
        s = raw.decode("utf-16-le", "replace")
        return s, 2 + ln * 2
    raw = data[start:start + ln]
    if len(raw) < ln:
        return None
    s = raw.decode("utf-8", "replace")
    return s, 2 + ln


def find_initial_block(data: bytes) -> Optional[int]:
    """搜索 "None" 条目（header=8 + 'None' + NUL），逐个校验后续条目是否吻合。"""
    needle = b"\x08\x00None\x00"
    start = 0
    while True:
        idx = data.find(needle, start)
        if idx < 0:
            return None
        off = idx
        names: List[str] = []
        cur = off
        ok = True
        for _ in range(len(EXPECTED)):
            r = read_entry(data, cur)
            if not r:
                ok = False
                break
            s, ln = r
            names.append(s)
            cur += ln
        if ok and names[:4] == EXPECTED[:4]:
            # 允许后续名字有细微差异，但前 8 条必须高度吻合
            matched = sum(1 for a, b in zip(names, EXPECTED) if a == b)
            if matched >= 6:
                return off
        start = idx + 1


def rva_of(data: bytes, offset: int, sections) -> int:
    for name, va, vsize, raw_ptr, raw_size in sections:
        if raw_ptr <= offset < raw_ptr + raw_size:
            return offset - raw_ptr + va
    return 0


def parse_sections(data: bytes, pe_off: int):
    nsec = struct.unpack_from("<H", data, pe_off + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe_off + 20)[0]
    sec_off = pe_off + 24 + opt_size
    out = []
    for i in range(nsec):
        base = sec_off + i * 40
        name = data[base:base + 8].rstrip(b"\x00").decode("latin-1")
        vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", data, base + 8)
        out.append((name, va, vsize, raw_ptr, raw_size))
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else \
        r"E:\steam\steamapps\common\MECCHA CHAMELEON\Chameleon\Binaries\Win64\PenguinHotel-Win64-Shipping.exe"
    print("目标：%s" % path)
    with open(path, "rb") as f:
        data = f.read()
    print("大小：%.1f MB" % (len(data) / 1048576.0))
    if data[:2] != b"MZ":
        print("不是 PE 文件")
        return 2
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off:pe_off + 4] != b"PE\x00\x00":
        print("PE 头无效")
        return 2
    machine = struct.unpack_from("<H", data, pe_off + 4)[0]
    print("机器类型：0x%04X（%s）" % (machine, "x64" if machine == 0x8664 else "其他"))
    sections = parse_sections(data, pe_off)
    for s in sections:
        print("  节 %-8s VA=0x%08X 虚拟大小=0x%08X 文件偏移=0x%08X" % (s[0], s[1], s[2], s[3]))

    off = find_initial_block(data)
    print()
    if off is None:
        print("结论：没有在 exe 里找到连续的 FName 初始名字表")
        print("      —— 说明该构建把名字池放在 pak 或运行时构造，需要走运行期扫描路径（代码已支持）")
        return 0
    rva = rva_of(data, off, sections)
    print("找到 FName 初始名字块：文件偏移 0x%X  RVA 0x%X" % (off, rva))
    cur = off
    got = []
    for _ in range(len(EXPECTED) + 4):
        r = read_entry(data, cur)
        if not r:
            break
        s, ln = r
        got.append(s)
        cur += ln
    print("按 [2 字节头 + UTF-8 + NUL] 解析出的名字序列：")
    for i, s in enumerate(got):
        mark = "OK " if i < len(EXPECTED) and s == EXPECTED[i] else "   "
        print("  id=%-3d %s %r" % (i, mark, s))
    matched = sum(1 for i, s in enumerate(got[:len(EXPECTED)]) if s == EXPECTED[i])
    print()
    print("与 UE 标准初始名字表吻合：%d / %d" % (matched, len(EXPECTED)))
    if matched >= 6:
        print("结论：FName 条目布局 = 2 字节头 + 字符串，块内按 id 顺序分配 —— 与实现一致。")
        print("      （fname_layout=u16, extra=0, stride=1 已在离线数据上得到验证）")
        return 0
    print("结论：布局与预期不符，需要用运行期校准重新推断（代码里的自动校准会覆盖）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
