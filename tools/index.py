# -*- coding: utf-8 -*-
"""
index.py — jieba 中文分词检索：倒排索引构建 + 检索 + 命中定位

检索策略（v2，短语优先）：
  1) mode 允许短语时，优先对“去掉首尾空白的完整查询串”做整串子串匹配。
     命中后只高亮“完整短语”本身，避免把词切成单字造成全页误标。
  2) 整串无命中才退回分词：jieba 分词 AND，无交集转 OR；单字词默认不参与
     （除非 allow_single 或查询本身是单字）。
  3) 高亮框按“字符比例坐标”从 OCR 文本块中切出命中词的精确小框，
     不再整段高亮。

search(cache, query, limit=80, mode="auto", allow_single=False, ctx=42)
  mode: auto=短语优先否则分词; phrase=只短语; terms=只分词
返回：
  { query, took_ms, total, pages, terms, missing, match_type:"phrase"|"terms",
    hits:[ {page,n,terms,snippet,marks,regions:[{x0..y1}精确框,...]} ] }
"""
import bisect
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STOP_WORDS = frozenset(
    "的了吗呢吧啊呀哦嗯哈呗么嘛唉哟啦呵呀则与和或及并且但而如果虽然而虽则若既并且则不然要么或者因为所以因此因而从而于是以及无论不管只要除非一旦即使尽管虽然可是不过但是却然而并且还又再才就都也都只要只仅便更最很太极其非常十分相当比较较相对稍微略略微几乎似乎仿佛像如例如比如等等等之类等等是是否没有有在在于对对于向从被把将让使叫由据按照依依据根据按照就按照据如般而已罢了上下中内里外前后左右东西南北"
)

PUNCT = frozenset("，。！？；：、”“‘’（）《》〈〉【】〔〕…—·～％￥＃＠＆＊＋－＝／｜＼　,.;:!?()[]{}\"'<>/|\\_^~`*+-=@#$%&·")


def _cjk(c: str) -> bool:
    o = ord(c)
    return (0x3400 <= o <= 0x4DBF) or (0x4E00 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) \
        or (0x3000 <= o <= 0x303F)


def _drop_token(t: str) -> bool:
    if not t:
        return True
    if all(c in PUNCT or c.isspace() for c in t):
        return True
    if t in STOP_WORDS:
        return True
    if len(t) == 1:
        c = t[0]
        return not (c.isalnum() and c.isascii()) and not _cjk(c)
    return False


def tokenize(text: str):
    import jieba
    return [t for t in jieba.lcut(text) if not _drop_token(t)]


# ---------------------------------------------------------------- 索引
def build_index(cache: Path, progress_cb=None) -> dict:
    """构建/重建倒排索引。progress_cb(done, total) 可选。"""
    import jieba
    pages_dir = cache / "pages"
    files = sorted(pages_dir.glob("p*.json"))
    n_files = len(files)
    terms: dict = {}
    pages_len: list = []
    n_pages = 0
    for fi, p in enumerate(files, 1):
        data = json.loads(p.read_text(encoding="utf-8"))
        n_pages += 1
        text = data.get("text", "")
        pages_len.append(len(text))
        seen: dict = {}
        for t in jieba.lcut(text):
            if _drop_token(t):
                continue
            seen[t] = seen.get(t, 0) + 1
        for t, c in seen.items():
            terms.setdefault(t, {})[str(n_pages)] = c
        if progress_cb and (fi % 5 == 0 or fi == n_files):
            try:
                progress_cb(fi, n_files)
            except Exception:
                pass
    idx = {"version": 2, "pages_len": pages_len, "terms": terms}
    tmp = cache / "index.json.tmp"
    tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache / "index.json")
    return idx


# 页文本模块级缓存：多次检索间复用（单文档内存量很小）
_TEXT_CACHE: dict = {}


