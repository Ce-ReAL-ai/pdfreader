# -*- coding: utf-8 -*-
"""make_release.py — 组装可分发的绿色包（不含 PDF，含预置 OCR 缓存）。

做四件事：
  1. 从缓存来源目录（默认项目 cache/，可用 --cache-src 指向别处，例如从 release
     资产解出来的 staging 目录）挑出「已 OCR 好的成品缓存」，拷进 dist/PDFReader/cache/
     —— 缓存按文件前 4MB 的 sha256 命名，与路径无关，所以对方拿到后只要书是同
        一份文件，打开就是毫秒级检索，完全跳过 OCR；
  2. 建空的 books/ 目录（让对方有地方放书）；
  3. 生成「使用说明.txt」；
  4. 可选产出归档：7z（LZMA2，比 zip 小 ~28%，推荐传 GitHub Release）或 zip。

用法：
    python tools/make_release.py                    # 自动挑选体积最大的 4 份缓存
    python tools/make_release.py --archive both     # 同时产出 7z 与 zip
    python tools/make_release.py --docid a,b,c,d    # 指定要预置的缓存
    python tools/make_release.py --all              # 预置全部缓存
    python tools/make_release.py --cache-src staging/cache   # 从别处取缓存（CI 用）
    python tools/make_release.py --keep-cache       # 保留 dist 里已有 cache
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "PDFReader"
CACHE_SRC = ROOT / "cache"

# 7z/LZMA2 在 DLL 上比 deflate 好得多：实测 143 MB → 103 MB。
# 有 7-Zip 就用它，没有就退回 zip（Windows 资源管理器能直接右键解压）。
SEVEN_ZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "7z",
]

# 按文件大小给缓存起个人看得懂的名字，仅供日志显示
SIZE_HINT = {
    234834921: "王道2027操作系统",
    208829946: "王道2027计算机组成原理",
    211679786: "2027数据结构",
    197045739: "2027计算机网络",
    129421776: "26组成原理",
    130556098: "26数据结构",
    122619475: "26计算机网络",
    1712501: "文献样本",
}


def _fmt_mb(num: int) -> str:
    return f"{num / 1048576:.1f} MB"


# 记录「这个包预置了哪几份缓存」，供同名仓库里复现发布、以及 CI 参考
SHIPPED_BOOKS = "SHIPPED_BOOKS.json"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def scan_cache(root: Path):
    """列出 root 下所有可用缓存：docid -> (页数, 源文件大小, 目录体积)。"""
    found = {}
    if not root.is_dir():
        return found
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        meta = d / "meta.json"
        if not meta.is_file():
            continue
        try:
            j = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:
            continue
        found[d.name] = {
            "dir": d,
            "pages": int(j.get("pages") or 0),
            "src_size": int(j.get("src_size") or 0),
            "ocr_pages": int(j.get("ocr_pages") or 0),
            "indexed": bool(j.get("indexed")),
            "ready": bool((d / "index.json").is_file()),
            "size": _dir_size(d),
        }
    return found


def pick_default(found, count: int):
    """默认挑选：已建索引、且源 PDF 体积最大的几本。

    按体积而不是页数排序：要分享的就是那几本几百 MB 的扫描教材，
    页数排序会把 400 页的小书排到 300 多页的大书前面（体积差 1.7 倍）。
    想精确控制就用 --docid 指定。
    """
    usable = [k for k, v in found.items() if v["ready"] and v["src_size"] >= 5 << 20]
    usable.sort(key=lambda k: (-found[k]["src_size"], k))
    return usable[:count]


def clean_state_files(dst_root: Path):
    """删掉打包机上的会话状态：_last.json / _library.json 存的是绝对路径。

    对接收方全是无效信息（路径不存在，文档库列表也指不到东西），带上只会让
    程序一启动就提示「继续上次的文档」再落空。缓存本体（<docid>/）与路径无关，不动。
    """
    removed = []
    for name in ("_last.json", "_library.json"):
        f = dst_root / name
        if f.exists():
            f.unlink()
            removed.append(name)
    # progress.json 是「构建到哪了」的中间态：打包机上是 done，但对方没缓存进度概念，
    # 留着容易让网页进度条显示上一轮的残留状态
    for f in dst_root.glob("*/progress.json"):
        f.unlink()
        removed.append(f"{f.parent.name}/progress.json")
    if removed:
        print(f"  已清理打包机残留状态: {', '.join(removed)}")


def copy_caches(docids, found, dst_root: Path):
    dst_root.mkdir(parents=True, exist_ok=True)
    total = 0
    for docid in docids:
        info = found.get(docid)
        if info is None:
            print(f"  [跳过] cache/{docid} 不存在或没有 meta.json")
            continue
        dst = dst_root / docid
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(info["dir"], dst)
        total += info["size"]
        hint = SIZE_HINT.get(info["src_size"], "?")
        flag = "含OCR" if info["ocr_pages"] else "仅文字层"
        print(f"  [预置] {docid}  {info['pages']:>4} 页  {flag}  "
              f"{_fmt_mb(info['size']):>9}  {hint}")
    return total


README = """\
PDF 快速检索阅读器 —— 解压即用版
================================================================
【怎么用】

  1. 把要看的 PDF 放进 books\\ 文件夹（放几本都行）；
  2. 双击 PDF阅读器.exe；
  3. 程序会列出 books\\ 里的书让你选，然后自动打开浏览器；
     第一次打开某本书会在后台自动 OCR 建缓存（网页上有进度条），
     建完之后再搜就是毫秒级出结果。

  关掉那个黑色命令行窗口 = 退出程序。浏览器窗口可以直接关，不影响程序。


