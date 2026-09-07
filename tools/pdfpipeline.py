# -*- coding: utf-8 -*-
"""
pdfpipeline.py — OCR 文字层缓存管线（核心）

对一个大 PDF 做“预扫描 + 逐页 OCR”，产出检索用侧车缓存：

    cache/<docid>/
        meta.json        文档元信息、构建统计
        progress.json    构建进度（服务器轮询用）
        index.json       检索倒排索引（由 index.py 生成）
        pages/p00001.json ... 每页一个 {text, regions(命中框), ...}

页面产物 schema（page_XXXXX.json）:
    {
      "n": 页码(1基),
      "pt": [宽, 高]            // PDF 点坐标（72dpi），region 坐标同单位
      "mode": "ocr"|"native",   // ocr=RapidOCR; native=原PDF自带文字层
      "text": "该页全部文字（按阅读顺序拼接，含字符偏移映射）",
      "regions": [
         {"x0":..,"y0":..,"x1":..,"y1":.., "t":"文本块", "o":起始字符偏移, "s":置信度(ocr)}
      ]
    }

region 坐标统一为 PDF 点坐标，前端用 pdf.js viewport 变换后直接画高亮框。
"""
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = ROOT / "cache"
SCHEMA_VERSION = 2

# ---------------------------------------------------------------- 公共工具
def sha_docid(pdf_path: Path, chunk=1 << 20) -> str:
    """取文件内容前 4MB 的 sha256 前 16 位作为稳定 docid"""
    h = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        remain = 4 * chunk
        while remain > 0:
            b = f.read(min(chunk, remain))
            if not b:
                break
            h.update(b)
            remain -= len(b)
    return h.hexdigest()[:16]


def cache_dir_for(pdf_path: Path, root: Path = CACHE_ROOT) -> Path:
    return root / sha_docid(pdf_path)


def _cjk_char(c: str) -> bool:
    o = ord(c)
    return (0x3400 <= o <= 0x4DBF) or (0x4E00 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) \
        or (0x2E80 <= o <= 0x2EFF) or (0x3000 <= o <= 0x303F)


def _needs_space(a: str, b: str) -> bool:
    """两个相邻文本块之间是否需要空格（仅当左右都是 ASCII 词字符时）"""
    if not a or not b:
        return False
    la, fb = a[-1], b[0]
    return la.isascii() and la.isalnum() and fb.isascii() and fb.isalnum()


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- 进度管理
class Progress:
    def __init__(self, cache: Path):
        self.cache = cache
        self.lock = threading.Lock()
        self.state = {"state": "idle", "done": 0, "total": 0, "ocr_done": 0,
                      "native_done": 0, "cur_page": 0, "avg_ms": 0.0,
                      "eta_s": 0.0, "dpi": 0, "phase": "", "error": None,
                      "started_at": None}
        self._load()

    def _load(self):
        p = self.cache / "progress.json"
        if p.exists():
            try:
                self.state.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass

    def set(self, **kw):
        with self.lock:
            self.state.update(kw)
            self._save_locked()

    def get(self):
        with self.lock:
            return dict(self.state)

    def _save_locked(self):
        try:
            _write_json(self.cache / "progress.json", self.state)
        except Exception:
            pass


# ---------------------------------------------------------------- 原生文字页
def _native_page_regions(page) -> list:
    """对自带文字层的页：按 dict 结构的 block/line 抽出行文本块（带 bbox）"""
    regions = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            parts = [s.get("text", "") for s in line.get("spans", [])]
            text = "".join(parts).strip()
            if not text:
                continue
            x0, y0, x1, y1 = line["bbox"]
            regions.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "t": text, "s": 1.0})
    return regions


# ---------------------------------------------------------------- OCR 部分
_TL = threading.local()          # 每线程独立 OCR 引擎（onnx 推理释放 GIL，可线程并行）
INTRA_THREADS = 0                # 每引擎 CPU 推理线程数；0=onnxruntime 默认(全核)
GPU_ENABLED = False              # True=用 CUDA ExecutionProvider


