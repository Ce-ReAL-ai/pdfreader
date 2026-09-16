@echo off
chcp 65001 >nul
rem ============================================================
rem  package.cmd - build the share-ready PDFReader bundle
rem  Usage: package.cmd [--zip / --7z / --release] [--all]
rem    --zip      also produce dist\PDFReader-share.zip
rem    --7z       also produce dist\PDFReader-share.7z (smallest, needs 7-Zip)
rem    --release  7z + a separate pdfreader-cache.zip (both go to GitHub Releases)
rem    --all      preload every cache in cache\ (not just the 4 big books)
rem  Shipping to GitHub?  Read release\README.md first: the repo stays
rem  source-only (~250 KB); the archive and cache go to Releases.
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv not found: %PY%
    echo         run:  py -3.10 -m venv .venv
    echo               .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

if not exist "node_modules\pdfjs-dist\build\pdf.mjs" (
    echo [ERROR] frontend render library missing.
    echo         run:  npm install
    exit /b 1
)

"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [1/3] installing PyInstaller ...
    "%PY%" -m pip install pyinstaller || exit /b 1
)

echo [2/3] building with PyInstaller ^(this takes a few minutes^) ...
"%PY%" -m PyInstaller pdfreader.spec --noconfirm --clean || exit /b 1

set "RELEASE_ARGS="
set "BUILD_ARGS="
if /i "%~1"=="--zip" set "BUILD_ARGS=zip"
if /i "%~2"=="--zip" set "BUILD_ARGS=zip"
if /i "%~1"=="--7z" set "BUILD_ARGS=7z"
if /i "%~2"=="--7z" set "BUILD_ARGS=7z"
if /i "%~1"=="--release" set "BUILD_ARGS=7z"
if /i "%~2"=="--release" set "BUILD_ARGS=7z"
if defined BUILD_ARGS set "RELEASE_ARGS=--archive %BUILD_ARGS%"
if /i "%~1"=="--release" set "RELEASE_ARGS=%RELEASE_ARGS% --cache-archive"
if /i "%~2"=="--release" set "RELEASE_ARGS=%RELEASE_ARGS% --cache-archive"
if /i "%~1"=="--all" set "RELEASE_ARGS=%RELEASE_ARGS% --all"
if /i "%~2"=="--all" set "RELEASE_ARGS=%RELEASE_ARGS% --all"

echo [3/3] assembling release folder ^(cache + books + readme^) ...
"%PY%" tools\make_release.py %RELEASE_ARGS% || exit /b 1

echo.
echo Done. Share the folder:  dist\PDFReader
echo Archive (if any) is in dist\.  For GitHub, upload the archive to Releases,
echo not to the repository -- see release\README.md
endlocal
