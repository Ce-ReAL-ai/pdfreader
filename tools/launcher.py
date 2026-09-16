# -*- coding: utf-8 -*-
"""launcher.py — 双击即用的打包入口（PyInstaller 的 entry point）。

做的事，按顺序：
  1. 把缓存根定位到「exe 同级目录/cache」（打包版）或项目根目录（源码运行）；
  2. 挑一个端口（默认 8765，被占用就往后试）；
  3. 决定打开哪本书：命令行 --pdf > 上次用过的书（仍存在时）> books/ 里唯一一本
     > 交互式列出 books/ 让用户选 > 不指定（进网页后再「切换文档」）；
  4. 起服务器，并自动打开系统浏览器。

用法（打包后双击 exe 等价于不带参数运行）：
    阅读器.exe                       选书 → 起服务 → 开浏览器
    阅读器.exe --pdf "D:\\书.pdf"    直接打开指定 PDF
    阅读器.exe --books D:\\mybooks   指定放书的目录
    阅读器.exe --port 8766 --no-open 换端口 / 不自动开浏览器
"""
import argparse
import json
import os
import socket
import sys
from pathlib import Path

PDF_SUFFIX = ".pdf"


def _force_utf8_console() -> None:
    """Windows 控制台默认 GBK，中文文件名/提示会变乱码；统一按 UTF-8 输出。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", write_through=True)
        except Exception:  # noqa: BLE001
            pass
    # 输出被重定向时 Python 会整块缓冲，用户看不到「缓存目录/正在建缓存」这些提示
    os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _base_dir() -> Path:
    """exe 所在目录（打包版）或项目根目录（源码运行）——可写数据的落脚点。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _pick_port(preferred: int, tries: int = 20) -> int:
    """从 preferred 开始找一个能监听的端口；全被占用就返回 preferred 让服务器报错。"""
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def _last_pdf(cache_root: Path):
    """上次用的文档；路径在别人电脑上失效时返回 None（缓存仍是好的）。"""
    state = cache_root / "_last.json"
    try:
        pdf = json.loads(state.read_text(encoding="utf-8")).get("pdf")
    except Exception:
        return None
    if pdf and Path(pdf).is_file():
        return Path(pdf)
    return None


def _books_dir(books_arg) -> Path:
    return Path(books_arg).expanduser().resolve() if books_arg else _base_dir() / "books"


