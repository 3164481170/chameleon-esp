"""静态一致性检查：模块导入、跨模块符号、配置默认值、UI 常量。"""
import importlib, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAIL = []
def ck(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)

print("== 模块导入 ==")
mods = ["sdk.util", "sdk.mem", "sdk.dump", "sdk.ue", "sdk.resolver", "sdk.autoloc",
        "sdk.esp", "sdk.filters", "sdk.overlay", "sdk.player", "sdk.config", "sdk.bridge"]
for m in mods:
    try:
        importlib.import_module(m)
        ck(True, m)
    except Exception as exc:
        ck(False, "%s -> %r" % (m, exc))

import sdk.config as C
import sdk.filters as F
import sdk.overlay as O
import sdk.util as U
import sdk.ue as UE
from sdk.config import Config, DEFAULT

print("== 配置默认值完整性 ==")
cfg = Config({})
for path in ["process.pid", "overlay.mode", "overlay.fps", "read.interval_ms",
             "read.max_distance", "draw.box", "draw.radar", "esp.show_hider",
             "esp.show_hunter", "keys.toggle", "keys.exit", "colors.hider",
             "class_patterns", "offsets_file"]:
    v = cfg.get(path.split(".")[0], {})
    cur = cfg.data
    ok = True
    for p in path.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            ok = False
            break
    ck(ok, "配置项 %s = %s" % (path, cur if ok else "<缺失>"))
ck(len(cfg.get("class_patterns")) >= 3, "class_patterns 至少 3 条")

print("== 颜色解析 ==")
ck(U.hex_to_colorref("#00FF3C") == 0x003CFF00, "绿 #00FF3C -> 0x%06X" % U.hex_to_colorref("#00FF3C"))
ck(U.hex_to_colorref("#FF2D2D") == 0x002D2DFF, "红 #FF2D2D -> 0x%06X" % U.hex_to_colorref("#FF2D2D"))
ck(U.hex_to_colorref("bad", 0x123456) == 0x123456, "非法颜色回退")

print("== 按键解析 ==")
ck(U.parse_vk("F4") == 0x73, "F4 -> 0x73")
ck(U.parse_vk("end") == 0x23, "END -> 0x23")
ck(U.parse_vk("A") == 0x41, "A -> 0x41")

print("== 类名规则 ==")
f = F.Filter()
cases = [
    ("BP_FirstPersonCharacter_cLeon_Character_Survivor_C", "hider"),
    ("BP_FirstPersonCharacter_cLeon_Character_Hunter_C", "hunter"),
    ("BP_FirstPersonCharacter_cLeon_Character_Hunter_Default_C", "hunter"),
    ("BP_FirstPersonPlayerState_Online_cLeon_C", "other"),
    ("WBP_KillLog_C", "skip"),
    ("T_Chameleon_D", "skip"),
    ("SM_Wood", "skip"),
]
for cn, want in cases:
    got = f.classify(cn)
    ck(got == want, "%-58s -> %s" % (cn, got))

print("== 投影数学 ==")
ov = O.Overlay.__new__(O.Overlay)
r = O.Renderer(ov)
r.cam = {"x": 0.0, "y": 0.0, "z": 0.0, "pitch": 0.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0}
p = r.world_to_screen(1000.0, 0.0, 0.0, 1920, 1080)
ck(p is not None and abs(p[0] - 960) < 1 and abs(p[1] - 540) < 1, "正前方投影")
ck(r.world_to_screen(-1000.0, 0.0, 0.0, 1920, 1080) is None, "背后剔除")
r.cam["yaw"] = 90.0
p = r.world_to_screen(0.0, 1000.0, 0.0, 1920, 1080)
ck(p is not None and abs(p[0] - 960) < 1 and abs(p[1] - 540) < 1, "yaw=90 朝 +Y 投影")
r.cam = {"x": 0.0, "y": 0.0, "z": 0.0, "pitch": -30.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0}
p = r.world_to_screen(1000.0, 0.0, 0.0, 1920, 1080)
ck(p is not None and abs(p[0] - 960) < 1 and p[1] < 540, "俯视时眼平目标出现在屏幕上方")
r.cam = {"x": 0.0, "y": 0.0, "z": 0.0, "pitch": 0.0, "yaw": 0.0, "roll": 0.0, "fov": 90.0}
p = r.world_to_screen(1000.0, 0.0, -300.0, 1920, 1080)
ck(p is not None and p[1] > 540, "脚下方目标投影在屏幕下方")

print("== 偏移结构体 ==")
o = UE.UeOffsets(position=0x1234) if "position" in UE.UeOffsets.__dataclass_fields__ else UE.UeOffsets()
ck(isinstance(o.to_dict(), dict), "UeOffsets.to_dict")
o2 = UE.UeOffsets.from_dict({"objects": 0x20, "fname": 0x18, "position_dtype": "f64", "junk": 1})
ck(o2.objects == 0x20 and o2.fname == 0x18, "UeOffsets.from_dict 过滤未知字段")

print("== 掩码解析 ==")
pat, mask = __import__("sdk.mem", fromlist=["mem"]).parse_aob("48 8B ?? E8 ? ? ? ?")
ck(len(pat) == 8 and mask.count(b"x") == 3 and mask.count(b"?") == 5,
   "parse_aob：8 字节 / 固定 3 / 通配 5")

print()
print("静态一致性检查：%d 项失败" % len(FAIL))
if FAIL:
    for m in FAIL:
        print("  - " + m)
    raise SystemExit(1)
