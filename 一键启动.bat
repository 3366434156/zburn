@echo off
chcp 65001 >nul
title zburn 一键启动
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python，请先安装 Python 3.10+ 并勾选 Add to PATH
  pause & exit /b 1
)

echo ============================================
echo   zburn 一键启动：网关 -^> 守护 -^> 看门狗 -^> 面板
echo ============================================
echo.

if exist config.json (
  python zburn.py --config config.json up
) else (
  echo [提示] 没有 config.json，用内置默认配置启动
  python zburn.py up
)

echo.
echo 面板已退出（关掉这个窗口不影响后台守护进程）
pause
