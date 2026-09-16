# -*- coding: utf-8 -*-
"""serve.py — 本地网页版 PDF 检索阅读器服务器（仅 Python 标准库）

单进程多文档：可随时“切换文档”，每个文件按内容哈希自动对应自己的 OCR 缓存
(cache/<docid>/)，首次打开自动后台建缓存（断点续跑），之后秒开。

用法:
    python tools/serve.py --pdf "D:\\docs\\big.pdf" [--port 8765] [--dpi 200] [--workers 4] [--gpu]
    不带 --pdf 时打开上次使用的文档。

浏览器: http://127.0.0.1:8765/
"""
import argparse
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def _bundle_root() -> Path:
    """只读资源根目录。

    - 源码运行：项目根目录（web/ 与 node_modules/ 所在处）
    - PyInstaller 打包：sys._MEIPASS（--add-data 打进去的资源解压根）
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    """可写数据根目录（cache/ 落在 exe 旁边，而不是只读的临时解压目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(_data_root()))

ROOT = _data_root()
WEB_DIR = _bundle_root() / "web"
CACHE_ROOT = ROOT / "cache"
PDFJS_DIR = _bundle_root() / "node_modules" / "pdfjs-dist"
STATE_FILE = CACHE_ROOT / "_last.json"
LIBRARY_FILE = CACHE_ROOT / "_library.json"


def set_cache_root(path) -> Path:
    """把缓存根改到别处（打包版双击运行时用 exe 同级目录）。

    必须在创建 App 之前调用；同时刷新由它派生的两个状态文件路径。
    """
    global CACHE_ROOT, STATE_FILE, LIBRARY_FILE
    CACHE_ROOT = Path(path).resolve()
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_FILE = CACHE_ROOT / "_last.json"
    LIBRARY_FILE = CACHE_ROOT / "_library.json"
    return CACHE_ROOT

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".map": "application/json",
    ".woff2": "font/woff2",
}


