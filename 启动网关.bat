@echo off
chcp 65001 >nul
title 只启动网关
cd /d "%~dp0"
if exist config.json (
  python zburn.py gateway --config config.json
) else (
  python zburn.py gateway
)
pause
