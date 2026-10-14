# MECCHA CHAMELEON 透视插件（Chameleon ESP）

给 **MECCHA CHAMELEON / PenguinHotel**（Unreal Engine 5.6.1，Steam AppID 4704690）写的
运行时透视（ESP）插件。纯 Python + Win32 API 实现，不需要注入 DLL、不修改游戏文件、
不写进程内存，只做**只读内存解析 + 屏幕叠加层绘制**。

```
游戏目录：E:\steam\steamapps\common\MECCHA CHAMELEON
主模块  ：Chameleon\Binaries\Win64\PenguinHotel-Win64-Shipping.exe  (169.8 MB, UE 5.6.1)
引擎    ：UE5.6 Shipping，Steamworks 1.57，EOS SDK（RedpointEOS）
```

**直接下载**：[`dist/ChameleonESP.exe`](../../releases)（13.8 MB，单文件，免装 Python）·
[打包说明](#12-打包成-exe免装-python) · [MIT License](LICENSE)

---

## 1. 它能做什么

| 功能 | 说明 | 开关 |
| --- | --- | --- |
| 透视方框 | 把场上玩家的世界坐标投影到屏幕，画 2D 方框 | F4 总开关 |
| 名称 + 距离 | 每帧显示对象名与实时距离（米） | 配置 draw.name / draw.distance |
| 阵营配色 | 幸存者/Survivor 绿色，猎人/Hunter 红色，其他青色 | filters.json |
| 雷达 | 左下角圆形雷达，按自身朝向旋转 | F6 |
| 屏幕外指示 | 视野外的目标在屏幕边缘显示方向箭头 | 配置 draw.offscreen_marker |
| 移动判定 | 相邻两次扫描位置变化 > 1cm 判定为“移动中”，用来区分真人/场景装饰 | 自动 |
| 准心 / 状态栏 | 屏幕中心准心、左上角状态信息（帧率、目标数、偏移） | F8 |
| 数据桥 | 本地 HTTP 接口 http://127.0.0.1:17655/state 输出实时 JSON | --bridge |

设计取向：**不给任何硬编码偏移**。所有结构偏移（GObjects、GNames、世界链、坐标字段、
相机 POV）都在运行时自动解析，并且在游戏更新后自动重新校准——不会因为一次小补丁就失效。

---

## 2. 快速开始

前置：Windows 10/11、Python 3.10+、**以管理员身份运行**（读进程内存需要）。

```bat
:: 1) 启动游戏并进入主菜单或一局对局（必须有 UWorld 才能解析世界链）
:: 2) 管理员 PowerShell 里执行：

cd <本目录>
python chameleon_esp.py esp

:: 帧率不够就降一点，或者提高：
python chameleon_esp.py esp --fps 240

:: 只想验证解析是否正确、不画东西：
python chameleon_esp.py esp --dry-run

:: 只做探测 + 出类名报告，不启动叠加层：
python chameleon_esp.py probe --report reports/classes.md
```

也可以直接双击 `run.bat`（自带提权与菜单）。

热键（可在 `config.json` 里改）：

| 键 | 作用 |
| --- | --- |
| `F4` | 透视 开 / 关 |
| `F5` | 立即重新扫描对象并重新绑定本地玩家（换局、复活后按） |
| `F6` | 雷达 开 / 关 |
| `F7` | 其他类目标（非角色物件）开 / 关 |
| `F8` | 状态栏 开 / 关 |
| `END` | 退出 |

---

## 3. 命令一览

| 命令 | 作用 |
| --- | --- |
| `esp` | 启动叠加层（主功能） |
| `probe` | 探测 + 结构自检 + 类名直方图，并输出 Markdown 报告 |
| `dump` | 把主模块内存镜像落盘，供离线分析/对比版本 |
| `selftest` | 纯数学自检（不需要游戏），验证世界坐标→屏幕坐标投影 |

常用参数：

```
--pid N              指定进程号（默认按窗口标题 MECCHA CHAMELEON 自动找）
--config config.json 配置文件
--offsets-file       偏移缓存（下次启动直接复用，省掉扫描时间）
--no-cache           忽略缓存强制重扫（游戏更新后第一次用）
--log-level debug    打印每一步定位细节
--elevate            以管理员身份重启自身
--mode dc|window     叠加层模式（见第 6 节）
--dry-run            只打印目标列表，不创建叠加层
--bridge             启动本地数据桥
--screenshot N       第 N 帧导出 PNG（自绘内容，不抓桌面）
```

辅助工具：

```
python tools\diagnose_offsets.py --deep    定位失败时的诊断（候选全打印）
python tools\consistency_check.py          静态一致性检查（导入/配置/数学/规则）
python tools\analyze_exe.py                在 exe 里离线验证结构假设
```

---

## 4. 它是怎么工作的

### 4.1 为什么不用特征码（AOB）

传统插件写死一串 `48 8B 05 ?? ?? ?? ?? 48 8B 88 ...` 去搜 `GObjects`。
UE 每次更新都会重排指令，特征码立刻失效。这里全部改成**结构自证**：

```
1) GNames (FNamePool)
   在可写数据段里找“连续若干个 64KB 对齐的可读指针” → 区块表 Blocks[8192]
   再用已知名字验证：用一批 id 去读名字，看能不能读出
   "None" / "ByteProperty" / "IntProperty" ... 这些任何 UE 构建都存在的名字。

   离线验证：tools/analyze_exe.py 直接在 shipping exe 里按
   [2 字节头 + UTF-8 + NUL] 解析出 "ByteProperty" / "IntProperty" / "FloatProperty"
   等连续条目，证明条目头是 2 字节、块内按 id 顺序分配 —— 与实现完全一致。

2) GObjects (FUObjectArray)
   a. 在数据段里找成对出现的 (MaxElements, NumElements) 两个 int32
      —— 数量在几千到几百万、容量 >= 数量，这个组合极其罕见，直接把候选压到几十个
   b. 廉价先验：数组里存的必须是 Chunk 指针，Chunk 里放的是可读的 UObject*
   c. 结构自证：第 0 个元素必须是“类是 UClass 的 UClass”（UE 的硬性质）
   d. 元素大小自校准：FUObjectItem 可能是 0x10 / 0x18 / 0x20 字节，
      用哪个能连续遍历出对象就用哪个

3) 本地玩家 / 相机
   先在所有 PlayerCameraManager 里找 FMinimalViewInfo：
   Location 是 3 个分量、Rotation 是 3 个满足 [-90,90]/[-180,180] 的角、
   附近还有一个 25~150 的 FOV —— 同时满足这三条的内存位置基本唯一。
   多个相机时，挑“短时间内位置真的会变”的那个 = 本地玩家。

4) 目标坐标字段
   关键洞察：**角色的世界坐标会随移动而变化**。
   对一批候选对象采多帧，统计每个候选 float3/double3 偏移的位移量，
   变化量最大且在多个对象上同时成立的那个，就是坐标字段（autoloc.py）。
   没人动的时候退化为“静态一致性投票”：找在 >=75% 对象上都读出合理世界坐标、
   且数值分散（不是常量）的字段。

   UE5 的 LWC（Large World Coordinates）让 FVector 变成 3 个 double，
   所以 f32 / f64 两种布局都会试，并对 double 略微加权。
```

### 4.2 读取与绘制的分工

```
读取（默认 16ms 一轮）                     渲染（默认 144 FPS）
  ├─ 相机 (POV: 位置/角度/FOV)              ├─ begin()  清空绘制队列
  ├─ 目标坐标（只读候选对象）                ├─ box/line/text  入队
  ├─ 全量扫描（每 5s 一次，刷新候选）        ├─ present() 一次性提交到屏幕
  └─ 本地玩家绑定                            └─ sleep(1/fps)
```

候选对象**缓存**：全量枚举 UObject（几十万条目）只做一次（约 1~3 秒），
之后每帧只对缓存的几十个候选读坐标（亚毫秒）。类名解析用 FName 块缓存
（一次读整个 64KB 块），遍历对象数组因此是纯内存操作。

### 4.3 时间线

```
启动 → 找进程 → 定位 GObjects/GNames（首次 3~10s，之后走缓存 < 0.3s）
     → 相机校准 → 全量扫描候选 → 运动反推坐标字段（约 0.5s）
     → 创建叠加层 → 每帧：读坐标 → 投影 → 绘制
```

---

## 5. 解析出的偏移存在哪

`offsets.json`：每次运行会把成功的偏移写进去，下次直接复用。

```json
{
  "fields": {
    "uobject_array": {"value": 87059432, "source": "find_globals"},
    "fname_pool":    {"value": 87311056, "source": "find_globals"},
    "objects":       {"value": 32,       "source": "find_globals"},
    "fname":         {"value": 24,       "source": "find_globals"},
    "item_size":     {"value": 16,       "source": "find_globals"},
    "pov":           {"value": 2736,     "source": "resolve_camera_offsets"},
    "position":      {"value": 400,      "source": "motion_vote"}
  }
}
```

注意：**模块基址每次启动都会变**（ASLR），所以 `uobject_array` / `fname_pool`
这两个绝对地址会做失效检测（用 `self_check` 抽查命名/分类成功率），失效就重扫；
而 `objects` / `fname` / `item_size` / `pov` 这些**相对偏移不会变**，可长期复用。

---

## 6. 叠加层模式

UE5 用 flip model DX12 独占前台缓冲时，直接在桌面 DC 上 BitBlt 会和游戏抢同一块
present 缓冲，可能出现闪烁。所以给了两种模式，用 `--mode` 切换：

| 模式 | 做法 | 适用 |
| --- | --- | --- |
| `dc`（默认） | CreateDIBSection + 桌面 DC BitBlt，每帧 GdiFlush | 窗口化 / 无边框窗口，简单直接 |
| `window` | 创建 WS_EX_LAYERED + WS_EX_TRANSPARENT + WS_EX_TOPMOST + WS_EX_NOACTIVATE 分层窗口，colorkey 抠背景 | 独占全屏，或 dc 模式闪烁时 |

窗口模式下的窗口是**点击穿透**的（WS_EX_TRANSPARENT），不会抢走游戏的鼠标输入，
也不会被 Alt+Tab 影响。

调试建议：先用 `--dry-run` 确认解析出的目标是否正确（会打印类别/名称/距离），
再上叠加层。

---

## 7. 目录结构

```
chameleon-esp/
├─ chameleon_esp.py          主程序（esp / probe / dump / selftest）
├─ run.bat                   一键启动（自动提权 + 菜单）
├─ config.json               配置（首次运行自动生成）
├─ offsets.json              偏移缓存（自动生成）
├─ filters.json              类名 → 类别规则（首次运行自动生成）
├─ sdk/
│  ├─ mem.py                 Win32 内存读写、模块枚举、区域遍历、AOB 扫描
│  ├─ ue.py                  FUObjectArray / FNamePool / UObject 访问层
│  ├─ resolver.py            运行期偏移自动发现（GObjects / GNames / 世界链 / 相机）
│  ├─ autoloc.py             运动反推坐标字段、POV 定位、本地玩家挑选
│  ├─ esp.py                 目标采集（全量扫描 + 轻量扫描 + 候选缓存）
│  ├─ filters.py             类名规则（hider / hunter / other / skip）
│  ├─ player.py              本地玩家绑定（PC / PCM / Pawn / 相机）
│  ├─ overlay.py             叠加层（GDI 绘制 + 世界→屏幕投影）
│  ├─ bridge.py              本地 HTTP 数据桥（可选）
│  ├─ config.py              配置读写
│  └─ dump.py                内存镜像落盘
└─ tools/
   ├─ diagnose_offsets.py    定位诊断（失败时先跑这个）
   ├─ consistency_check.py   静态一致性检查
   ├─ analyze_exe.py         exe 离线结构验证
   ├─ probe_fname.py         打印 FName 条目原始字节
   └─ scan_strings.py        大文件字符串提取（pak / utoc 情报）
```

---

## 8. 类名规则（filters.json）

规则来自游戏 pak 里的真实资产名：

```
猎人阵营：BP_FirstPersonCharacter_cLeon_Character_Hunter[_Default|_Cube|_Fukuyoka]
幸存者  ：BP_FirstPersonCharacter_cLeon_Character_Survivor[_Default_Base|_Cube|...]
状态类  ：BP_FirstPersonPlayerState[_Online|_LINK|_cLeon]、BP_GameState_cLeon
可交互物：BP_CraftedObject_*、BP_RandomObject_*、BP_PresentSpawner
界面(跳过)：WBP_*、MI_*、T_*、SM_*
```

规则是**顺序敏感的正则列表**，直接改 `filters.json` 即可。例如想把玩家状态对象
单独标成紫色：

```json
{ "rules": [
  { "pattern": "(?i)survivor", "tag": "hider" },
  { "pattern": "(?i)hunter",   "tag": "hunter" },
  { "pattern": "(?i)playerstate", "tag": "dummy" }
] }
```

---

## 9. 常见问题

**Q: 提示“找不到游戏进程”**
先启动游戏。Steam 里的进程名是 `PenguinHotel-Win64-Shipping.exe`，
启动器 `PenguinHotel.exe` 只是壳。也可以用 `--pid` 直接指定。

**Q: 提示“定位失败”或对象数一直是 0**
1. 确认**已经进入一局对局或主菜单**（空载时对象数组还没建立）；
2. 加上 `--no-cache` 强制重扫；
3. 跑 `python tools\diagnose_offsets.py --deep`，把输出保存下来——
   它会打印名字池候选、数值对候选、通过先验的候选，一眼能看出卡在哪一步。

**Q: 方框位置飘、偏上或偏下**
投影用的是相机位置 + 相机朝向 + FOV。如果游戏内 FOV 被改过，程序读到的是**当前实际 FOV**，
会自动跟随。如果仍有偏差，说明坐标字段被投票选成了相近字段，删除 `offsets.json` 里的
`position` 后重跑即可重新反推。

**Q: 帧率掉到 60 以下**
把 `config.json` 里的 `read.interval_ms` 调大（比如 33 = 30Hz 更新），
`read.full_scan_interval` 调到 10 秒；渲染帧率 `overlay.fps` 保持高值即可。
全量扫描只在刷新时做，卡顿通常说明刷新太频繁。

**Q: 某个角色类没被识别**
跑 `probe --report reports/classes.md` 看类名直方图，把新类名按过滤器格式加进
`filters.json` 的正则里即可，不用改代码。

**Q: 游戏更新后失效**
先 `--no-cache` 跑一次。结构自证是针对 UE 对象模型设计的，通常能自动适应；
若真的失败，跑 `dump` 落盘 + `tools\diagnose_offsets.py --deep` 定位。

---

## 10. 已验证 / 待运行期确认

已经在本机离线验证（不需要游戏进程）：

- [x] 全部 12 个模块导入无误，静态一致性检查 30+ 项全部通过
- [x] 世界坐标→屏幕坐标投影：正前方 / 正右方 / 正上方 / 正下方 / 左前方 /
      背后剔除 / 抬头 90° / 俯视，误差 < 1px
- [x] GDI 叠加层对象生命周期、绘制队列、颜色解析（#RRGGBB → COLORREF）
- [x] 类名规则对 7 组真实资产名分类正确
- [x] (MaxElements, NumElements) 候选推导的数学（由 num 地址反推数组起点）
- [x] 相机 POV 两种布局（f32 / f64）的字段推导逻辑
- [x] FName 条目布局：在 169.8 MB 的 shipping exe 里定位到
      ByteProperty / IntProperty / FloatProperty 连续条目

需要在**游戏运行时**确认的部分（第一次跑请用 `--dry-run`）：

- [ ] GObjects / GNames 的实际地址与字段布局（自证打分应当 >= 30）
- [ ] PlayerCameraManager 的 POV 偏移具体数值
- [ ] 目标坐标字段的具体偏移（运动反推结果）
- [ ] 两种阵营类名在实际对局里的具体命名

---

## 12. 打包成 exe（免装 Python）

已经生成好：@@dist\ChameleonESP.exe@@（单文件，约 13.6 MB，内嵌 @@requireAdministrator@@ 清单）。

### 12.1 直接用

@@@
dist\ChameleonESP.exe              :: 双击即用，会弹一次 UAC（读写游戏进程需要）
dist\ChameleonESP.exe selftest     :: 投影自检，不需要游戏
dist\ChameleonESP.exe probe        :: 探测 + 结构自检
dist\ChameleonESP.exe esp          :: 启动叠加层（也可以直接双击）
dist\ChameleonESP.exe --help
@@@

exe 首次运行会在**自己旁边**生成三个文件：@@config.json@@、@@filters.json@@、@@offsets.json@@
（程序用 @@app_dir()@@ 定位 exe 目录，不会写进 PyInstaller 的临时解包目录）。
所以可以把 exe 放到任意目录，配置跟着走。

无控制台参数地双击 = 直接进 @@esp@@ 模式。

### 12.2 重新打包

@@@
build_exe.bat                      :: 一键重新打包（自动检测/安装 PyInstaller）
:: 或手动：
python -m PyInstaller --clean --noconfirm ChameleonESP.spec
@@@

@@ChameleonESP.spec@@ 里做了三件事：

1. @@uac_admin=True@@ —— exe 清单里声明需要管理员权限，双击自动弹 UAC，
   不用再右键“以管理员身份运行”；
2. 排除 numpy / pandas / scipy / tkinter / unittest 等用不到的大块头，
   体积从 40+ MB 压到 13.6 MB；
3. 只保留 Pillow（用于 @@--screenshot@@ 落图），其余全用标准库。

### 12.3 验证打包结果

@@@
python tools\verify_exe.py         :: 构建一个临时 exe，跑通 selftest/--help 后自动清理
python tools\verify_exe.py --keep  :: 保留产物用于排查
@@@

另外，exe 运行时的权限处理是**按需弹 UAC**：默认不提权启动，只有在读游戏进程被拒绝
（错误码 5）时才自动请求管理员权限并重启自己；从命令行手动加 @@--elevate@@ 可以强制提权。

### 12.4 打包注意事项

* **杀软误报**：PyInstaller 单文件 exe 是自解压结构，行为上像打包器，
  部分杀软会提示。exe 没有做数字签名（@@Get-AuthenticodeSignature@@ 返回 @@NotSigned@@），
  介意的话可以买证书做代码签名，或改用 @@--onedir@@ 目录模式（见下）。
* **目录模式**：把 spec 里的 @@EXE(...)@@ 换成 @@EXE(... exclude_binaries=True) + COLLECT(...)@@
  即可得到 @@dist\ChameleonESP\@@ 目录，启动更快、误报更少，代价是要带着整个目录走。
* **体积**：13.6 MB 主要来自 Python 运行时 + Pillow。想更小可以去掉 Pillow
  （代价是 @@--screenshot@@ 不可用），能再省 3~4 MB。
* **首次启动**：单文件 exe 每次启动要解包到 @@%TEMP%\_MEIxxxx@@，约 1~2 秒；
  之后常驻内存不受影响。想秒开就用目录模式。

---

## 11. 说明

- 只做**只读**内存访问：没有 WriteProcessMemory、没有 DLL 注入、没有修改游戏文件
  （`mem.py` 里保留了 `write()` 接口，但主流程从不调用）。
- 目标目录里不存在 EasyAntiCheat / BattlEye / Denuvo 相关模块（已全盘扫描确认），
  网络层是 Steamworks + EOS。
- 叠加层是独立进程的透明窗口，不改变游戏本身的渲染。
- 仓库里**不包含**任何游戏资源：没有 pak/utoc、没有提取出的字符串清单，
  `tools/scan_strings.py` 只是分析脚本本身。