def _tuned_config_path(intra: int, gpu: bool) -> str:
    """生成定制 rapidocr 配置：统一设置 intra 线程数；gpu=True 时开 use_cuda"""
    import yaml
    import rapidocr_onnxruntime as ro_pkg
    pkg_dir = Path(ro_pkg.__file__).parent
    cfg_dir = CACHE_ROOT / "_rapidocr_cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    name = f"config_gpu{1 if gpu else 0}_intra{intra}.yaml"
    out = cfg_dir / name
    if not out.exists():
        cfg = yaml.safe_load((pkg_dir / "config.yaml").read_text(encoding="utf-8"))
        for sec in ("Det", "Cls", "Rec"):
            if sec not in cfg:
                continue
            if intra > 0:
                cfg[sec]["intra_op_num_threads"] = intra
            if gpu:
                cfg[sec]["use_cuda"] = True
                cfg[sec]["use_dml"] = False
        out.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return str(out)


def _add_nvidia_dll_dirs():
    """Windows：把 venv 里 nvidia-* pip 包的 bin 目录加入 PATH 与 DLL 搜索路径，
    使 onnxruntime CUDA provider 能找到 cublas/cudnn/cudart。"""
    if os.name != "nt":
        return
    if getattr(_TL, "cuda_paths_added", False):
        return
    site = Path(sys.prefix) / "Lib" / "site-packages"
    nv = site / "nvidia"
    bins = []
    if nv.exists():
        bins = [d.resolve() for d in nv.glob("*/bin") if d.is_dir()]
    if bins:
        paths = [str(b) for b in bins]
        cur = os.environ.get("PATH", "")
        os.environ["PATH"] = ";".join(paths + [cur])
        for b in bins:
            try:
                os.add_dll_directory(str(b))
            except Exception:
                pass
    _TL.cuda_paths_added = True


def _ensure_engine():
    eng = getattr(_TL, "engine", None)
    if eng is None:
        intra = INTRA_THREADS or int(os.environ.get("PDFOCR_INTRA", "0") or 0)
        gpu = GPU_ENABLED or os.environ.get("PDFOCR_GPU", "0") == "1"
        if gpu:
            _add_nvidia_dll_dirs()
        from rapidocr_onnxruntime import RapidOCR
        if intra > 0 or gpu:
            eng = RapidOCR(config_path=_tuned_config_path(intra, gpu))
        else:
            eng = RapidOCR()
        _TL.engine = eng
    return eng


def _ocr_boxes_from_bytes(png_bytes: bytes, min_score: float):
    """返回原始检测框列表 [dict(x0..y1,t,s)]，坐标为像素"""
    engine = _ensure_engine()
    result, _ = engine(png_bytes)
    out = []
    if not result:
        return out
    for box, text, score in result:
        try:
            text = (text or "").strip()
        except Exception:
            text = ""
        if not text or float(score) < min_score:
            continue
        pts = box if isinstance(box, list) else box.tolist()
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        out.append({"x0": float(min(xs)), "y0": float(min(ys)),
                    "x1": float(max(xs)), "y1": float(max(ys)),
                    "t": text, "s": float(score)})
    return out