class DocCache:
    """一次检索会话内的页文本缓存"""

    def __init__(self, cache: Path):
        self.cache = Path(cache)
        self._texts = {}
        self._regions = {}
        self._meta = None

    def meta(self):
        if self._meta is None:
            m = self.cache / "meta.json"
            self._meta = json.loads(m.read_text(encoding="utf-8")) if m.exists() else {}
        return self._meta

    def _load(self, n: int) -> None:
        p = self.cache / "pages" / f"p{n:05d}.json"
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            self._texts[n] = d.get("text", "")
            self._regions[n] = d.get("regions", [])
        else:
            self._texts[n] = ""
            self._regions[n] = []

    def page(self, n: int) -> dict:
        if n not in self._texts:
            key = str(self.cache)
            if key in _TEXT_CACHE and n in _TEXT_CACHE[key]:
                t, r = _TEXT_CACHE[key][n]
                self._texts[n], self._regions[n] = t, r
            else:
                self._load(n)
        return {"text": self._texts[n], "regions": self._regions[n]}

    def text(self, n: int) -> str:
        return self.page(n)["text"]

    def regions(self, n: int) -> list:
        return self.page(n)["regions"]

    def texts_all(self) -> dict:
        """返回 {页码: text}（首次调用会全量读盘并缓存）"""
        key = str(self.cache)
        if key not in _TEXT_CACHE:
            total = int(self.meta().get("pages", 0))
            mem = {}
            for n in range(1, total + 1):
                d = self.page(n)
                mem[n] = d["text"]
            _TEXT_CACHE[key] = {n: (mem[n], self._regions[n]) for n in mem}
        return {n: t for n, (t, _) in _TEXT_CACHE[key].items()}


def _region_for_offset(regions: list, off: int):
    starts = [r["o"] for r in regions]
    i = bisect.bisect_right(starts, off) - 1
    if i < 0:
        return None
    r = regions[i]
    if off < r["o"] + len(r["t"]):
        return i
    return None


# ---------------------------------------------------------------- 精确框
def _char_w(ch: str) -> float:
    """粗略字符宽（全角≈1.0，半角≈0.5），用于按比例切高亮框"""
    if ch.isspace():
        return 0.35
    o = ord(ch)
    if o < 128:
        return 0.55
    return 1.0


def _sub_rect(region: dict, lo: int, hi: int) -> dict | None:
    """region 文本内字符区间 [lo,hi) -> 精确子矩形（PDF 坐标）"""
    t = region.get("t", "")
    x0, x1 = region.get("x0", 0.0), region.get("x1", 0.0)
    y0, y1 = region.get("y0", 0.0), region.get("y1", 0.0)
    w = x1 - x0
    if w <= 0.1 or not t:
        return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    lo = max(0, min(lo, len(t)))
    hi = max(lo, min(hi, len(t)))
    if lo >= hi:
        return None
    cum = []
    acc = 0.0
    for ch in t:
        cum.append(acc)
        acc += _char_w(ch)
    total = acc or 1.0
    ax = x0 + (cum[lo] / total) * w
    bx = x0 + ((cum[hi - 1] + _char_w(t[hi - 1])) / total) * w
    return {"x0": ax, "y0": y0, "x1": bx, "y1": y1}


def _precise_rects(text: str, regions: list, spans, max_rects: int = 400) -> list:
    """把 [s,e) 命中区间切成字符级精确框（跨 region 时每段一个框）"""
    out = []
    seen = set()
    for s, e in spans:
        for off in (s, e - 1):
            ri = _region_for_offset(regions, off)
            if ri is not None:
                r = regions[ri]
                ro = r.get("o", 0)
                lo = max(ro, s) - ro
                hi = min(ro + len(r.get("t", "")), e) - ro
                rect = _sub_rect(r, lo, hi)
                if rect is None:
                    continue
                key = (round(rect["x0"], 2), round(rect["y0"], 2),
                       round(rect["x1"], 2), round(rect["y1"], 2))
                if key not in seen:
                    seen.add(key)
                    out.append(rect)
                    if len(out) >= max_rects:
                        return out
    return out


def _spans_of(text: str, sub: str):
    out = []
    start = 0
    low = sub.lower()
    low_text = text.lower()
    while True:
        i = low_text.find(low, start)
        if i < 0:
            break
        out.append((i, i + len(sub)))
        start = i + max(1, len(sub))
    return out


