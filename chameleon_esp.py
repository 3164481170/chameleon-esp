"""MECCHA CHAMELEON 透视（ESP）插件主程序。

子命令：
    probe     离线探测：定位 GObjects/GNames，校准结构，dump 类名统计（不画东西）
    dump      把主模块内存镜像落盘，供离线分析
    esp       启动叠加层，实时显示目标框/距离/雷达
    selftest  纯数学自检（不需要游戏进程），验证投影是否正确

常用：
    python chameleon_esp.py esp --fps 144
    python chameleon_esp.py probe --report reports/classes.md
    python chameleon_esp.py esp --mode window --no-radar
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import struct
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

def app_dir() -> str:
    """程序所在目录。打包成 exe 时是 exe 所在目录，源码运行时是脚本所在目录。

    配置、偏移缓存、报告都写在这里，而不是 PyInstaller 的临时解包目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from sdk import autoloc, config as config_mod, dump as dump_mod, filters, mem as memmod
from sdk import player as player_mod, resolver, util
from sdk.esp import Scanner
from sdk.filters import Filter, HIDER, HUNTER, OTHER, DUMMY
from sdk.overlay import Overlay, Renderer
from sdk.ue import Ue, UeOffsets

GAME_DEFAULT = {
    "exe": "PenguinHotel-Win64-Shipping.exe",
    "title": "MECCHA CHAMELEON",
}

user32 = ctypes.WinDLL("user32", use_last_error=True)


def elevate_if_needed() -> None:
    """--elevate：主动提权重启。默认不提权，只在真正被拒绝时按需弹 UAC。"""
    if "--elevate" not in sys.argv:
        return
    if memmod.is_admin():
        return
    if memmod.relaunch_elevated():
        sys.exit(0)


class Keys:
    """全局热键轮询（GetAsyncKeyState），带边沿检测。"""

    def __init__(self, mapping: Dict[str, str]):
        self.map = {name: util.parse_vk(vk) for name, vk in mapping.items()}
        self.state: Dict[str, bool] = {k: False for k in self.map}

    def pressed(self, name: str) -> bool:
        vk = self.map.get(name) or 0
        if not vk:
            return False
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        prev = self.state.get(name, False)
        self.state[name] = down
        return down and not prev


def open_process(cfg: Dict, log, allow_elevate: bool = True) -> memmod.Process:
    pid = int(cfg.get("pid") or 0)
    if not pid:
        title = cfg.get("window_title") or ""
        if title:
            pid = memmod.find_pid_by_window(title) or 0
        if not pid:
            found = memmod.find_pids([cfg.get("exe") or GAME_DEFAULT["exe"]])
            if found:
                pid = found[0][0]
    if not pid:
        raise SystemExit("找不到游戏进程，先启动游戏，或用 --pid 指定进程号")
    try:
        proc = memmod.Process(pid).open()
    except memmod.MemoryError_ as exc:
        # 权限不足时自动请求一次 UAC 提升，而不是让用户自己去“以管理员身份运行”
        if allow_elevate and not memmod.is_admin() and ("错误码 5" in str(exc) or "denied" in str(exc).lower()):
            log.warn("打开进程被拒绝（权限不足），正在请求管理员权限重启…")
            if memmod.relaunch_elevated():
                raise SystemExit(0)
            log.error("提权被取消或失败；请手动右键“以管理员身份运行”")
        raise
    log.info("已打开进程 PID=%d，模块 %d 个%s"
             % (pid, len(proc.modules), "（管理员）" if memmod.is_admin() else ""))
    return proc