def _write_json(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class App:
    """全局状态：文档库 + 当前文档 + 后台构建线程"""

    def __init__(self, pdf, dpi: int, workers: int, auto_build: bool,
                 gpu: bool = False):
        self.dpi = dpi
        self.workers = workers
        self.gpu = gpu
        self.auto_build = auto_build

        from tools import pdfpipeline
        self.pipeline = pdfpipeline

        self._build_thread = None
        self._build_job = None          # (pdf, docid, cache, prog)
        self._build_lock = threading.Lock()
        self._doc_lock = threading.RLock()
        self._search_mod = None

        self.doc = None                 # 当前文档快照 (pdf, docid, cache)
        self.library: list = self._load_library()

        target = pdf if pdf is not None else self._load_last()
        if target is not None:
            p = Path(target)
            if p.exists():
                self.set_current(p, auto_start=auto_build)

    # ------------------------------------------------ 持久化
    @property
    def search_mod(self):
        if self._search_mod is None:
            from tools import index as m
            self._search_mod = m
        return self._search_mod

    def _load_library(self) -> list:
        try:
            if LIBRARY_FILE.exists():
                data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [s for s in data if isinstance(s, str)]
        except Exception:
            pass
        found = []
        if CACHE_ROOT.exists():
            for mf in CACHE_ROOT.glob("*/meta.json"):
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    if m.get("src"):
                        found.append(m["src"])
                except Exception:
                    continue
        return found

    def _save_library(self) -> None:
        _write_json(LIBRARY_FILE, list(dict.fromkeys(self.library))[-200:])

    def _load_last(self):
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("pdf")
        except Exception:
            pass
        return None

    def _save_last(self, pdf: Path) -> None:
        _write_json(STATE_FILE, {"pdf": str(pdf), "docid": self.pipeline.sha_docid(pdf)})

    # ------------------------------------------------ 文档切换
    def _add_to_library(self, pdf: Path) -> None:
        s = str(pdf)
        if s not in self.library:
            self.library.append(s)
            self._save_library()

    def set_current(self, pdf: Path, auto_start=None, raise_if_missing=True) -> dict:
        pdf = pdf.resolve()
        if raise_if_missing and not pdf.exists():
            raise FileNotFoundError(str(pdf))
        docid = self.pipeline.sha_docid(pdf)
        cache = CACHE_ROOT / docid
        with self._doc_lock:
            self.doc = (pdf, docid, cache)
            self._save_last(pdf)
            self._add_to_library(pdf)
        if auto_start is None:
            auto_start = self.auto_build
        if auto_start:
            self.ensure_built(pdf)
        return self.current_meta()

    def _cache_ready(self, cache: Path) -> bool:
        return (cache / "meta.json").exists() and (cache / "index.json").exists()

    def current_meta(self) -> dict:
        with self._doc_lock:
            if self.doc is None:
                return {"ready": False, "building": False, "pages": 0, "file": ""}
            pdf, docid, cache = self.doc
        prog = self._read_progress(cache)
        meta = self.pipeline.load_meta(cache)
        building = self._building_docid() == docid
        return {
            "file": pdf.name, "name": pdf.stem, "path": str(pdf),
            "size": pdf.stat().st_size if pdf.exists() else -1,
            "docid": docid, "cache_dir": str(cache),
            "pages": (meta or {}).get("pages", 0),
            "ocr_pages": (meta or {}).get("ocr_pages", 0),
            "native_pages": (meta or {}).get("native_pages", 0),
            "dpi": (meta or {}).get("dpi"),
            "ready": self._cache_ready(cache),
            "building": building,
            "progress": prog,
        }

    # ------------------------------------------------ 后台构建（同一时刻至多一个）
    def _building_docid(self):
        with self._build_lock:
            if self._build_thread and self._build_thread.is_alive() and self._build_job:
                return self._build_job[1]
            return None

    def building_any(self) -> bool:
        return self._building_docid() is not None

    def ensure_built(self, pdf=None) -> bool:
        with self._doc_lock:
            cur = self.doc
        pdf = pdf or (cur[0] if cur else None)
        if pdf is None:
            return False
        pdf = pdf.resolve()
        docid = self.pipeline.sha_docid(pdf)
        cache = CACHE_ROOT / docid
        if self._cache_ready(cache):
            return False
        with self._build_lock:
            if self._build_thread and self._build_thread.is_alive():
                return False
            from tools.pdfpipeline import Progress
            prog = Progress(cache)
            job = (pdf, docid, cache, prog)
            self._build_job = job
            t = threading.Thread(target=self._build_worker, args=(job,),
                                 daemon=True, name="cache-builder")
            self._build_thread = t
            t.start()
            return True

    def _build_worker(self, job):
        pdf, docid, cache, prog = job
        try:
            from tools.pdfpipeline import build_cache, load_meta
            from tools import index as im
            # 1) OCR 页缓存（断点续跑：已有页自动复用，只补缺失页）
            build_cache(pdf, dpi=self.dpi, workers=self.workers, progress=prog,
                        backend="thread", gpu=self.gpu)
            # 2) 分词索引（缺失才建；逐页汇报进度）
            if not (cache / "index.json").exists():
                meta0 = load_meta(cache) or {}
                n_total = int(meta0.get("pages", 0)) or 1

                def _idx_cb(done, total):
                    prog.set(state="indexing", done=done, total=total,
                             cur_page=done, avg_ms=0.0, eta_s=0.0,
                             phase=f"正在构建分词索引… {done}/{total} 页")

                im.build_index(cache, progress_cb=_idx_cb)
                meta = load_meta(cache)
                if meta:
                    meta["indexed"] = True
                    self.pipeline._write_json(cache / "meta.json", meta)
            prog.set(state="done", phase="缓存与索引就绪", done=1, total=1, error=None)
            print("[serve] cache ready:", cache, flush=True)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            prog.set(state="error", phase="构建失败", error=str(e))

    def _read_progress(self, cache: Path) -> dict:
        p = cache / "progress.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"state": "idle"}

    # ------------------------------------------------ 文档库
    def library_items(self) -> list:
        entries: dict = {}
        for p in self.library:
            entries[p] = self._item_of(p)
        if CACHE_ROOT.exists():
            for mf in CACHE_ROOT.glob("*/meta.json"):
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    src = m.get("src")
                    if src and src not in entries:
                        entries[src] = self._item_of(src)
                except Exception:
                    continue
        items = list(entries.values())
        items.sort(key=lambda x: (0 if x.get("ready") else 1,
                                  str(x.get("name", "")).lower()))
        return items

    def _item_of(self, path: str) -> dict:
        p = Path(path)
        exists = p.exists()
        cache = CACHE_ROOT / self.pipeline.sha_docid(p) if exists else None
        meta = self.pipeline.load_meta(cache) if cache else None
        prog = self._read_progress(cache) if cache else {}
        return {
            "name": p.stem if exists else Path(path).name,
            "path": path,
            "exists": exists,
            "size": p.stat().st_size if exists else -1,
            "pages": (meta or {}).get("pages", 0),
            "ready": bool(cache and self._cache_ready(cache)) if cache else False,
            "building": prog.get("state") in ("ocr", "scan", "indexing"),
            "progress_state": prog.get("state", "idle"),
            "docid": cache.name if cache else None,
        }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: App = None  # type: ignore[assignment]

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str, root: Path):
        try:
            rel = path.lstrip("/")
            fp = (root / rel).resolve()
            fp.relative_to(root.resolve())
        except (ValueError, OSError):
            return self._json({"error": "bad path"}, 400)
        if not fp.exists() or not fp.is_file():
            return self.send_error(404, "not found")
        ctype = MIME.get(fp.suffix.lower(), "application/octet-stream")
        size = fp.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(fp, "rb") as f:
            self.wfile.write(f.read())

    def _range_response(self, fpath: Path, ctype: str):
        size = fpath.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a:
                    start = int(a)
                if b:
                    end = int(b)
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(fpath, "rb") as f:
            f.seek(start)
            remain = length
            while remain > 0:
                chunk = f.read(min(1 << 20, remain))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remain -= len(chunk)

    def _read_body(self, max_bytes=1 << 20) -> dict:
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            if ln <= 0 or ln > max_bytes:
                return {}
            return json.loads(self.rfile.read(ln).decode("utf-8"))
        except Exception:
            return {}

    def _route(self, method: str):
        app = self.app
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            return self._static("index.html", WEB_DIR)
        if path.startswith("/web/"):
            return self._static(path[len("/web/"):], WEB_DIR)
        if path.startswith("/vendor/pdfjs/"):
            return self._static(path[len("/vendor/pdfjs/"):], PDFJS_DIR)

        # ---------- 文档库 / 切换 ----------
        if path == "/api/library":
            return self._json({"current_docid": app.doc[1] if app.doc else None,
                               "items": app.library_items()})

        if path == "/api/pdfs":
            folder = (qs.get("folder") or [""])[0]
            folder = Path(folder).expanduser().resolve()
            if not folder.exists() or not folder.is_dir():
                return self._json({"error": "文件夹不存在", "folder": str(folder)}, 400)
            pdfs = sorted(folder.glob("*.pdf"))[:500]
            return self._json({"folder": str(folder), "count": len(pdfs),
                               "items": [{"name": p.name, "path": str(p),
                                          "size": p.stat().st_size} for p in pdfs]})

        if path == "/api/open" and method == "POST":
            body = self._read_body()
            pdf_str = (body or {}).get("pdf") or (qs.get("pdf") or [""])[0]
            pdf = Path(pdf_str).expanduser()
            if not pdf.exists():
                return self._json({"error": f"文件不存在: {pdf}", "switched": False}, 400)
            try:
                meta = app.set_current(pdf)
                meta["switched"] = True
                return self._json(meta)
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{e!r}", "switched": False}, 500)

        if path == "/api/library/add" and method == "POST":
            body = self._read_body()
            pdf_str = (body or {}).get("pdf") or (qs.get("pdf") or [""])[0]
            pdf = Path(pdf_str).expanduser()
            if not pdf.exists():
                return self._json({"error": f"文件不存在: {pdf}"}, 400)
            app._add_to_library(pdf)
            return self._json({"ok": True, "path": str(pdf)})

        # ---------- 当前文档 ----------
        if path == "/api/meta":
            if app.doc is None:
                return self._json({"ready": False, "building": False,
                                   "file": "", "pages": 0})
            return self._json(app.current_meta())

        if path == "/api/pdf":
            if not app.doc:
                return self._json({"error": "no document"}, 404)
            pdf = app.doc[0]
            if not pdf.exists():
                return self._json({"error": "pdf missing"}, 404)
            return self._range_response(pdf, "application/pdf")

        if path == "/api/search":
            q = (qs.get("q") or [""])[0].strip()
            limit = int((qs.get("limit") or ["80"])[0])
            mode = (qs.get("mode") or ["auto"])[0]
            allow_single = (qs.get("single") or ["0"])[0] in ("1", "true")
            ctx = int((qs.get("ctx") or ["42"])[0])
            if not app.doc:
                return self._json({"error": "no_document"}, 409)
            cache = app.doc[2]
            if not ((cache / "meta.json").exists() and (cache / "index.json").exists()):
                return self._json({"error": "cache_not_ready",
                                   "progress": app._read_progress(cache)}, 409)
            res = app.search_mod.search(cache, q, limit=limit, mode=mode,
                                        allow_single=allow_single, ctx=ctx)
            res["options"] = {"mode": mode, "allow_single": allow_single, "ctx": ctx}
            return self._json(res)

        if path == "/api/page":
            n = int((qs.get("n") or ["1"])[0])
            if not app.doc:
                return self._json({"error": "no document"}, 404)
            p = app.doc[2] / "pages" / f"p{n:05d}.json"
            if not p.exists():
                return self._json({"error": "no page"}, 404)
            data = json.loads(p.read_text(encoding="utf-8"))
            return self._json({"n": n, "text": data.get("text", ""),
                               "regions": data.get("regions", []),
                               "mode": data.get("mode")})

        if path == "/api/cache/build" and method == "POST":
            if not app.doc:
                return self._json({"error": "no document"}, 409)
            if app.building_any() and not app._cache_ready(app.doc[2]):
                return self._json({"started": False, "building": True}, 409)
            started = app.ensure_built()
            return self._json({"started": started, "building": app.building_any()})

        if path == "/api/cache/progress":
            if not app.doc:
                return self._json({"state": "idle"})
            return self._json(app._read_progress(app.doc[2]))

        self.send_error(404)

    def do_GET(self):
        try:
            self._route("GET")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": f"server error: {e!r}"}, 500)
            except Exception:
                pass

    def do_HEAD(self):
        if self.path.startswith("/api/pdf") and self.app.doc:
            pdf = self.app.doc[0]
            if pdf.exists():
                size = pdf.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
        self.do_GET()

    def do_POST(self):
        try:
            self._route("POST")
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"server error: {e!r}"}, 500)

    def log_message(self, fmt, *args):  # 静音访问日志
        pass


