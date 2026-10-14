"""进程内存镜像落盘（离线分析 / 自动校准用）。

把目标进程加载的主模块按内存布局写到本地文件，之后可以：
1) 用 tools/analyze_ue.py 离线分析 GObjects 结构与类名；
2) 用 tools/find_offsets.py 批量验证候选偏移；
3) 备份一份用于对比游戏更新前后的变化。
"""

from __future__ import annotations

from typing import Dict, Tuple

from . import mem as memmod


def dump_module(proc: memmod.Process, module_name: str, out_path: str,
                max_bytes: int = 0) -> Tuple[str, int, Dict[str, Tuple[int, int]]]:
    """把模块的已提交可读区域按内存地址顺序写入 out_path。

    返回 (路径, 写入字节数, 区域表)。区域表可用于把文件偏移换算回内存 RVA。
    """
    base, size = proc.module(module_name)
    written = 0
    regions: Dict[str, Tuple[int, int]] = {}
    with open(out_path, "wb") as f:
        for rbase, rsize, prot in proc.regions(include_image=True, include_mapped=False):
            if rbase + rsize <= base or rbase >= base + size:
                continue
            off = 0
            while off < rsize:
                chunk = min(1 << 20, rsize - off)
                data = proc.read(rbase + off, chunk)
                if data:
                    f.write(data)
                    regions["%X" % (rbase + off)] = (written, len(data))
                    written += len(data)
                off += chunk
            if max_bytes and written >= max_bytes:
                break
    return out_path, written, regions