# ---------------------------------------------------------------- 检索
def search(cache, query: str, limit: int = 80, mode: str = "auto",
           allow_single: bool = False, ctx: int = 42) -> dict:
    t0 = time.time()
    cache = Path(cache)
    if not (cache / "index.json").exists():
        build_index(cache)
    idx = json.loads((cache / "index.json").read_text(encoding="utf-8"))
    terms = idx.get("terms", {})
    dc = DocCache(cache)
    n_pages = int(dc.meta().get("pages", 0))

    clean = query.strip()
    toks = tokenize(clean) if clean else []
    if not clean or not toks:
        return {"query": clean, "took_ms": round((time.time() - t0) * 1000, 1),
                "total": 0, "pages": n_pages, "terms": [], "missing": [],
                "match_type": "terms", "hits": []}

    hit_buckets = []  # (page, term_or_phrase, spans)
    match_type = "terms"

    # ---- 策略1：完整短语优先（含空格折叠副本）
    phrase_cands = []
    if mode in ("auto", "phrase") and len(clean) >= 2:
        phrase_cands.append(clean)
    # ---- 策略2：分词
    tok_list = toks

    if phrase_cands:
        all_texts = dc.texts_all()
        for ph in phrase_cands:
            pages = [n for n in range(1, n_pages + 1) if ph.lower() in all_texts[n].lower()]
            if pages:
                hit_buckets = [(n, ph, _spans_of(all_texts[n], ph)) for n in pages]
                match_type = "phrase"
                break
    if not hit_buckets:
        # 分词路径
        eligible = []
        for t in tok_list:
            if len(t) == 1 and not allow_single and len(clean) >= 2 and _cjk(t[0]):
                continue  # 多字查询时单字词不参与，避免“所有 X”噪声
            eligible.append(t)
        if not eligible:
            eligible = tok_list[:1] if tok_list else []
        term_pages: dict = {}
        missing = []
        all_texts = dc.texts_all()
        for t in eligible:
            pg = terms.get(t)
            if pg:
                term_pages[t] = {int(k) for k in pg}
            else:
                sub = {n for n in range(1, n_pages + 1) if t in all_texts[n]}
                term_pages[t] = sub
                if not sub:
                    missing.append(t)
        if term_pages:
            sets = list(term_pages.values())
            cand = set.intersection(*sets) if sets else set()
            if not cand:
                cand = set.union(*sets)
            for n in sorted(cand):
                text = all_texts[n]
                spans = []
                mt = []
                for t in eligible:
                    sp = _spans_of(text, t)
                    if sp:
                        mt.append(t)
                        spans.extend(sp)
                if spans:
                    hit_buckets.append((n, mt, spans))
            match_type = "terms"

    # 合并重叠区间 + 组装
    hits = []
    for pno, tag, spans in hit_buckets:
        page = dc.page(pno)
        text = page["text"]
        if not spans:
            continue
        spans.sort()
        merged = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        span_s = min(s for s, _ in merged)
        span_e = max(e for _, e in merged)
        s_start = max(0, span_s - ctx)
        s_end = min(len(text), span_e + ctx)
        snippet = text[s_start:s_end]
        base = s_start
        marks = [[s - base, e - base] for s, e in merged if e > s_start and s < s_end]
        rects = _precise_rects(text, page["regions"], merged)
        term_lbl = tag if isinstance(tag, str) else " ".join(tag)
        hits.append({
            "page": pno, "n": len(merged),
            "terms": [term_lbl] if isinstance(tag, str) else tag,
            "snippet": snippet, "marks": marks, "regions": rects,
        })

    hits.sort(key=lambda h: (-h["n"], h["page"]))
    total = len(hits)
    hits = hits[:limit]

    missing = [t for t in tok_list
               if (t not in terms and not any(
                   t in dc.texts_all()[n] for n in range(1, n_pages + 1)))]
    return {"query": clean, "took_ms": round((time.time() - t0) * 1000, 1),
            "total": total, "pages": n_pages,
            "terms": [ph] if match_type == "phrase" and phrase_cands else toks,
            "missing": missing, "match_type": match_type, "hits": hits}