【已经有缓存的书，秒开】

  本包预置了下面这几本书的 OCR 缓存（缓存按文件内容识别，与路径无关）：

{books}

  只要你手上是同一份 PDF 文件（从同一个来源下载、大小一致），
  放进 books\\ 后双击打开就能直接检索，不用再等 OCR。
  如果 PDF 内容不一样（不同版本、重新扫描过），程序会自动重新建缓存，
  不会拿旧缓存出错结果 —— 缓存目录是按文件内容哈希区分的。


【常见问题】

  · 双击没反应 / 一闪而过
      多半是杀毒软件或 SmartScreen 拦了。右键 exe → 属性 → 若有「解除锁定」就勾上；
      或把整个文件夹加进杀软白名单。注意：**不能只拷 exe**，_internal 文件夹必须在一起。

  · 浏览器显示「加载文档…」卡住
      按 Ctrl+Shift+R 强制刷新一次。仍不行就重启程序（换端口会自动处理）。

  · 提示端口被占用
      程序会自动往后找端口（8765 → 8766 …），一般无需理会。

  · 搜索没有结果 / 高亮对不上
      第一次搜索要把整本书的全文载入内存，几百毫秒，属正常。
      OCR 难免有错字，遇到搜不到的词可在「检索设置」里切换匹配模式，
      或勾选单字匹配辅助排查。

  · 想换一本书 / 打开 books\\ 以外的 PDF
      网页右上角「切换文档」→ 输入路径，或扫描某个文件夹。

  · CPU 占用高、风扇狂转
      建缓存时是正常的（多核并行 OCR）。建完之后检索几乎不占 CPU。


【可选：命令行参数】

  在文件夹地址栏输入 cmd 回车，然后：

      PDF阅读器.exe --pdf "D:\\某本书.pdf"     直接打开指定 PDF
      PDF阅读器.exe --books D:\\我的书         指定放书的目录
      PDF阅读器.exe --port 8790                换个端口
      PDF阅读器.exe --dpi 300                  提高 OCR 清晰度（更慢）
      PDF阅读器.exe --gpu                      有 NVIDIA 显卡时用 GPU 加速
                                               （需要显卡版程序包，普通版无此功能）

  本包是 CPU 版，任何 Windows 10/11 64 位电脑都能跑，不需要装 Python。
