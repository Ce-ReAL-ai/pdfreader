# -*- coding: utf-8 -*-
"""publish_release.py — 用 GitHub API 创建/更新 Release 并上传资产。

为什么不用 git / gh：
  * gh CLI 本机没装；
  * Release 资产上传本来就不是 git 的事，只有 REST API 一条路；
  * 本机 git 走 schannel，历史上在受限沙箱里会 `SEC_E_NO_CREDENTIALS`，
    而 Python 的 TLS 一直是好的 —— 所以这里全部用 urllib 直连 api.github.com，
    也就顺带绕开了 git 的证书问题。

token 来源：--token > $GITHUB_TOKEN > .secrets/github-token > ~/.secrets/github-token

用法：
    python tools/publish_release.py --tag v1.0.0 --asset dist/PDFReader-分享包.7z
    python tools/publish_release.py --tag v1.0.0 --asset a.zip --asset b.zip --title "标题" --notes-file notes.md
    python tools/publish_release.py --list
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
DEFAULT_REPO = "Ce-ReAL-ai/pdfreader"
TOKEN_FILES = [ROOT / ".secrets" / "github-token",
               Path.home() / ".secrets" / "github-token"]
TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN", "GH_PAT", "PDFREADER_GIT_TOKEN")


def get_token(override=None) -> str:
    if override:
        return override.strip()
    for var in TOKEN_ENVS:
        v = os.environ.get(var)
        if v and v.strip():
            return v.strip()
    for f in TOKEN_FILES:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    return line.strip()
        except OSError:
            continue
    return ""


class GitHub:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo

    def _headers(self, extra=None):
        h = {
            "User-Agent": "pdfreader-publish",
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if extra:
            h.update(extra)
        return h

    def request(self, path, method="GET", data=None, raw=False, timeout=300):
        url = path if path.startswith("http") else API + path
        body = None
        headers = self._headers()
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                return r.status, (payload if raw else json.loads(payload.decode() or "{}"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(text).get("message", text)
            except Exception:  # noqa: BLE001
                msg = text
            raise SystemExit(f"[错误] {method} {url} -> {e.code} {msg}") from None

    # ---------------------------------------------------------------- release
    def get_release_by_tag(self, tag):
        try:
            return self.request(f"/repos/{self.repo}/releases/tags/{tag}")[1]
        except SystemExit:
            return None

    def create_release(self, tag, name=None, body="", draft=False, prerelease=False):
        return self.request(f"/repos/{self.repo}/releases", "POST", {
            "tag_name": tag, "name": name or tag, "body": body,
            "draft": draft, "prerelease": prerelease,
        })[1]

    def update_release(self, rid, **kw):
        return self.request(f"/repos/{self.repo}/releases/{rid}", "PATCH", kw)[1]

    def delete_asset(self, aid):
        self.request(f"/repos/{self.repo}/releases/assets/{aid}", "DELETE")

    def upload_asset(self, upload_url, path: Path, name: str = None):
        """流式上传（用 http.client 手工发，便于看到 GitHub 的真实报错）。

        两个坑：
          * urllib 在 4xx 时抛 HTTPError，信息不好读 → 手工读 response body；
          * `?name=` 里的非 ASCII 字符会被 GitHub 丢掉（"分享包.7z" 变成 ".7z"），
            所以资产名要用 ASCII：只对 ASCII 名字做 URL 编码，中文名字另行传 name。
        """
        import http.client
        asset_name = (name or path.name).strip()
        if not asset_name.isascii():
            raise SystemExit(
                f"[错误] 资产名含非 ASCII 字符会显示异常: {asset_name!r}\n"
                f"        请用 --asset-name 指定一个 ASCII 名字，或改名后再传。")
        base = upload_url.split("{")[0]          # 去掉 {...} 模板部分
        parsed = urllib.parse.urlsplit(base)
        target = f"{parsed.path}?name={urllib.parse.quote(asset_name, safe='')}"
        size = path.stat().st_size

        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=1800)
        try:
            conn.putrequest("POST", target)
            for k, v in self._headers({
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            }).items():
                conn.putheader(k, v)
            conn.endheaders()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", "replace")
            if resp.status not in (200, 201):
                try:
                    msg = json.loads(body).get("message", body)
                except Exception:  # noqa: BLE001
                    msg = body
                raise SystemExit(
                    f"[错误] 上传 {asset_name} 失败 -> HTTP {resp.status}: {msg}\n"
                    f"        上传地址: {base}")
            return resp.status, json.loads(body or "{}")
        finally:
            conn.close()

    def list_releases(self):
        return self.request(f"/repos/{self.repo}/releases?per_page=50")[1]


def sha256_of(path: Path, chunk=1 << 20) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest().upper()


def human(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发布 GitHub Release 并上传资产")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--tag")
    ap.add_argument("--title")
    ap.add_argument("--notes", default="")
    ap.add_argument("--notes-file")
    ap.add_argument("--asset", action="append", default=[], help="可多次指定")
    ap.add_argument("--asset-name", action="append", default=[],
                    help="与 --asset 一一对应的 ASCII 资产名（中文名会被 GitHub 吞掉）")
    ap.add_argument("--token")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--prerelease", action="store_true")
    ap.add_argument("--replace", action="store_true",
                    help="已存在同名资产时先删除再上传")
    ap.add_argument("--list", action="store_true", help="只列出已有 Release")
    args = ap.parse_args(argv)

    token = get_token(args.token)
    if not token:
        print("[错误] 没有 token。用 --token、$GITHUB_TOKEN 或 .secrets/github-token 提供。")
        return 1
    gh = GitHub(token, args.repo)

    if args.list:
        for r in gh.list_releases():
            print(f"  {r['tag_name']:<16} {r['name']}  assets={len(r['assets'])}  draft={r['draft']}")
        return 0

    if not args.tag:
        print("[错误] 需要 --tag")
        return 1

    body = args.notes
    if args.notes_file:
        body = Path(args.notes_file).read_text(encoding="utf-8")

    assets = [Path(a).resolve() for a in args.asset]
    names = list(args.asset_name)
    if names and len(names) != len(assets):
        print(f"[错误] --asset-name 有 {len(names)} 个，--asset 有 {len(assets)} 个，必须一一对应")
        return 1
    if not names:
        names = [a.name for a in assets]
    for a in assets:
        if not a.is_file():
            print(f"[错误] 资产不存在: {a}")
            return 1

    print(f"仓库 : {args.repo}")
    print(f"tag  : {args.tag}")
    for a, n in zip(assets, names):
        print(f"资产 : {a.name}  ->  上传名 {n}  ({human(a.stat().st_size)})  sha256={sha256_of(a)[:16]}…")

    rel = gh.get_release_by_tag(args.tag)
    if rel:
        print(f"\n[1/2] Release {args.tag} 已存在，更新标题/说明")
        rel = gh.update_release(rel["id"], name=args.title or rel["name"], body=body or rel["body"])
    else:
        print(f"\n[1/2] 创建 Release {args.tag}")
        rel = gh.create_release(args.tag, name=args.title, body=body,
                                draft=args.draft, prerelease=args.prerelease)
    print(f"      id={rel['id']}  url={rel['html_url']}")

    existing = {a["name"]: a for a in rel.get("assets", [])}
    upload_url = rel["upload_url"]
    if not assets:
        print("\n（没有要上传的资产）")
        return 0

    print(f"\n[2/2] 上传 {len(assets)} 个资产")
    for a, n in zip(assets, names):
        if n in existing:
            if not args.replace:
                print(f"      跳过 {n}（已存在；要覆盖加 --replace）")
                continue
            print(f"      删除已存在的同名资产 {n}")
            gh.delete_asset(existing[n]["id"])
        print(f"      上传 {n} ({human(a.stat().st_size)}) …")
        status, info = gh.upload_asset(upload_url, a, name=n)
        print(f"      -> {status}  {info.get('name')}  {info.get('browser_download_url')}")
        # 校验 GitHub 记录的体积与本地一致（哈希由调用方另行核对下载件）
        if info.get("size") != a.stat().st_size:
            print(f"      [警告] 远端体积 {info.get('size')} != 本地 {a.stat().st_size}")
            return 1

    print("\n完成。Release 页面:", rel["html_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