def _sort_regions_reading_order(regions: list) -> list:
    """把检测框按视觉行归组并按(行→左→右)排序，得到近似阅读顺序。
    单栏为主；双栏文档此近似仍可按页内各自行序错排，后续可加版面分析。"""
    if not regions:
        return []
    hs = sorted(r["y1"] - r["y0"] for r in regions)
    med_h = hs[len(hs) // 2]
    step = max(med_h * 0.55, 3.0)
    rows = {}
    for r in regions:
        cy = (r["y0"] + r["y1"]) / 2
        k = int(cy // step)
        rows.setdefault(k, []).append(r)
    ordered = []
    for k in sorted(rows):
        row = sorted(rows[k], key=lambda r: r["x0"])
        # 若行内出现巨大横向回退（罕见），按原 y 兜底
        ordered.extend(row)
    return ordered


def _compose_text_with_offsets(regions: list) -> tuple:
    """把阅读顺序的 regions 拼接为连续 text，并为每个 region 记录起始偏移 o。
    规则：左右都是 ASCII 词字符时插入空格，其余直接相连（中文无空格利于子串检索）。
    """
    text = ""
    offsets = []
    for i, r in enumerate(regions):
        t = r["t"]
        if i > 0:
            prev = regions[i - 1]["t"]
            if _needs_space(prev, t):
                text += " "
        offsets.append(len(text))
        text += t
    return text, offsets


def _flip_regions_y(regions: list, H_pt: float) -> None:
    """把 region 坐标从 PyMuPDF 的“左上角原点、Y 向下”转换为 PDF 用户空间
    “左下角原点、Y 向上”（pdf.js convertToViewportRectangle 期望的坐标系）。
    原地修改。x 轴两坐标系同向，无需处理。"""
    for r in regions:
        y0, y1 = r["y0"], r["y1"]
        r["y0"] = H_pt - y1
        r["y1"] = H_pt - y0


def _ocr_page_to_regions(png_bytes: bytes, zoom: float, min_score: float):
    raw = _ocr_boxes_from_bytes(png_bytes, min_score)
    regions = []
    for r in raw:
        regions.append({
            "x0": r["x0"] / zoom, "y0": r["y0"] / zoom,
            "x1": r["x1"] / zoom, "y1": r["y1"] / zoom,
            "t": r["t"], "s": r["s"],
        })
    ordered = _sort_regions_reading_order(regions)
    return ordered


def _render_page_png(page, dpi: int) -> bytes:
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    return pix.tobytes("png")


# 延迟导入 PDF 渲染库，避免 serve.py 无谓加载
try:
    import pymupdf as fitz  # PyMuPDF >=1.24 新命名
except ImportError:  # pragma: no cover
    import fitz


# ---------------------------------------------------------------- 页面级构建
def _build_one_page(pdf_path: Path, pno: int, dpi: int, min_score: float,
                    force_ocr: bool = False) -> dict:
    """处理单个页面：返回 page schema 或抛异常（含 pdf 内页索引 pno 1 基）"""
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[pno - 1]
        rect = page.rect
        w, h = float(rect.width), float(rect.height)
        if not force_ocr:
            native = _native_page_regions(page)
            text0, offs0 = _compose_text_with_offsets(native)
            if len(text0) >= 8:  # 自带的文字确实存在
                for r, o in zip(native, offs0):
                    r["o"] = o
                out = {"n": pno, "pt": [w, h], "mode": "native", "text": text0,
                       "regions": native, "words": len(text0)}
                _flip_regions_y(out["regions"], h)
                return out
        png = _render_page_png(page, dpi)
    finally:
        doc.close()
    regions = _ocr_page_to_regions(png, dpi / 72.0, min_score)
    _flip_regions_y(regions, h)
    text, offs = _compose_text_with_offsets(regions)
    for r, o in zip(regions, offs):
        r["o"] = o
    return {"n": pno, "pt": [w, h], "mode": "ocr", "text": text,
            "regions": regions, "words": len(text)}


def _worker_call(job):
    """进程池任务入口（必须模块级才能被 Windows spawn pickle）"""
    pdf, pno, dpi, min_score = job
    return pno, _build_one_page(Path(pdf), pno, dpi, min_score, force_ocr=True)


def _ocr_job_runner(pdf, pno, dpi, min_score):
    """thread 后端任务：返回 (pno, page_data) 或 (pno, None)"""
    try:
        return pno, _build_one_page(Path(pdf), pno, dpi, min_score, force_ocr=True)
    except Exception as e:  # noqa: BLE001
        print(f"[build] ocr page {pno} error: {e!r}", flush=True)
        return pno, None


def _run_ocr_backend(backend: str, pdf, missing_text, dpi, min_score, workers,
                     results, pages_dir, progress, total, t0):
    """按 backend 并行 OCR 缺失文字层的页，实时写页 JSON 与进度"""
    from concurrent.futures import as_completed
    if workers <= 0:
        workers = min(os.cpu_count() or 2, 6)
    jobs = [(pdf, pno, dpi, min_score) for pno in missing_text]

    if backend == "thread":
        from concurrent.futures import ThreadPoolExecutor
        # 提示各线程先各自初始化引擎，避免任务开始后扎堆
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_ocr_job_runner, *j) for j in jobs]
            for fut in as_completed(futs):
                pno, page_data = fut.result()
                _collect_page(pno, page_data, results, pages_dir, progress, total, t0)
    else:  # proc（用户直接跑真实批处理时默认）
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_worker_call, j) for j in jobs]
            for fut in as_completed(futs):
                try:
                    pno, page_data = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"[build] worker error: {e!r}", flush=True)
                    continue
                _collect_page(pno, page_data, results, pages_dir, progress, total, t0)


