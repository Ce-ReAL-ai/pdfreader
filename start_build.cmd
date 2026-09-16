@echo off
rem ============================================================
rem  start_build.cmd - build the OCR cache only, no UI
rem  Usage: start_build.cmd --pdf "D:\docs\your.pdf" [--dpi 200] [--workers 4]
rem  NOTE: keep this file ASCII-only (see start_server.cmd).
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" tools\build_cache.py %*
pause
