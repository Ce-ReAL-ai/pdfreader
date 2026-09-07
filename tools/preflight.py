# -*- coding: utf-8 -*-
"""preflight.py — 真实 PDF 预扫描（快速，只读元数据）

输出: 文件大小/页数/文字层情况/内嵌图像分辨率估计/CPU 核数
用法: python tools/preflight.py --pdf "path.pdf" [--sample 5]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--sample", type=int, default=5, help="抽查图像分辨率的页数")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    print(f"文件: {pdf}")
    print(f"大小: {pdf.stat().st_size/1e6:.1f} MB")
    print(f"CPU 核数: {os.cpu_count()}")

    doc = fitz.open(str(pdf))
    total = doc.page_count
    print(f"总页数: {total}")
    toc = doc.get_toc()
    print(f"书签层级条目数: {len(toc)}" + (f"  例: {toc[0][1] if toc else ''}" if toc else ""))

    # 文字层情况（每页抽查 words）
    text_pages, no_text_pages = [], []
    for i in range(total):
        n_words = len(doc[i].get_text("words"))
        (text_pages if n_words > 1 else no_text_pages).append(i + 1)
    print(f"自带文字层页: {len(text_pages)} 页；无文字层页: {len(no_text_pages)} 页")
    if text_pages:
        print(f"  文字层示例页: {text_pages[:5]}{'…' if len(text_pages) > 5 else ''}")
        probe = text_pages[len(text_pages) // 2]
        txt = doc[probe - 1].get_text()
        print(f"  中部文字页 p{probe} 文本前80字: {txt[:80]!r}")

    # 内嵌图像分辨率估计（抽查首/中/尾页，取每页最大图）
    pages_to_check = sorted(set(
        [1, max(2, total // 4), max(3, total // 2), max(4, total * 3 // 4), total]))
    if len(pages_to_check) > args.sample:
        pages_to_check = sorted(set([1, total // 2, total]))
    dpi_hits = []
    for pno in pages_to_check:
        imgs = doc[pno - 1].get_images(full=True)
        if not imgs:
            print(f"  p{pno}: 无内嵌图像")
            continue
        best = max(imgs, key=lambda im: (im[2] or 0) * (im[3] or 0))
        w, h = best[2], best[3]
        if w and h:
            pw, ph = doc[pno - 1].rect.width, doc[pno - 1].rect.height
            dx, dy = w / pw * 72, h / ph * 72
            dpi_hits.append((pno, w, h, dx, dy))
            print(f"  p{pno}: 图 {w}x{h}px  ->  等效约 {dx:.0f}x{dy:.0f} DPI")
    if dpi_hits:
        med = sorted(d for _, _, _, dx, dy in dpi_hits for d in (dx, dy))
        print(f"估计源图 DPI 中位 ≈ {med[len(med)//2]:.0f}")

    doc.close()
    print("预扫描完成。")


if __name__ == "__main__":
    main()
