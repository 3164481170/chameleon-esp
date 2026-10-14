"""定位诊断工具：当自动定位失败时，把候选线索全部打印出来。

用法：
    python tools/diagnose_offsets.py                    # 自动找游戏进程
    python tools/diagnose_offsets.py --pid 1234
    python tools/diagnose_offsets.py --deep             # 连结构自证过程一起打印

它做四件事：
    1) 打印模块基址、可读区域数量、堆区域数量；
    2) 列出所有 (MaxElements, NumElements) 候选，标注是否通过分块数组先验；
    3) 尝试定位 FNamePool，并打印池里前若干个名字（可直接肉眼确认对不对）；
    4) 若定位成功，打印对象总数、类名直方图 Top20。

定位失败时把这份输出贴出来，就能判断是脚本问题还是游戏结构变化。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdk import mem as memmod, resolver, util
from sdk.ue import Ue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--exe", default="PenguinHotel-Win64-Shipping.exe")
    ap.add_argument("--title", default="MECCHA CHAMELEON")
    ap.add_argument("--deep", action="store_true")
    args = ap.parse_args()

    log = util.Log("debug" if args.deep else "info")
    pid = args.pid
    if not pid:
        pid = memmod.find_pid_by_window(args.title) or 0
    if not pid:
        found = memmod.find_pids([args.exe])
        pid = found[0][0] if found else 0
    if not pid:
        print("找不到游戏进程")
        return 2

    proc = memmod.Process(pid).open()
    base, size = proc.main_module()
    regions = list(proc.regions(include_image=True, include_mapped=True))
    heap = [r for r in regions if 0x1000000000 < r[0] < 0x800000000000]
    print("PID=%d  主模块基址=0x%X  大小=%.1f MB" % (pid, base, size / 1048576.0))
    print("可读区域=%d 段，其中堆区=%d 段" % (len(regions), len(heap)))
    print("模块列表（前 12）：")
    for name, (mb, ms) in list(proc.modules.items())[:12]:
        print("  %-46s 0x%012X  %.1f MB" % (name, mb, ms / 1048576.0))

    rmap = resolver.RegionMap(regions)
    spans = resolver.spans_for(proc, base, size, "image")
    print("镜像可写段=%d 段（%.1f MB）"
          % (len(spans), sum(s for _b, s in spans) / 1048576.0))

    print()
    print("== FNamePool 定位 ==")
    pool, pool_cfg = resolver.resolve_fname_pool(proc, spans, rmap, log)
    if pool:
        ue = Ue(proc)
        ue.pool = pool
        ue.fname_header = int(pool_cfg["header"])
        ue.fname_layout = str(pool_cfg["layout"])
        ue.fname_extra = int(pool_cfg["extra"])
        ue.fname_stride = int(pool_cfg["stride"])
        ue.prefetch_blocks(limit=32, max_empty=4)
        print("池地址=0x%X  配置=%s  前 24 个名字：" % (pool, pool_cfg))
        for i in range(24):
            print("   id=%-4d %r" % (i, ue.name_at(i)))
    else:
        print("  未找到 FNamePool")

    print()
    print("== (MaxElements, NumElements) 候选（前 20，按数量降序）==")
    cands = resolver.find_num_elements_candidates(proc, spans, log)
    cands.sort(key=lambda c: -c[2])
    print("  候选总数：%d" % len(cands))
    passed = []
    for num_addr, mx, num in cands[:20]:
        marks = []
        for objects_off, num_off in resolver.ARRAY_LAYOUTS:
            addr = num_addr - num_off
            if addr <= 0:
                continue
            if resolver._looks_like_chunked_array(proc, rmap, addr, objects_off):
                marks.append("obj=+0x%X num=+0x%X" % (objects_off, num_off))
        if marks:
            passed.append((num_addr, mx, num, marks[0]))
        print("  num_addr=0x%X max=%d num=%d %s"
              % (num_addr, mx, num, ("通过先验：" + "; ".join(marks[:2])) if marks else ""))
    print("  通过分块数组先验的候选：%d 个" % len(passed))

    print()
    print("== FUObjectArray 结构自证 ==")
    if pool:
        gobjects, cfg = resolver.resolve_uobject_array(proc, spans, pool, pool_cfg, log, rmap)
        if gobjects:
            from sdk.ue import UeOffsets
            off = UeOffsets()
            off.objects = int(cfg["objects_off"])
            off.fname = int(cfg["fname_off"])
            off.item_size = int(cfg.get("item_size") or 0x10)
            ue = Ue(proc, off)
            try:
                ue.attach(gobjects, pool)
                print("  对象总数：%d" % ue.num())
                hist: Dict[str, int] = {}
                for _i, obj in ue.iter_items(max_objects=200000):
                    cn = ue.class_name_of(obj)
                    if cn:
                        hist[cn] = hist.get(cn, 0) + 1
                print("  类名直方图 Top 20：")
                for cn, cnt in sorted(hist.items(), key=lambda kv: -kv[1])[:20]:
                    print("    %-52s %d" % (cn, cnt))
                keys = [k for k in hist if any(s in k for s in
                        ("Player", "Pawn", "Character", "World", "GameState", "CameraManager"))]
                print("  与玩家/世界相关的类：")
                for cn in sorted(keys)[:30]:
                    print("    %-52s %d" % (cn, hist[cn]))
            except Exception as exc:
                print("  attach 失败：%r" % exc)
        else:
            print("  结构自证未通过")
    proc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
