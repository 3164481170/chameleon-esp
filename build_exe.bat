@echo off
chcp 65001 >nul
title 打包 ChameleonESP
cd /d "%~dp0"

set PY=python
where python >nul 2>&1 || set PY=py

echo [*] 检查 PyInstaller...
%PY% -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [*] 未安装，正在安装 PyInstaller...
    %PY% -m pip install --upgrade pyinstaller
)

echo.
echo [1/2] 打包 ChameleonESP.exe（普通权限，按需弹 UAC）...
%PY% -m PyInstaller --clean --noconfirm ChameleonESP.spec
if errorlevel 1 goto fail

echo.
echo [2/2] 打包 ChameleonESP-Admin.exe（强制管理员）...
%PY% -m PyInstaller --clean --noconfirm ChameleonESP-Admin.spec
if errorlevel 1 goto fail

echo.
echo [√] 打包完成：
dir /b dist\*.exe
echo.
echo   dist\ChameleonESP.exe        双击即用；只在需要读游戏时才弹 UAC
echo   dist\ChameleonESP-Admin.exe  双击直接弹 UAC，适合固定放在游戏目录
echo.
pause
exit /b 0

:fail
echo [!] 打包失败
pause
exit /b 1
