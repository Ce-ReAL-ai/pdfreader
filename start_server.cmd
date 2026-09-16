@echo off
rem ============================================================
rem  start_server.cmd - launch the local PDF search reader
rem  Usage: start_server.cmd --pdf "D:\docs\your.pdf" [--dpi 200] [--gpu]
rem  NOTE: keep this file ASCII-only. A Chinese comment saved as UTF-8 is
rem        mis-decoded under the default GBK console codepage on Windows,
rem        and the leftover bytes can swallow the command on the next line.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" tools\serve.py --open %*
pause