def init_ue(proc, log, offs_path: str, no_cache: bool = False) -> Tuple[Ue, Dict[str, int]]:
    """定位 GObjects/GNames 并校准结构；缓存命中时跳过全量扫描。"""
    cached: Dict[str, int] = {}
    if not no_cache:
        cached = resolver.load_offsets(offs_path)

    def usable(cand: Ue) -> bool:
        st = cand.self_check(sample=150)
        log.debug("缓存自检：对象 %s 命名 %s 分类 %s"
                  % (st["objects_total"], st["named"], st["classed"]))
        return bool(st["objects_total"]) and st["named"] > 10 and st["classed"] > 5

    gobjects = int(cached.get("uobject_array") or 0)
    pool = int(cached.get("fname_pool") or 0)
    if gobjects and pool:
        cand = Ue(proc, UeOffsets.from_dict(cached))
        try:
            cand.attach(gobjects, pool)
        except Exception as exc:
            log.debug("缓存地址 attach 失败：%s" % exc)
        else:
            if usable(cand):
                log.info("复用上次缓存的 GObjects/GNames（0x%X / 0x%X）" % (gobjects, pool))
                return cand, cached
        log.warn("缓存地址已失效，重新扫描")

    gobjects, pool, offs = resolver.find_globals(proc, log)
    if not gobjects:
        raise SystemExit("定位失败：请确认游戏已进入主菜单或对局；"
                         "仍失败可先跑 python chameleon_esp.py dump 落盘，"
                         "再用 tools/analyze_dump.py 离线分析。")
    ue = Ue(proc, offs)
    ue.attach(gobjects, pool)
    merged: Dict[str, int] = dict(cached)
    merged.update(offs.to_dict())
    merged["uobject_array"] = gobjects
    merged["fname_pool"] = pool
    resolver.save_offsets(offs_path, {"uobject_array": gobjects, "fname_pool": pool},
                          source="find_globals")
    return ue, merged


def enrich_offsets(ue, log, offs: Dict[str, int], offs_path: str) -> Dict[str, int]:
    """补齐世界链 / 相机链偏移（有缓存就用缓存，缺什么补什么）。"""
    todo = []
    if not offs.get("pov"):
        todo.append("camera")
    if not offs.get("position") and not offs.get("root_component"):
        todo.append("world")
    if "camera" in todo:
        cam = resolver.resolve_camera_offsets(ue, log)
        if cam:
            offs.update(cam)
            resolver.save_offsets(offs_path, cam, source="resolve_camera_offsets")
    if "world" in todo:
        wc = resolver.resolve_world_chain(ue, log)
        if wc:
            offs.update(wc)
            resolver.save_offsets(offs_path, wc, source="resolve_world_chain")
        loc = resolver.resolve_location_offsets(ue, log)
        if loc:
            offs.update(loc)
            resolver.save_offsets(offs_path, loc, source="resolve_location_offsets")
    return offs


def cmd_dump(args) -> int:
    log = util.Log(args.log_level)
    proc = open_process({"pid": args.pid, "window_title": args.title,
                         "exe": args.exe}, log)
    os.makedirs(args.dump_dir, exist_ok=True)
    out = os.path.join(args.dump_dir, "%s-%d.bin" % (args.module_name, int(time.time())))
    path, size, regions = dump_mod.dump_module(proc, args.module_name, out,
                                               max_bytes=args.max_mb * 1024 * 1024)
    meta = out + ".regions.json"
    with open(meta, "w", encoding="utf-8") as f:
        json.dump(regions, f)
    log.info("已落盘 %s（%.1f MB），区域表 %s" % (path, size / 1048576.0, meta))
    proc.close()
    return 0


def cmd_probe(args) -> int:
    log = util.Log(args.log_level)
    proc = open_process({"pid": args.pid, "window_title": args.title,
                         "exe": args.exe}, log)
    ue, offs = init_ue(proc, log, args.offsets_file, no_cache=args.no_cache)
    offs = enrich_offsets(ue, log, offs, args.offsets_file)
    print()
    print("== 已解析偏移 ==")
    for k in sorted(offs):
        if isinstance(offs[k], int) and offs[k]:
            print("  %-20s 0x%X" % (k, offs[k]))
    print()
    print("== 结构自检 ==")
    print(json.dumps(ue.stats(), indent=2, ensure_ascii=False))
    check = ue.self_check(sample=args.sample)
    print(json.dumps(check, indent=2, ensure_ascii=False))
    print()
    print("== 关键类可见性 ==")
    for needle in ("World", "GameState", "PlayerController", "PlayerState",
                   "PlayerCameraManager", "Pawn", "Character", "Actor"):
        found = ue.find_objects_by_class([needle], max_objects=200000)
        print("  %-24s %d 个" % (needle, len(found)))
        for _idx, obj, cn, on in found[:3]:
            print("      %s  %s  @0x%X" % (cn, on or "-", obj))
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        filt = Filter.from_file(args.filter_file)
        sc = Scanner(ue, filt, log)
        sc.full_scan()
        sc.dump_report(args.report)
        log.info("类名统计报告：%s" % args.report)
    proc.close()
    return 0


