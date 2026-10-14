# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：ChameleonESP-Admin（单文件、免安装 Python）。

    python -m PyInstaller --clean --noconfirm ChameleonESP-Admin.spec

要点：
* 单文件模式：运行时解包到 %TEMP%_MEIxxxx，程序用 app_dir() 定位 exe 目录，
  配置 / offsets.json / filters.json / reports 都写在 exe 旁边，不会被清理；
* 只保留 Pillow（--screenshot 落图用），排除 numpy/pandas/scipy/tkinter 等大块头；
* uac_admin=True —— 清单声明 requireAdministrator，双击直接弹 UAC。
"""

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = collect_submodules("sdk") + ["PIL", "PIL.Image"]

a = Analysis(
    ["chameleon_esp.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "IPython", "jupyter",
        "test", "unittest", "lib2to3", "pydoc_data", "setuptools", "pip",
        "win32com", "win32api", "tkinter", "sqlite3", "xml",
        "asyncio", "concurrent", "multiprocessing",
        "pyinstaller", "PyInstaller",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="ChameleonESP-Admin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)
