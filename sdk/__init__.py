"""MECCHA CHAMELEON ESP 插件 SDK。

模块：
    util     日志 / 小工具
    mem      进程内存读写、模块枚举、AOB 扫描（ctypes）
    dump     PE 文件内存镜像（离线自检用）
    ue       UE5 对象模型：FUObjectArray / FNamePool / UObject 访问
    resolver 运行时偏移自动发现与校准
    filters  类名 -> 类别（hider/hunter/other）规则
    overlay  Win32 GDI 无边框穿透叠加层 + 绘制器
    bridge   可选本地 HTTP 数据桥（给未来的 Web 工具用）
    config   配置读写
    app      主程序（命令行 + 渲染循环 + 读取线程）
"""

__version__ = "0.1.0"
__all__ = ["util", "mem", "dump", "ue", "resolver", "filters", "overlay", "bridge", "config", "app"]
