@echo off
rem 命令行 OCR 建缓存（不启动界面）
rem 用法: start_build.cmd --pdf "D:\docs\your.pdf" [--dpi 200] [--workers 4]
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" tools\build_cache.py %*
pause
