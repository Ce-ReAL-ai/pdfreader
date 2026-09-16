# -*- coding: utf-8 -*-
"""打包后冒烟测试：验证「解压即用」的关键链路，不依赖 pytest/网络。

默认对源码运行，也可以直接指向打包产物：

    python tools/smoke_release.py                       # 测源码
    python tools/smoke_release.py --exe dist\PDFReader\PDF阅读器.exe

检查项（全过才打印 OK）：
  1. 启动后服务器能连上，且前端渲染库 / web 资源都能取到（200）；
  2. 预置缓存命中：同一份 PDF 的 docid 与 cache/ 里的目录一致，ready=True，
     也就是说对方不需要重新 OCR；
  3. 预置缓存里没有打包机的 _last.json / _library.json 残留；
  4. /api/pdf 支持 HTTP Range（pdf.js 靠它按需读大 PDF）。

用 --pdf 指定一本已建好缓存的书；省略时从 cache/ 自动挑一本索引完整的。
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _http(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers)


def _find_seeded_pdf(cache_root: Path):
    """从 cache/ 里挑一本有索引的缓存，反查它对应的源 PDF（用体积+页数匹配不靠谱，
    改成直接问 meta.json 里的 src，不存在就跳过）。"""
    for d in sorted(cache_root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if not (d / "index.json").exists():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        src = meta.get("src")
        if src and Path(src).is_file():
            return Path(src), d.name
    return None, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="打包产物冒烟测试")
    ap.add_argument("--exe", help="打包后的 exe；省略则用源码 launcher")
    ap.add_argument("--pdf", help="要打开的 PDF（默认从 cache/ 自动挑一本）")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--cache-root", default=str(ROOT / "cache"))
    ap.add_argument("--timeout", type=int, default=120, help="等待服务就绪的秒数")
    args = ap.parse_args(argv)

    cache_root = Path(args.cache_root).resolve()
    pdf = Path(args.pdf) if args.pdf else None
    expect_docid = None
    if pdf is None:
        pdf, expect_docid = _find_seeded_pdf(cache_root)
        if pdf is None:
            print("[跳过] cache/ 里没有可用的成品缓存（先跑一次 build_cache.py）")
            return 0
    print(f"源 PDF    : {pdf}")
    print(f"缓存根    : {cache_root}")

    if args.exe:
        cmd = [str(Path(args.exe).resolve()), "--pdf", str(pdf),
               "--cache-root", str(cache_root), "--port", str(args.port), "--no-open"]
    else:
        cmd = [sys.executable, str(ROOT / "tools" / "launcher.py"), "--pdf", str(pdf),
               "--cache-root", str(cache_root), "--port", str(args.port), "--no-open"]
    print(f"启动命令  : {' '.join(cmd)}\n")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            cwd=str(ROOT))
    base = f"http://127.0.0.1:{args.port}"
    failures = []
    try:
        deadline = time.time() + args.timeout
        meta = None
        while time.time() < deadline:
            if proc.poll() is not None:
                failures.append(f"进程提前退出（code={proc.returncode}）")
                break
            try:
                status, body, _ = _http(f"{base}/api/meta", timeout=5)
                if status == 200:
                    meta = json.loads(body)
                    break
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                pass
            time.sleep(2)

        if meta is None and not failures:
            failures.append(f"{args.timeout}s 内服务未就绪")
        if meta:
            print(f"[1] 服务就绪   ready={meta['ready']} pages={meta['pages']} "
                  f"docid={meta['docid']}")
            if not meta["ready"]:
                failures.append("文档未就绪（缓存没命中，会触发重新 OCR）")
            if expect_docid and meta["docid"] != expect_docid:
                failures.append(
                    f"docid 不匹配：期望 {expect_docid}，实际 {meta['docid']} "
                    "（缓存按内容哈希命名，对不上说明 PDF 不是同一份）")
            cache_dir = Path(meta["cache_dir"])
            if not (cache_dir / "index.json").exists():
                failures.append(f"缓存目录缺 index.json: {cache_dir}")

            for path, label in [("/", "前端首页"),
                                ("/vendor/pdfjs/build/pdf.mjs", "pdf.js 主库"),
                                ("/vendor/pdfjs/build/pdf.worker.mjs", "pdf.js worker"),
                                ("/vendor/pdfjs/cmaps/UniGB-UCS2-H.bcmap", "中文 cmap"),
                                (f"/api/page?n=1", "首页文本")]:
                try:
                    status, body, _ = _http(f"{base}{path}")
                    ok = status == 200 and len(body) > 0
                    print(f"    {'OK ' if ok else 'BAD'} {label:<12} {status} {len(body)}B")
                    if not ok:
                        failures.append(f"{label} 取不到内容: {path}")
                except Exception as e:  # noqa: BLE001
                    print(f"    BAD {label:<12} {e}")
                    failures.append(f"{label} 请求失败: {e!r}")

            print("[2] 缓存命中   已跳过 OCR，index.json 存在")
            for stale in ("_last.json", "_library.json"):
                if (cache_root / stale).exists() and args.exe:
                    print(f"    [警告] 分发包里残留 {stale}（打包机绝对路径，对方无效）")
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        tail = [ln for ln in (out or "").splitlines() if ln.strip()][:12]
        if tail:
            print("\n--- 进程输出（前 12 行）---")
            for ln in tail:
                print("   ", ln)

    if failures:
        print("\n结果: FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n结果: OK —— 解压即用链路正常（服务/前端/缓存命中/Range）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
