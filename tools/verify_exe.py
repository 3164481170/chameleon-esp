"""打包自验证：构建一个精简 exe，跑通离线用例后自动删除。

用途：改完代码后一条命令确认“打包出来的 exe 真的能跑”，不用手动双击试。
（正式的 dist\\ChameleonESP.exe 由 build_exe.bat / PyInstaller 生成。）
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "build", "verify")
NO_WINDOW = 0x08000000


def run(cmd, cwd=None, timeout=900):
    print("$ " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       creationflags=NO_WINDOW)
    if p.stdout:
        print(p.stdout.strip()[-3000:])
    if p.stderr:
        print("[stderr] " + p.stderr.strip()[-1500:])
    return p.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留构建产物用于排查")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        print("未安装 PyInstaller：python -m pip install pyinstaller")
        return 2

    os.makedirs(WORK, exist_ok=True)
    cmd = [
        sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
        "--onefile", "--console", "--name", "ChameleonESP_verify",
        "--distpath", os.path.join(WORK, "dist"),
        "--workpath", os.path.join(WORK, "build"),
        "--specpath", WORK,
        "--hidden-import", "PIL",
        "--exclude-module", "numpy", "--exclude-module", "pandas",
        "--exclude-module", "matplotlib", "--exclude-module", "scipy",
        "--exclude-module", "tkinter", "--exclude-module", "unittest",
        "--exclude-module", "email", "--exclude-module", "html",
        os.path.join(ROOT, "chameleon_esp.py"),
    ]
    print("== 构建验证用 exe ==")
    if run(cmd) != 0:
        print("构建失败")
        return 1

    exe = os.path.join(WORK, "dist", "ChameleonESP_verify.exe")
    if not os.path.exists(exe):
        print("找不到构建产物：%s" % exe)
        return 1
    print("产物：%s（%.2f MB）" % (exe, os.path.getsize(exe) / 1048576.0))

    fails = []
    print()
    print("== exe: selftest（投影数学，不需要游戏）==")
    rc = run([exe, "selftest"])
    if rc != 0:
        fails.append("selftest 返回 %d" % rc)
    else:
        print(">> selftest 通过")

    print()
    print("== exe: --help ==")
    if run([exe, "--help"]) != 0:
        fails.append("--help 失败")
    else:
        print(">> --help 通过")

    print()
    print("== exe: 无参数启动（应尝试进入 esp，找不到游戏则优雅退出）==")
    run([exe], timeout=180)

    print()
    print("== 启动时在 exe 旁生成的文件 ==")
    dist = os.path.join(WORK, "dist")
    made = [f for f in ("config.json", "filters.json", "offsets.json")
            if os.path.exists(os.path.join(dist, f))]
    print("   " + (", ".join(made) if made else "（无）"))
    if "config.json" not in made:
        fails.append("exe 未能在自己旁边生成 config.json（app_dir 定位有问题）")

    if args.keep:
        print("保留构建目录：%s" % WORK)
    else:
        shutil.rmtree(WORK, ignore_errors=True)
        print("已清理构建目录")

    print()
    if fails:
        print("打包自验证失败：")
        for f in fails:
            print("  - " + f)
        return 1
    print("打包自验证通过：exe 可独立运行，离线用例全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
