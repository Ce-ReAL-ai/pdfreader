@echo off
rem 启动本地检索阅读器（缓存缺失时自动后台 OCR 构建）
rem 用法: start_server.cmd --pdf "D:\docs\your.pdf" [--dpi 200]
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" tools\serve.py %*
pause
