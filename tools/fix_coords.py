# -*- coding: utf-8 -*-
"""fix_coords.py — 把旧缓存(早期版本)的 region 坐标升级为 PDF 用户空间
（修复高亮与文字错位）。原地转换，无需重跑 OCR。

用法:
    python tools/fix_coords.py --pdf "path.pdf"      # 按内容哈希定位缓存
    python tools/fix_coords.py --cache cache/<docid> # 或直接给缓存目录
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import pdfpipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf")
    ap.add_argument("--cache")
    args = ap.parse_args()

    if args.cache:
        cache = Path(args.cache)
    elif args.pdf:
        pdf = Path(args.pdf)
        if not pdf.exists():
            sys.exit(f"文件不存在: {pdf}")
        cache = pdfpipeline.cache_dir_for(pdf)
    else:
        sys.exit("需要 --pdf 或 --cache 之一")

    if not cache.exists():
        sys.exit(f"缓存不存在: {cache}")
    n = pdfpipeline.convert_cache_to_pdf_space(cache)
    print(f"{cache}  已转换 {n} 页 → 坐标空间: pdf (coord_space=pdf)")
    if n:
        print("提示：前端无需改动，刷新页面后重新搜索即可看到正确高亮。")


if __name__ == "__main__":
    main()
