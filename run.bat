@echo off
chcp 65001 >nul
title Chameleon ESP
cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [*] 需要管理员权限，正在请求提权...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set PY=python
where python >nul 2>&1 || set PY=py

echo ============================================
echo   MECCHA CHAMELEON ESP
echo ============================================
echo   1. 启动透视（叠加层）
echo   2. 空跑验证（不画图，只打印目标）
echo   3. 探测 + 类名报告
echo   4. 投影数学自检
echo   5. 定位诊断
echo ============================================
set /p CH=请选择 [1-5, 默认1]:
if "%CH%"=="" set CH=1

if "%CH%"=="1" %PY% chameleon_esp.py esp
if "%CH%"=="2" %PY% chameleon_esp.py esp --dry-run
if "%CH%"=="3" %PY% chameleon_esp.py probe --report reports\classes.md
if "%CH%"=="4" %PY% chameleon_esp.py selftest
if "%CH%"=="5" %PY% tools\diagnose_offsets.py --deep

echo.
pause
