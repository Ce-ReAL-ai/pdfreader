# -*- mode: python ; coding: utf-8 -*-
"""pdfreader.spec — 把 PDF 检索阅读器打成「解压即用」的 Windows 绿色包（onedir）。

为什么是 onedir 而不是 onefile：
  本程序带 onnxruntime / opencv / pymupdf，解压后 400+ MB。onefile 每次启动都要把
  这些解压到临时目录，冷启动多等 5~15 秒，且极易被杀软误报；onedir 冷启动 2~3 秒。

打包内容分工：
  只读资源（web/、pdfjs 渲染库）→ 进 _internal/，随包发布
  可写数据（cache/  OCR 缓存）  → 放在 exe 同级目录，由 package.cmd 预置、用户可增删

用法：  pyinstaller pdfreader.spec --noconfirm
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(os.environ.get("PDFREADER_ROOT") or SPECPATH).resolve()
DIST_NAME = "PDFReader"

# ---------------------------------------------------------------- 只读资源
datas = []

# 前端（index.html / app.js / style.css）
datas.append((str(ROOT / "web"), "web"))


def _pdfjs_files():
    """只要 pdf.js 真正用到的文件，别把 70 MB 全搬进来。

    app.js 实际请求的是 /vendor/pdfjs/build/pdf.mjs、pdf.worker.mjs，
    外加 cmaps/、standard_fonts/、wasm/ 三个数据目录。build/ 里的 .map 有 7 MB，
    是调试用的源码映射，运行时用不到，直接排除。
    """
    pkg = ROOT / "node_modules" / "pdfjs-dist"
    if not (pkg / "build" / "pdf.mjs").exists():
        raise SystemExit(
            f"缺少前端渲染库: {pkg / 'build' / 'pdf.mjs'} 不存在。\n"
            "请先在项目根目录执行:  npm install"
        )
    out = []
    for sub in ("build", "cmaps", "standard_fonts", "wasm"):
        folder = pkg / sub
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix == ".map":            # 调试映射，排除
                continue
            rel = f.relative_to(pkg)
            out.append((str(f), str(Path("node_modules") / "pdfjs-dist" / rel.parent)))
    return out


datas += _pdfjs_files()

# OCR 引擎的模型权重与配置（rapidocr_onnxruntime 内部按包目录相对路径读取，必须带上）
datas += collect_data_files("rapidocr_onnxruntime", includes=["**/*.onnx", "**/*.yaml"])
# jieba 的分词词典（dict.txt / idf.txt）
datas += collect_data_files("jieba", includes=["**/dict.txt", "**/idf.txt"])

# ---------------------------------------------------------------- 分析
hiddenimports = [
    # 本项目的包（tools/ 是命名空间包，显式列出更稳）
    "tools.launcher",
    "tools.serve",
    "tools.pdfpipeline",
    "tools.index",
    "tools.build_cache",
    # 图形化选书兜底：tkinter 在 launcher 里是「用到才 import」，静态分析抓不到，
    # 必须显式声明，否则打包版会在控制台不可用时静默地弹不出选书框
    "tkinter",
    "tkinter.filedialog",
    "_tkinter",
    # 常用隐式依赖
    "jieba",
    "yaml",
    "pyclipper",
    "shapely",
    "PIL",
    "cv2",
    "onnxruntime",
]
excludes = [
    "matplotlib", "IPython", "pytest", "notebook",
    "PyQt5", "PySide2", "PySide6", "wx",
]

a = Analysis(
    [str(ROOT / "tools" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF阅读器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # UPX 压 onnxruntime 的 DLL 容易被杀软误报，关掉
    console=True,          # 保留控制台：能看 OCR 进度、给用户明确的错误提示
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=DIST_NAME,
)