def _open_browser(url: str) -> bool:
    """打开系统默认浏览器；失败只提示，不影响服务器继续跑。"""
    try:
        import webbrowser
        if webbrowser.open(url):
            return True
    except Exception as e:  # noqa: BLE001
        print(f"[提示] 自动打开浏览器失败（{e}），请手动访问上面的地址。")
        return False
    print(f"[提示] 未能自动唤起浏览器，请手动访问: {url}")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地 PDF 检索阅读器服务器（多文档）")
    ap.add_argument("--pdf", help="PDF 路径（省略则打开上次文档；网页内可随时切换）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--gpu", action="store_true", help="使用 CUDA GPU 加速 OCR")
    ap.add_argument("--no-auto-build", action="store_true",
                    help="不自动建缓存，仅提供已有缓存")
    ap.add_argument("--cache-root", help="缓存根目录（默认：项目/exe 同级的 cache/）")
    ap.add_argument("--open", dest="open_browser", action="store_true",
                    help="启动后自动打开系统浏览器")
    args = ap.parse_args(argv)

    if args.cache_root:
        set_cache_root(args.cache_root)

    pdf = None
    if args.pdf:
        pdf = Path(args.pdf)
        if not pdf.exists():
            sys.exit(f"文件不存在: {args.pdf}")

    Handler.app = App(pdf, dpi=args.dpi, workers=args.workers,
                      auto_build=not args.no_auto_build, gpu=args.gpu)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"端口 {args.port} 无法监听（{e}）。可能是已有一个阅读器在运行；"
                 f"换一个端口再试：--port {args.port + 1}")
    url = f"http://127.0.0.1:{args.port}/"
    cur = Handler.app.current_meta()
    print(f"阅读器已启动: {url}")
    print(f"当前文档: {cur.get('file') or '(无)'}  就绪={cur.get('ready')}"
          + ("  [GPU加速]" if args.gpu else ""))
    print("网页右上角可“切换文档”；新文件首次打开会自动建立缓存。")
    print("关闭本窗口（或按 Ctrl+C）即退出阅读器。")
    sys.stdout.flush()
    # 前端渲染库自检：缺失时网页会卡在“加载文档…”，给出明确提示而不是无声失败
    if not ((PDFJS_DIR / "build" / "pdf.mjs").exists()
            and (PDFJS_DIR / "build" / "pdf.worker.mjs").exists()):
        print("\n[警告] 前端渲染库缺失：未找到 node_modules/pdfjs-dist "
              "（网页会卡在右上角“加载文档…”，无法显示 PDF 内容）。")
        print("        源码运行请执行:  npm install   然后强刷浏览器（Ctrl+Shift+R）。")
        print("        打包版请检查 _internal 是否完整（不要只拷 exe）。\n")
    if args.open_browser:
        _open_browser(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