def cmd_selftest(args) -> int:
    """纯数学自检：世界坐标 -> 屏幕坐标的投影是否正确。"""
    ov = Overlay.__new__(Overlay)
    r = Renderer(ov)
    w, h = 1920, 1080
    cases = [
        ("正前方", (1000.0, 0.0, 0.0), (960.0, 540.0)),
        ("正右方", (1000.0, 500.0, 0.0), (1440.0, 540.0)),
        ("正上方", (1000.0, 0.0, 500.0), (960.0, 540.0 - 480.0)),
        ("正下方", (1000.0, 0.0, -500.0), (960.0, 540.0 + 480.0)),
        ("左前方", (1000.0, -500.0, 0.0), (480.0, 540.0)),
    ]
    print("== 投影自检 (fov=90, 1920x1080, 相机在原点朝 +X) ==")
    ok = True
    for name, world, expect in cases:
        r.cam = {"x": 0.0, "y": 0.0, "z": 0.0, "pitch": 0.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0}
        got = r.world_to_screen(world[0], world[1], world[2], w, h)
        if got is None:
            print("  %-6s 失败：返回 None" % name)
            ok = False
            continue
        dx = abs(got[0] - expect[0])
        dy = abs(got[1] - expect[1])
        flag = "OK " if (dx < 1.5 and dy < 1.5) else "FAIL"
        if flag == "FAIL":
            ok = False
        print("  %s %-6s 期望(%.0f,%.0f) 实际(%.0f,%.0f)" %
              (flag, name, expect[0], expect[1], got[0], got[1]))
    # 背后目标必须被剔除
    r.cam = {"x": 0.0, "y": 0.0, "z": 0.0, "pitch": 0.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0}
    behind = r.world_to_screen(-500.0, 0.0, 0.0, w, h)
    print("  %s 背后目标剔除" % ("OK " if behind is None else "FAIL"))
    ok = ok and behind is None
    # pitch 抬头
    r.cam = {"x": 0.0, "y": 0.0, "z": 0.0, "pitch": 90.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0}
    up = r.world_to_screen(0.0, 0.0, 1000.0, w, h)
    good_up = up is not None and abs(up[0] - 960.0) < 1.5 and abs(up[1] - 540.0) < 1.5
    print("  %s 抬头 90° 看正上方目标" % ("OK " if good_up else "FAIL"))
    ok = ok and good_up
    print()
    print("投影自检结果：%s" % ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 2


def cmd_esp(args) -> int:
    log = util.Log(args.log_level)
    cfg = config_mod.Config.load(args.config)
    cfg.set("overlay.mode", args.mode or cfg.get("overlay")["mode"])
    cfg.set("overlay.fps", args.fps or cfg.get("overlay")["fps"])
    cfg.set("read.max_distance", args.max_distance or cfg.get("read")["max_distance"])
    if args.no_radar:
        cfg.set("draw.radar", False)
    if args.no_tracer:
        cfg.set("draw.tracer", False)
    if args.show_other:
        cfg.set("esp.show_other", True)

    proc = open_process({"pid": args.pid, "window_title": args.title,
                         "exe": args.exe}, log)
    ue, offs = init_ue(proc, log, args.offsets_file, no_cache=args.no_cache)
    offs = enrich_offsets(ue, log, offs, args.offsets_file)

    filt = Filter.from_file(args.filter_file)
    sc = Scanner(ue, filt, log,
                 refresh_interval=cfg.get("read")["full_scan_interval"],
                 include_other=bool(cfg.get("esp")["show_other"]),
                 max_distance=float(cfg.get("read")["max_distance"]),
                 patterns=cfg.get("class_patterns"))
    sc.full_scan()
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        sc.dump_report(args.report)
        log.info("类名统计报告：%s" % args.report)

    local = player_mod.LocalPlayer(ue, log)
    local.init()
    cam = local.read_camera()

    # 自动定位坐标字段（运动反推），只需一次，之后写进 offsets.json
    position_off = int(offs.get("position") or 0)
    if not position_off:
        samples: List[int] = []
        for cand in sorted(sc.candidates.values(), key=lambda c: c.kind):
            if "character" in cand.class_name.lower() or "pawn" in cand.class_name.lower():
                samples.append(cand.obj)
        if len(samples) < 2:
            samples = [c.obj for c in list(sc.candidates.values())[:24]]
        log.info("开始运动反推坐标字段（%d 个样本，约 0.5s）" % len(samples))
        found = autoloc.resolve_position_offset(ue, samples, log)
        if found:
            position_off = int(found["position"])
            offs["position"] = position_off
            offs["position_dtype"] = str(found.get("position_dtype") or "f32")
            resolver.save_offsets(args.offsets_file,
                                  {"position": position_off,
                                   "position_dtype": offs["position_dtype"]},
                                  source="motion_vote")
            sc.light_scan(offs)

    ov = Overlay(fps=int(cfg.get("overlay")["fps"]),
                 color_key=util.hex_to_colorref(str(cfg.get("overlay")["color_key"])),
                 font_size=int(cfg.get("overlay")["font_size"]),
                 mode=str(cfg.get("overlay")["mode"]))
    if not args.dry_run:
        ov.create()
    else:
        log.info("空跑模式：不创建叠加层，只打印目标列表（用于安全验证）")
    rd = Renderer(ov)
    for key in ("box", "name", "distance", "radar", "crosshair", "skeleton"):
        if key in rd.cfg:
            rd.cfg[key] = bool(cfg.get("draw").get(key, rd.cfg[key]))
    rd.cfg["tracer"] = bool(cfg.get("draw").get("tracer", False))
    rd.cfg["offscreen"] = bool(cfg.get("draw").get("offscreen_marker", True))
    rd.cfg["stats"] = bool(cfg.get("draw").get("stats", True))
    rd.colors["hider"] = util.hex_to_colorref(str(cfg.get("colors")["hider"]))
    rd.colors["hunter"] = util.hex_to_colorref(str(cfg.get("colors")["hunter"]))
    rd.colors["other"] = util.hex_to_colorref(str(cfg.get("colors")["other"]))
    rd.colors["dummy"] = util.hex_to_colorref(str(cfg.get("colors")["dummy"]))
    rd.colors["radar"] = util.hex_to_colorref(str(cfg.get("colors")["radar"]))
    rd.colors["text"] = util.hex_to_colorref(str(cfg.get("colors")["text"]))
    rd.origin_kind = str(cfg.get("esp").get("tracer_origin", "bottom"))

    keys = Keys(cfg.get("keys"))
    enabled = True
    frames = 0
    read_errors = 0
    t_last_read = 0.0
    t_last_full = time.perf_counter()
    target_count = 0
    bridge = None
    if args.bridge:
        from sdk.bridge import Bridge
        state = {}

        def provider():
            return state

        bridge = Bridge(provider, port=args.bridge_port, log=log)
        bridge.start()

    log.info("叠加层就绪：模式=%s 帧率=%d 热键=%s"
             % (ov.mode, ov.fps, ", ".join("%s:%s" % (k, v) for k, v in cfg.get("keys").items())))
    log.info("按 %s 开关透视，%s 退出" % (cfg.get("keys")["toggle"], cfg.get("keys")["exit"]))

    try:
        while True:
            # --- 热键 ---
            if keys.pressed("exit"):
                log.info("收到退出热键")
                break
            if keys.pressed("toggle"):
                enabled = not enabled
                log.info("透视 %s" % ("开启" if enabled else "关闭"))
            if keys.pressed("rescan"):
                sc.full_scan()
                local.refresh()
            if keys.pressed("toggle_radar"):
                rd.cfg["radar"] = not rd.cfg["radar"]
            if keys.pressed("toggle_other"):
                sc.include_other = not sc.include_other
                log.info("其他类目标 %s" % ("开启" if sc.include_other else "关闭"))
                sc.full_scan()
            if keys.pressed("toggle_stats"):
                rd.cfg["stats"] = not rd.cfg["stats"]

            if not proc.alive():
                log.warn("目标进程已退出")
                break

            # --- 读数据 ---
            now = time.perf_counter()
            if now - t_last_read >= max(0.001, float(cfg.get("read")["interval_ms"]) / 1000.0):
                t_last_read = now
                cam = local.read_camera()
                if cam is None:
                    read_errors += 1
                    if read_errors % 120 == 1:
                        log.warn("相机读取失败（第 %d 次），尝试重新绑定本地玩家" % read_errors)
                        local.refresh()
                else:
                    read_errors = 0
                if position_off or offs.get("root_component"):
                    sc.light_scan(offs)
                if now - t_last_full >= float(sc.refresh_interval):
                    t_last_full = now
                    sc.full_scan()

            # --- 画 ---
            if args.dry_run:
                if frames % 60 == 0 and cam:
                    sc.pick_self(cam, list(sc.candidates.values()))
                    rows = sc.to_render_list(cam, list(sc.candidates.values()))
                    target_count = len(rows)
                    print("[空跑] 相机(%.0f,%.0f,%.0f) yaw=%.1f 目标 %d 个"
                          % (cam["x"], cam["y"], cam["z"], cam["yaw"], len(rows)))
                    for t in rows[:12]:
                        print("        %-8s %-34s %6.1fm  %s"
                              % (t["kind"], str(t["label"])[:34], t["distance"],
                                 "移动中" if t["moving"] else "静止"))
                frames += 1
                time.sleep(0.05)
                continue
            ov.begin()
            w, h = ov.screen.size
            if enabled and cam:
                rd.cam = cam
                sc.pick_self(cam, list(sc.candidates.values()))
                targets = sc.to_render_list(cam, list(sc.candidates.values()))
                target_count = len(targets)
                for t in targets:
                    kind = str(t.get("kind") or "other")
                    if kind == HIDER and not cfg.get("esp")["show_hider"]:
                        continue
                    if kind == HUNTER and not cfg.get("esp")["show_hunter"]:
                        continue
                    if kind == OTHER and not cfg.get("esp")["show_other"]:
                        continue
                    if kind == DUMMY and not cfg.get("esp")["show_dummy"]:
                        continue
                    rd.draw_target(t, w, h, kind)
                if rd.cfg["offscreen"]:
                    rd.draw_offscreen(targets, w, h)
                rd.draw_radar(targets, w, h)
                rd.draw_crosshair(w, h)
            if rd.cfg["stats"]:
                lines = [
                    "MECCHA CHAMELEON ESP  |  PID %d  |  %.0f FPS" % (proc.pid, ov.fps_actual),
                    "目标 %d / 候选 %d / 对象 %d" % (target_count, len(sc.candidates), ue.num()),
                    "坐标字段 +0x%X  相机 %.0f,%.0f,%.0f  yaw %.1f pitch %.1f fov %.0f" % (
                        position_off, (cam or {}).get("x", 0.0), (cam or {}).get("y", 0.0),
                        (cam or {}).get("z", 0.0), (cam or {}).get("yaw", 0.0),
                        (cam or {}).get("pitch", 0.0), (cam or {}).get("fov", 90.0)),
                    "F4 开关 | F5 重扫 | F6 雷达 | F7 其他 | F8 信息 | END 退出",
                ]
                for i, line in enumerate(lines):
                    ov.text(12, 10 + i * 17, line, rd.colors["text"], i == 0)
            ov.present()
            frames += 1
            if args.screenshot and frames == int(args.screenshot):
                shot = os.path.abspath(args.screenshot_path or "screenshot.png")
                log.info("已导出叠加层截图：%s（%s）"
                         % (shot, "成功" if ov.snapshot(shot) else "失败（需 Pillow）"))
            if bridge:
                state["fps"] = ov.fps_actual
                state["targets"] = len(sc.candidates)
                state["camera"] = cam
                state["pid"] = proc.pid
                state["objects"] = ue.num()
            ov.sleep_frame()
    except KeyboardInterrupt:
        log.info("用户中断")
    finally:
        resolver.save_offsets(args.offsets_file, offs, source="session_end")
        if bridge:
            bridge.stop()
        if not args.dry_run:
            ov.destroy()
        proc.close()
        log.info("已退出：绘制 %d 帧，候选 %d 个" % (frames, len(sc.candidates)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="MECCHA CHAMELEON ESP 插件")
    p.add_argument("--pid", type=int, default=0, help="目标进程号（默认按窗口标题/进程名找）")
    p.add_argument("--title", default=GAME_DEFAULT["title"], help="游戏窗口标题子串")
    p.add_argument("--exe", default=GAME_DEFAULT["exe"], help="游戏进程名")
    p.add_argument("--config", default=os.path.join(APP_DIR, "config.json"), help="配置文件路径")
    p.add_argument("--offsets-file", default=os.path.join(APP_DIR, "offsets.json"), help="偏移缓存文件")
    p.add_argument("--filter-file", default=os.path.join(APP_DIR, "filters.json"), help="自定义类名规则文件")
    p.add_argument("--elevated", action="store_true", help="内部标记：已通过 UAC 提权重启")
    p.add_argument("--no-cache", action="store_true", help="忽略 offsets.json，强制重新扫描")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warn", "error"])
    p.add_argument("--elevate", action="store_true", help="以管理员权限重启自身")
    sub = p.add_subparsers(dest="cmd")

    e = sub.add_parser("esp", help="启动叠加层")
    e.add_argument("--mode", default=None, choices=["dc", "window"], help="绘制模式")
    e.add_argument("--fps", type=int, default=0, help="叠加层帧率")
    e.add_argument("--max-distance", type=float, default=0.0, help="最远显示距离（cm）")
    e.add_argument("--no-radar", action="store_true")
    e.add_argument("--no-tracer", action="store_true")
    e.add_argument("--show-other", action="store_true", help="连非角色目标一起显示")
    e.add_argument("--report", default="", help="同时输出类名统计报告（默认写到程序目录）")
    e.add_argument("--bridge", action="store_true", help="启动本地数据桥")
    e.add_argument("--bridge-port", type=int, default=17655)
    e.add_argument("--dry-run", action="store_true", help="不画叠加层，只在控制台打印目标")
    e.add_argument("--screenshot", type=int, default=0, help="第 N 帧导出 PNG")
    e.add_argument("--screenshot-path", default="", help="PNG 输出路径")
    e.set_defaults(func=cmd_esp)

    r = sub.add_parser("probe", help="离线探测与结构自检")
    r.add_argument("--report", default=os.path.join(APP_DIR, "reports", "classes.md"))
    r.add_argument("--sample", type=int, default=4000)
    r.set_defaults(func=cmd_probe)

    d = sub.add_parser("dump", help="主模块内存镜像落盘")
    d.add_argument("--module-name", default=GAME_DEFAULT["exe"])
    d.add_argument("--dump-dir", default=os.path.join(APP_DIR, "dumps"))
    d.add_argument("--max-mb", type=int, default=0, help="最多写多少 MB，0 表示全量")
    d.set_defaults(func=cmd_dump)

    t = sub.add_parser("selftest", help="投影数学自检（不需要游戏）")
    t.set_defaults(func=cmd_selftest)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = parser.parse_args(list(argv or sys.argv[1:]) + ["esp"])
    elevate_if_needed()
    if getattr(args, "cmd", None) == "selftest":
        return cmd_selftest(args)
    os.makedirs(os.path.dirname(os.path.abspath(args.offsets_file)), exist_ok=True)
    if not os.path.exists(args.config):
        config_mod.Config({}).save(args.config)
    if not os.path.exists(args.filter_file):
        with open(args.filter_file, "w", encoding="utf-8") as f:
            f.write(filters.default_rules_json())
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