def _writable(path: Path) -> bool:
    """能不能在 path 里写文件（程序放在只读盘/Program Files 时不能）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_cache_root(base: Path, override) -> Path:
    """缓存根：命令行 > exe 同级 cache/ > 用户目录（同级不可写时）。"""
    if override:
        root = Path(override).expanduser().resolve()
        if not _writable(root):
            print(f"[错误] 缓存目录不可写: {root}")
            raise SystemExit(3)
        return root

    beside = base / "cache"
    if _writable(beside):
        return beside

    fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PDFReader" / "cache"
    print(f"[提示] 程序目录不可写，缓存改放到: {fallback}")
    if not _writable(fallback):
        print("[错误] 程序目录和用户目录都不可写，无法建立 OCR 缓存。")
        print("       请把程序解压到有写入权限的目录（例如桌面或 D 盘新建文件夹）。")
        raise SystemExit(3)
    return fallback


def _list_books(books: Path):
    if not books.is_dir():
        return []
    return sorted((p for p in books.iterdir()
                   if p.is_file() and p.suffix.lower() == PDF_SUFFIX),
                  key=lambda p: p.name.lower())


def _pick_pdf_dialog(books: Path):
    """图形化选书：双击运行、控制台拿不到输入时的兜底。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:  # noqa: BLE001
        return None
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.update()
        chosen = filedialog.askopenfilename(
            title="选择要阅读的 PDF",
            initialdir=str(books if books.is_dir() else _base_dir()),
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        return Path(chosen) if chosen else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass


def _choose_pdf(books: Path):
    """没有 --pdf 时的挑书逻辑：

    1 本 → 直接用；多本 → 控制台列编号选；控制台不可用 → 弹文件选择框；
    都失败 → 返回 None，进网页后用「切换文档」。
    """
    found = _list_books(books)
    if not found:
        print(f"[提示] 没有在 {books} 里找到 PDF。")
        print("       把书放进这个文件夹再双击本程序，或在网页右上角「切换文档」。")
        return None
    if len(found) == 1:
        print(f"唯一一本书: {found[0].name}")
        return found[0]

    picks = _menu_choice(found, books)
    if picks is not None:
        return picks
    print("控制台选不了书，正在弹出文件选择框…（若没看到窗口，看看任务栏是否被本窗口挡住）")
    sys.stdout.flush()
    return _pick_pdf_dialog(books)


def _menu_choice(found, books: Path):
    """控制台编号选书；返回 None 表示「没选」或「控制台不可用」。"""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None          # 管道/重定向输入，别傻等，直接走弹窗
    except (AttributeError, ValueError):
        return None
    print(f"\n在 {books} 里找到 {len(found)} 本书：")
    for i, p in enumerate(found, 1):
        print(f"  [{i}] {p.name}")
    sys.stdout.flush()
    try:
        raw = input("\n输入编号直接打开（回车=稍后在网页里选）：").strip()
    except (EOFError, OSError, RuntimeError):
        # 没有可用 stdin 的宿主（EOF/非交互）：交给图形化选书
        print()
        return None
    except KeyboardInterrupt:
        print()
        raise SystemExit(0)
    if not raw:
        return None
    try:
        idx = int(raw)
        if 1 <= idx <= len(found):
            return found[idx - 1]
    except ValueError:
        pass
    print("[提示] 没看懂这个编号，改成在网页里「切换文档」。")
    return None


def main(argv=None) -> int:
    _force_utf8_console()
    base = _base_dir()
    ap = argparse.ArgumentParser(description="PDF 快速检索阅读器（本地网页版）",
                                 add_help=True)
    ap.add_argument("--pdf", help="直接打开这个 PDF")
    ap.add_argument("--books", help="放 PDF 的目录（默认：程序同级的 books/）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--gpu", action="store_true", help="使用 CUDA GPU 加速 OCR")
    ap.add_argument("--cache-root", help="缓存目录（默认：程序同级的 cache/）")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-auto-build", action="store_true",
                    help="不自动建缓存，只用已有缓存")
    args, unknown = ap.parse_known_args(argv)

    if getattr(sys, "frozen", False):
        sys.path.insert(0, str(base))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from tools import pdfpipeline, serve  # noqa: E402  （须在 sys.path 就绪后导入）

    cache_root = _resolve_cache_root(base, args.cache_root)
    # 两个模块各持一份 CACHE_ROOT：必须都改，否则 build() 会往只读的 _MEIPASS 里写
    serve.set_cache_root(cache_root)
    pdfpipeline.set_cache_root(cache_root)
    print("=" * 58)
    print("  PDF 快速检索阅读器 —— 首次打开某本书会自动建立 OCR 缓存")
    print("=" * 58)
    print(f"程序目录: {base}")
    print(f"缓存目录: {cache_root}")
    sys.stdout.flush()

    if args.pdf:
        pdf = Path(args.pdf).expanduser()
        if not pdf.is_file():
            print(f"[错误] 找不到文件: {pdf}")
            return 2
    else:
        pdf = _last_pdf(cache_root)
        if pdf:
            print(f"继续上次的文档: {pdf}")
        else:
            pdf = _choose_pdf(_books_dir(args.books))

    port = _pick_port(args.port)
    if port != args.port:
        print(f"[提示] 端口 {args.port} 被占用，改用 {port}。")

    passthrough = ["--port", str(port), "--dpi", str(args.dpi),
                   "--cache-root", str(cache_root)]
    if pdf is not None:
        passthrough += ["--pdf", str(pdf)]
    if args.gpu:
        passthrough.append("--gpu")
    if args.no_auto_build:
        passthrough.append("--no-auto-build")
    if not args.no_open:
        passthrough.append("--open")

    try:
        serve.main(passthrough)
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
