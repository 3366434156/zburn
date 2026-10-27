@echo off
chcp 65001 >nul
title 只启动网关
cd /d "%~dp0"
if exist config.json (
  python zburn.py --config config.json gateway
) else (
  python zburn.py gateway
)
pause