def _collect_page(pno, page_data, results, pages_dir, progress, total, t0):
    results[pno] = page_data
    if page_data is not None:
        _write_json(pages_dir / f"p{pno:05d}.json", page_data)
    done = len(results)
    elapsed = time.time() - t0
    avg = elapsed / max(done, 1) * 1000
    eta = avg * (total - done) / 1000
    progress.set(done=done, cur_page=pno, avg_ms=round(avg, 1), eta_s=round(eta, 1))


# ---------------------------------------------------------------- 整本构建
def build_cache(pdf_path: Path, dpi: int = 200, workers: int = 0,
                min_score: float = 0.45, progress: Progress = None,
                force_rebuild: bool = False, backend: str = "proc",
                intra_threads: int = 0, gpu: bool = False) -> Path:
    """构建整本缓存（含逐页 JSON + meta）。返回 cache 目录。
    workers<=0 时用 min(os.cpu_count(), 6)。
    backend: 'proc'=进程池（默认，适合直接跑大文件）；
             'thread'=线程池（受限环境/服务器内部构建用，避免命名管道）。
    intra_threads: 每个 OCR 引擎的 CPU 推理线程数；0 表示自动(≈cpu/workers)。
    gpu: True=用 CUDA（需安装 onnxruntime-gpu 且 GPU 驱动可用）。"""
    pdf_path = pdf_path.resolve()
    docid = sha_docid(pdf_path)
    cache = CACHE_ROOT / docid
    pages_dir = cache / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    if progress is None:
        progress = Progress(cache)

    # 先扫描元数据（页数/尺寸/是否缺文字层）
    progress.set(state="scan", phase="扫描 PDF 页数与文字层情况", done=0, total=0, error=None)
    with fitz.open(str(pdf_path)) as doc:
        total = doc.page_count
        missing_text: list = []      # 需要 OCR 的页码
        native_pages: list = []      # 自带文字层页码
        pg_sizes = {}
        for i in range(total):
            pno = i + 1
            has_text = len(doc[pno - 1].get_text("words")) > 0
            (native_pages if has_text else missing_text).append(pno)
            pg_sizes[pno] = [float(doc[pno - 1].rect.width), float(doc[pno - 1].rect.height)]

    st = time.time()
    progress.set(state="ocr", total=total, done=0, cur_page=0,
                 phase=f"OCR {len(missing_text)} 页 + 原生文字 {len(native_pages)} 页 (dpi={dpi})",
                 dpi=dpi, started_at=time.time())
    print(f"[build] 总页数 {total}，需 OCR {len(missing_text)} 页，自带文字 {len(native_pages)} 页",
          flush=True)

    results: dict = {}
    t0 = time.time()
    # ---- 断点续跑：页数/DPI/置信度一致时复用已有页缓存，只补缺失页 ----
    try:
        old_meta = json.loads((cache / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        old_meta = None
    can_resume = (not force_rebuild and old_meta
                  and old_meta.get("schema") == SCHEMA_VERSION
                  and old_meta.get("pages") == total
                  and old_meta.get("dpi") == dpi
                  and old_meta.get("min_score", min_score) == min_score)
    if can_resume:
        for p in sorted(pages_dir.glob("p*.json")):
            pno = int(p.stem[1:])
            if 1 <= pno <= total:
                try:
                    results[pno] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
        reused = len(results)
        missing_text = [p for p in missing_text if p not in results]
        native_pages = [p for p in native_pages if p not in results]
        if reused:
            print(f"[build] 复用已缓存 {reused}/{total} 页，需补 "
                  f"{len(missing_text) + len(native_pages)} 页", flush=True)
            progress.set(state="ocr", total=total, done=reused, cur_page=0,
                         phase=f"复用 {reused} 页，剩余 OCR {len(missing_text)} 页 "
                               f"(dpi={dpi})", dpi=dpi, started_at=time.time())
    # 解析并发与每引擎线程数（CPU 不超订）
    if workers <= 0:
        workers = min(os.cpu_count() or 2, 6)
    if gpu:
        workers = min(workers, 4)  # GPU 上并发过多反而被显存/上下文拖累
    if intra_threads <= 0:
        intra_threads = max(1, (os.cpu_count() or 2) // workers) if workers > 1 else 0
    if intra_threads > 0:
        os.environ["PDFOCR_INTRA"] = str(intra_threads)
    if gpu:
        os.environ["PDFOCR_GPU"] = "1"
    global INTRA_THREADS, GPU_ENABLED
    GPU_ENABLED = gpu and backend == "thread"   # 线程池直接传模块全局
    INTRA_THREADS = intra_threads if backend == "thread" else 0  # 子进程由 env 传递
    # 原生文字页（快）主进程直接做
    if native_pages:
        for pno in native_pages:
            try:
                page_data = _build_one_page(pdf_path, pno, dpi, min_score)
                results[pno] = page_data
                _write_json(pages_dir / f"p{pno:05d}.json", page_data)
            except Exception as e:  # noqa: BLE001
                print(f"[build] native page {pno} error: {e!r}", flush=True)
                results[pno] = None
            done = len(results)
            progress.set(done=done, cur_page=pno, native_done=done)

    if missing_text:
        _run_ocr_backend(backend, str(pdf_path), missing_text, dpi, min_score,
                         workers, results, pages_dir, progress, total, t0)

    if any(v is None for v in results.values()):
        failed = [p for p, v in results.items() if v is None]
        progress.set(state="error", error=f"以下页面处理失败: {failed[:20]}")
        raise RuntimeError(f"部分页面处理失败: {failed[:20]}")

    # meta
    ocr_pages = [p for p, v in results.items() if v and v["mode"] == "ocr"]
    meta = {
        "schema": SCHEMA_VERSION,
        "docid": docid,
        "src": str(pdf_path),
        "src_size": pdf_path.stat().st_size,
        "pages": total,
        "page_sizes_pt": pg_sizes,
        "dpi": dpi,
        "min_score": min_score,
        "engine": "rapidocr-onnxruntime-1.4.4",
        "coord_space": "pdf",
        "built_at": time.time(),
        "build_seconds": round(time.time() - t0, 1),
        "ocr_pages": len(ocr_pages),
        "native_pages": len(native_pages),
        "indexed": False,
    }
    _write_json(cache / "meta.json", meta)
    progress.set(state="done", phase="OCR 与索引完成", done=total, total=total,
                 error=None)
    return cache


def load_meta(cache: Path) -> dict | None:
    p = cache / "meta.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def convert_cache_to_pdf_space(cache: Path) -> int:
    """把旧缓存 region 坐标升级为 PDF 用户空间（左下原点/Y 向上）。
    仅 Y 轴翻转一次，幂等（meta.coord_space=pdf 后跳过）。返回处理页数。"""
    meta = load_meta(cache)
    if meta and meta.get("coord_space") == "pdf":
        return 0
    n = 0
    for p in sorted((cache / "pages").glob("p*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            pt = d.get("pt") or [1.0, 1.0]
            h = float(pt[1]) if len(pt) > 1 and pt[1] else 1.0
            _flip_regions_y(d.get("regions", []), h)
            _write_json(p, d)
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  skip {p.name}: {e!r}", flush=True)
    if meta:
        meta["coord_space"] = "pdf"
        _write_json(cache / "meta.json", meta)
    else:
        _write_json(cache / "meta.json", {"coord_space": "pdf"})
    return n


def is_indexed(cache: Path) -> bool:
    meta = load_meta(cache)
    return bool(meta and (cache / "index.json").exists())