"""


def write_readme(books_lines):
    text = README.format(books=books_lines or "  （本包未预置缓存，首次打开会自动建立）")
    (DIST / "使用说明.txt").write_text(text, encoding="utf-8")


def write_shipped_books(found, docids, cache_src: Path):
    """在缓存来源目录里记一笔：这次预置了哪些 docid。

    不含绝对路径（不含 src），可以安全提交/对比；CI 与以后复现发布时据此取缓存。
    """
    data = {
        "note": "预置到发布包里的 OCR 缓存；docid = 源 PDF 前 4MB 的 sha256 前 16 位",
        "source_cache_dir": cache_src.name,
        "docids": [            {
                "docid": d,
                "pages": found[d]["pages"],
                "src_size": found[d]["src_size"],
                "ocr_pages": found[d]["ocr_pages"],
                "size_bytes": found[d]["size"],
                "guess": SIZE_HINT.get(found[d]["src_size"], ""),
            }
            for d in docids if d in found
        ],
    }
    out = cache_src / SHIPPED_BOOKS
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  已记录预置清单: {out}")


def find_7z():
    for cand in SEVEN_ZIP_CANDIDATES:
        p = Path(cand)
        if p.is_file():
            return str(p)
        found = shutil.which(cand)
        if found:
            return found
    return None


def make_zip(dist: Path) -> Path:
    # 资产名必须是 ASCII：GitHub 上传接口的 ?name= 会把非 ASCII 字符吞掉
    # （实测 "PDFReader-分享包.7z" 上去后变成 "PDFReader-.7z"）
    out = dist.parent / "PDFReader-win64.zip"
    print(f"正在打包 zip: {out}")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(dist.rglob("*")):
            if f.is_file():
                z.write(f, Path(dist.name) / f.relative_to(dist))
    return out


def make_7z(dist: Path):
    """用 7-Zip 的 LZMA2 打包（比 zip 小 ~28%，适合当 GitHub Release 资产）。"""
    exe = find_7z()
    if not exe:
        print("[跳过] 没找到 7-Zip（7z.exe），无法产出 7z。")
        print("       安装 7-Zip 后重试，或直接传 zip（体积大 ~28%）。")
        return None
    out = dist.parent / "PDFReader-win64.7z"
    if out.exists():
        out.unlink()
    print(f"正在打包 7z (LZMA2 -mx=9): {out}")
    subprocess.run(
        [exe, "a", "-t7z", "-m0=lzma2", "-mx=9", "-mmt=on", str(out), str(dist)],
        check=True, stdout=subprocess.DEVNULL)
    return out


def make_cache_archive(found, docids, cache_src: Path) -> Path:
    """单独产出「只含预置缓存」的 zip —— 作为 Release 的独立资产。

    用途：别人只想要缓存（自己已经有程序、或想自己 from source 构建），
    下这一个几十 MB 的包就行，不用拖 100 MB 的整包。
    """
    out = DIST.parent / "pdfreader-cache.zip"
    if out.exists():
        out.unlink()
    print(f"正在打包缓存资产: {out}")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for d in docids:
            info = found.get(d)
            if not info:
                continue
            for f in sorted(info["dir"].rglob("*")):
                if not f.is_file():
                    continue
                # 打包机的会话状态一律不带（绝对路径、中间进度）
                if f.name in ("_last.json", "_library.json", "progress.json"):
                    continue
                z.write(f, Path("ocr-cache") / d / f.relative_to(info["dir"]))
        manifest = cache_src / SHIPPED_BOOKS
        if manifest.exists():
            z.write(manifest, Path("ocr-cache") / SHIPPED_BOOKS)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="组装 PDF 阅读器绿色包")
    ap.add_argument("--dist", default=str(DIST), help="打包输出目录")
    ap.add_argument("--cache-src", default=str(CACHE_SRC),
                    help="缓存来源目录（默认项目 cache/；CI 里可指向解压出来的 staging）")
    ap.add_argument("--docid", help="逗号分隔的 docid 白名单（默认自动挑选）")
    ap.add_argument("--count", type=int, default=4, help="自动挑选时预置几本（默认 4）")
    ap.add_argument("--all", action="store_true", help="预置全部缓存")
    ap.add_argument("--archive", choices=["none", "zip", "7z", "both"], default="none",
                    help="产出整包归档（默认不产出；7z 最小，推荐传 Release）")
    ap.add_argument("--cache-archive", action="store_true",
                    help="额外产出只含预置缓存的 pdfreader-cache.zip（Release 独立资产）")
    ap.add_argument("--zip", action="store_true", help="等价于 --archive zip（兼容旧用法）")
    ap.add_argument("--keep-cache", action="store_true",
                    help="保留输出目录里已有的 cache/（增量重打包时用）")
    args = ap.parse_args(argv)

    archive = "zip" if args.zip else args.archive
    cache_src = Path(args.cache_src).resolve()
    dist = Path(args.dist).resolve()
    if not (dist / "PDF阅读器.exe").exists():
        print(f"[错误] 没找到 {dist / 'PDF阅读器.exe'}，请先执行:")
        print("       pyinstaller pdfreader.spec --noconfirm")
        return 2

    print(f"发布目录: {dist}")
    print(f"缓存来源: {cache_src}")
    found = scan_cache(cache_src)
    if not found:
        print("[警告] 缓存来源目录里没有成品缓存，包里将不含预置缓存。")

    if args.all:
        docids = sorted(found, key=lambda k: (-found[k]["pages"], k))
    elif args.docid:
        docids = [d.strip() for d in args.docid.split(",") if d.strip()]
    else:
        docids = pick_default(found, args.count)

    cache_dst = dist / "cache"
    if args.keep_cache and cache_dst.exists():
        print("保留已有 cache/（--keep-cache）")
    else:
        if cache_dst.exists():
            shutil.rmtree(cache_dst)
        print(f"预置 OCR 缓存（{len(docids)} 本）：")
        copied = copy_caches(docids, found, cache_dst)
        print(f"  缓存合计: {_fmt_mb(copied)}")
    clean_state_files(cache_dst)

    books_dir = dist / "books"
    books_dir.mkdir(exist_ok=True)
    (books_dir / "把PDF放这里.txt").write_text(
        "把你的 PDF 放进这个文件夹，然后双击上级目录的「PDF阅读器.exe」。\n",
        encoding="utf-8")

    lines = []
    for docid in docids:
        info = found.get(docid)
        if not info:
            continue
        hint = SIZE_HINT.get(info["src_size"], f"{_fmt_mb(info['src_size'])} 的 PDF")
        lines.append(f"  · {hint}（{info['pages']} 页）")
    write_readme("\n".join(lines))
    if found:
        write_shipped_books(found, docids, cache_src)

    total = _dir_size(dist)
    cache_size = _dir_size(cache_dst) if cache_dst.exists() else 0
    print(f"\n分发包大小: {_fmt_mb(total)}  ({dist})")
    print(f"  其中 _internal: {_fmt_mb(_dir_size(dist / '_internal'))}")
    print(f"       cache:     {_fmt_mb(cache_size)}")

    outputs = []
    if archive in ("zip", "both"):
        outputs.append(make_zip(dist))
    if archive in ("7z", "both"):
        p = make_7z(dist)
        if p:
            outputs.append(p)
    if args.cache_archive and docids:
        outputs.append(make_cache_archive(found, docids, cache_src))
    for p in outputs:
        print(f"完成: {p}  {_fmt_mb(p.stat().st_size)}")

    if outputs:
        print("\n上传到 GitHub Release（推荐，仓库本身保持只有源码）:")
        print("  仓库 → Releases → Draft a new release → 选/建 tag（如 v1.0.0）"
              " → 把上面的文件拖进 assets → Publish")
        print("  或者有 gh CLI:")
        for p in outputs:
            print(f'    gh release create v1.0.0 "{p}" --title "v1.0.0" --notes "说明"')
    else:
        print("\n下一步：把整个 PDFReader 文件夹发给对方，解压后双击 PDF阅读器.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
