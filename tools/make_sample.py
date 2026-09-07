# -*- coding: utf-8 -*-
"""make_sample.py — 生成“扫描型 PDF”合成样本（无文字层，供端到端测试）

做法：用内置中文字体排版文本页 -> 栅格化成灰度图 -> 把图嵌入新 PDF。
结果 PDF 每页只有一张位图，没有任何文字层，等价于扫描件。
注意：不要用外置 ttf/ttc 渲染中文，PyMuPDF 会回退成豆腐块（诊断过）。

用法: python tools/make_sample.py --pages 6 --out sample/sample_scanned.pdf
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import pymupdf as fitz  # PyMuPDF >=1.24 新命名
except ImportError:  # pragma: no cover
    import fitz

FONT = "china-s"  # PyMuPDF 内置简体中文字体（无需字体文件）

PARAS = [
    "海山重工集团始建于一九八七年，前身是海山机械修理厂。经过三十余年发展，集团已形成矿山机械、重型卡车与港口装备三大事业部，产品远销东南亚、非洲与南美市场。",
    "集团研发中心于二零一零年成立，现有工程技术人员四百六十余人。中心承担全系列产品的新结构设计与有限元强度校核工作，并与多家高校建立了产学研联合实验室。",
    "无人驾驶矿卡项目是集团近年的战略重点。项目团队已完成矿区高精地图构建、障碍物识别与编队调度三大模块的实车验证，累计测试里程超过两万公里。",
    "质量体系方面，海山重工先后通过 ISO9001 与 ISO14001 认证，核心结构件全部采用机器人焊接，焊缝一次合格率保持在百分之九十八以上，关键工序实现全程可追溯。",
    "集团近年营收稳步增长。二零二三年实现营业收入六十七亿元，其中海外收入占比首次突破百分之四十。公司计划在二零二五年之前建成两座数字化灯塔工厂。",
    "在售后服务网络方面，集团已在全球设立四十六个备件中心与三十一个服务站，承诺关键部件四十八小时到场。通过远程诊断平台，八成以上的常见故障可在线完成初步判定。",
    "The company's research center focuses on structural optimization. Field tests were carried out in the open-pit mine near the port of Dalian, covering more than 20,000 km in total. A digital twin platform synchronizes machine data every 200 milliseconds.",
    "董事会于上季度批准了新一轮扩产计划，预计投入人民币九点八亿元建设新能源装载机产线，设计年产能一千二百台，达产后预计新增就业岗位六百余个。",
    "安全培训采用情景化教学方式。每位新员工须完成不少于四十学时的入职培训，并通过安全考核后方可进入生产现场。集团还设有每月一次的全员应急演练日。",
    "本手册用于内部技术交流，内容如有与正式合同文件不一致之处，以合同文本为准。如需进一步了解产品细节，请与集团技术资料室联系，索取对应机型的技术说明书。",
]

KEYWORDS = ["海山重工集团", "无人驾驶矿卡", "ISO9001", "数字孪生", "新能源装载机",
            "远程诊断平台", "灯塔工厂", "技术说明书", "结构强度校核", "机器人焊接"]


def _put(page, pt, text, size, color):
    """统一插入文本（内置中文字体，含拉丁字符）"""
    page.insert_text(pt, text, fontsize=size, fontname=FONT, color=color)


def fill_page(page, rng):
    """在 A4 页上排版一页“像扫描文档”的内容"""
    rect = page.rect
    fs = 11.0
    line_h = fs * 1.55
    y = 56.0
    x_margin = 54.0
    x_right = rect.width - x_margin

    _put(page, (x_margin, y), "海山重工集团 · 技术资料汇编（试读卷）", 16, (0.05, 0.05, 0.05))
    y += 26
    _put(page, (x_margin, y), "保密等级：内部公开        版次：2024-A", 9, (0.25, 0.25, 0.25))
    y += line_h

    n_para = rng.randint(4, 6)
    pool = PARAS[:]
    rng.shuffle(pool)
    for pi, para in enumerate(pool[:n_para], 1):
        if y > rect.height - 90:
            break
        head = f"{pi}. " + KEYWORDS[(pi + rng.randint(0, 3)) % len(KEYWORDS)] + "相关说明"
        _put(page, (x_margin, y), head, 12, (0.05, 0.05, 0.05))
        y += line_h
        # 简单换行：按可用宽度近似切字（中文等宽近似）
        max_chars = int((x_right - x_margin) / (fs * 1.0))
        cur = ""
        for ch in para:
            cur += ch
            if len(cur) >= max_chars and ch in "，。；：":
                _put(page, (x_margin, y), cur, fs, (0.02, 0.02, 0.02))
                cur = ""
                y += line_h
                if y > rect.height - 70:
                    break
        if cur:
            _put(page, (x_margin, y), cur, fs, (0.02, 0.02, 0.02))
            y += line_h
        y += 4
    _put(page, ((rect.width - x_margin) / 2, rect.height - 34), "— 第 X 页 —", 9,
         (0.3, 0.3, 0.3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=6)
    ap.add_argument("--out", default="sample/sample_scanned.pdf")
    ap.add_argument("--dpi", type=int, default=150, help="栅格化 DPI，越大样本越重")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    tmp_doc = fitz.open()
    for _ in range(args.pages):
        page = tmp_doc.new_page(width=595, height=842)  # A4
        fill_page(page, rng)

    out_doc = fitz.open()
    zoom = args.dpi / 72.0
    for pno in range(args.pages):
        page = tmp_doc[pno]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                              colorspace=fitz.csGRAY, alpha=False)
        n_page = out_doc.new_page(width=595, height=842)
        n_page.insert_image(n_page.rect, stream=pix.tobytes("png"))
        print(f"page {pno+1}/{args.pages} rendered", flush=True)
    out_doc.save(str(out), deflate=True)
    print(f"样本已生成: {out}  ({out.stat().st_size/1024:.0f} KB, {args.pages} 页)")


if __name__ == "__main__":
    main()
