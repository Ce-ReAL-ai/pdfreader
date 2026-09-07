# -*- coding: utf-8 -*-
"""build_cache.py — 命令行：对 PDF 构建 OCR 文字层缓存 + 索引

用法:
    python tools/build_cache.py --pdf "D:\\docs\\big.pdf" [--dpi 200] [--workers 4]
    python tools/build_cache.py --pdf big.pdf --pages 3-8     # 只跑样本页看效果
    python tools/build_cache.py --pdf big.pdf --no-index      # 只 OCR，不建索引
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import pdfpipeline  # noqa: E402
from tools import index as index_mod  # noqa: E402


def parse_pages(spec: str):
    """'3-8' / '1,5,9' -> 页号集合或 None"""
    if not spec:
        return None
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return out


def main():
    ap = argparse.ArgumentParser(description="PDF OCR 文字层缓存构建器")
    ap.add_argument("--pdf", required=True, help="源 PDF 路径")
    ap.add_argument("--dpi", type=int, default=200, help="OCR 渲染 DPI（默认 200）")
    ap.add_argument("--workers", type=int, default=0, help="并行进程数（默认自动 ≤6）")
    ap.add_argument("--min-score", type=float, default=0.45, help="OCR 置信度下限")
    ap.add_argument("--force", action="store_true", help="强制重建")
    ap.add_argument("--backend", choices=["proc", "thread"], default="proc",
                    help="并行后端：proc=进程池(默认)；thread=线程池(受限环境用)")
    ap.add_argument("--gpu", action="store_true", help="使用 CUDA GPU 加速(需 onnxruntime-gpu)")
    ap.add_argument("--intra", type=int, default=0, help="每引擎 CPU 线程数(0=自动)")
    ap.add_argument("--no-index", action="store_true", help="跳过分词索引构建")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        sys.exit(f"文件不存在: {pdf}")

    cache = pdfpipeline.cache_dir_for(pdf)
    if cache.exists() and (cache / "meta.json").exists() and not args.force:
        print(f"缓存已存在: {cache}  （加 --force 重建）")
    else:
        t = time.time()
        cache = pdfpipeline.build_cache(pdf, dpi=args.dpi, workers=args.workers,
                                        min_score=args.min_score, backend=args.backend,
                                        intra_threads=args.intra, gpu=args.gpu)
        print(f"OCR 缓存完成: {cache}  用时 {time.time()-t:.1f}s")

    if not args.no_index:
        t = time.time()
        idx = index_mod.build_index(cache)
        meta = pdfpipeline.load_meta(cache)
        if meta:
            meta["indexed"] = True
            pdfpipeline._write_json(cache / "meta.json", meta)
        pdfpipeline.Progress(cache).set(state="done", phase="OCR 与索引完成",
                                        done=1, total=1, error=None)
        n_terms = len(idx.get("terms", {}))
        print(f"索引完成: {n_terms} 个词条  用时 {time.time()-t:.1f}s")
        print(f"缓存位置: {cache}")


if __name__ == "__main__":
    main()
