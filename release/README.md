# 发布与分发（GitHub 方案）

## 一句话结论

**仓库里只放源码（实测 27 个文件、182 KB），exe 和 OCR 缓存全部走 [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。**

原因（几条硬限制）：

| 载体 | 限制 | 143 MB 的包会怎样 |
|---|---|---|
| 仓库普通文件 | 单文件 > 50 MB 警告、**> 100 MB 直接拒绝推送** | 推不上去 |
| 仓库 + Git LFS | 免费额度按**存储+带宽**计费，超出后 LFS 使用被锁到下月 | 一次下载就吃掉大半配额 |
| **Release 资产** | 单个文件上限 **2 GB**，走 CDN，不吃 LFS 配额 | 103 MB（7z）轻松放下 ✅ |

参考：[About large files on GitHub](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)、
[Git LFS 计费说明](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)。

## 体积实测（本项目真实数据）

| 格式 | 体积 | 压缩耗时 |
|---|---|---|
| zip（deflate） | 143.3 MB | — |
| tar.gz | 141.3 MB | 13 s |
| tar.zst | 133.7 MB | 4 s |
| **7z（LZMA2 -mx=9）** | **103.3 MB** | 108 s |

`make_release.py --archive 7z` 会自动调用 `C:\Program Files\7-Zip\7z.exe`；
没装 7-Zip 就退回 zip 并提示。**只有预置缓存的独立资产仅 6.6 MB**（缓存主体是 OCR 文本
JSON，deflate 就压得很好），让"只想拿缓存"的人不必拖整包。

## 两种给别人用的方式

**A. 小白/同学（推荐）**：直接下 Release 里的 `PDFReader-win64.7z`，
解压双击 `PDF阅读器.exe`。解 `.7z` 需要 7-Zip / WinRAR（`.zip` 资源管理器自带）。

**B. 想自己构建的人**：克隆仓库 → 按 README「快速开始」装依赖 →
可选地拉取预置缓存：

```powershell
powershell -ExecutionPolicy Bypass -File release\fetch_cache.ps1 `
    -Repo <owner>/<repo> -Tag cache-latest
python tools\make_release.py --cache-src staging\ocr-cache --archive 7z
```

## 发布流程（手动，最稳）

```bat
:: 1) 本地出包（7z 最小）
package.cmd --release              :: 或手动两步：
.venv\Scripts\python.exe -m PyInstaller pdfreader.spec --noconfirm
.venv\Scripts\python.exe tools\make_release.py --archive 7z --cache-archive

:: 2) 冒烟自检（服务器/前端资源/缓存命中/Range 四项）
.venv\Scripts\python.exe tools\smoke_release.py --exe "dist\PDFReader\PDF阅读器.exe"

:: 3) 用 API 直接发 Release（不需要 gh CLI，也不需要 git 推送）
set GITHUB_TOKEN=<你的PAT>
.venv\Scripts\python.exe tools\publish_release.py --tag v1.0.0 ^
    --asset "dist\PDFReader-win64.7z" --asset "dist\pdfreader-cache.zip"
```

`publish_release.py` 走 REST API（Python 的 TLS 在本机可用，不受 git 的 schannel 问题影响），
流式上传、自动跳过或覆盖同名资产（`--replace`），并在上传后校验远端体积与本地一致。
也可以手动在网页上传：**Releases → Draft a new release** → 新建 tag → 拖入 assets → Publish。

> **资产名必须是 ASCII**：GitHub 上传接口的 `?name=` 会把非 ASCII 字符吞掉，
> 实测 `PDFReader-分享包.7z` 上传后变成 `PDFReader-.7z`。所以 `make_release.py` 固定产出
> `PDFReader-win64.7z` / `PDFReader-win64.zip` / `pdfreader-cache.zip`；
> `publish_release.py` 遇到非 ASCII 资产名会直接报错而不是悄悄传上去。
> 压缩包**内部**的解压目录仍是 `PDFReader\`。

> 没装 7-Zip 时 `--archive 7z` 会跳过并提示，此时改成 `--archive zip` 即可
> （体积 143 MB，仍远低于 Release 的 2 GB 上限）。

## 发布流程（自动，GitHub Actions）

`.github/workflows/release.yml` 已经写好：打 tag 或手动触发 → `windows-latest` 上
装依赖、构建、冒烟测试、把资产挂到 Release。

```
git tag v1.0.0 && git push origin v1.0.0     # 触发
```

注意：**CI 里不会重新 OCR**（runner 没有你那几本书，跑几十分钟 OCR 也不划算），
它只会去名为 `cache-latest` 的 Release 里抓 `pdfreader-cache.zip`。
抓不到也能构建成功，只是包内不含预置缓存。要更新缓存，就用下面这一步。

## 缓存怎么更新（关键的一步）

缓存 = OCR 结果，只有真正 OCR 过那本书的机器才产得出来。流程：

```bat
:: 1) 本地把缓存整理成独立资产
.venv\Scripts\python.exe tools\make_release.py --cache-archive
::    产出 dist\pdfreader-cache.zip（只含预置的那几本，实测 6.6 MB）

:: 2) 把它挂到一个固定 tag 的 Release（例如 cache-latest），
::    下次打 v* tag 构建时 CI 会自动来取
```

`staging/ocr-cache/SHIPPED_BOOKS.json` 会记录这次预置了哪些 docid、页数、体积，
**不含绝对路径**，可以安全提交进仓库，方便日后核对/复现。

## 为什么不把缓存提交进仓库

1. 22 MB 的 JSON 每次重建都产生全新 blob，git 历史会持续膨胀（不可逆）；
2. 缓存里的 `meta.json` 含**打包机的绝对路径**（`src`），提交等于泄露本机目录结构；
   `make_release.py` 打资产时会剔除 `_last.json` / `_library.json` / `progress.json`，
   但 `src` 字段仍在 —— 作为 Release 资产分发问题不大，进 git 历史就不合适了；
3. 它是**可再生产物**：只要 PDF 一致，谁都能重新 OCR 出来。二进制产物不进版本库是基本原则。

## 其他 Git 注意事项

- `.gitignore` 已排除 `*.pdf` 和 `books/`：教材动辄几百 MB，且不一定有再分发授权。
  真要把样张提交进仓库，用 `git add -f` 明确覆盖。
- **不要把 `dist/` 拿去 commit**，哪怕只有一次：git 历史会永久保留那个 100 MB 的 blob，
  之后只能靠 `git filter-repo` 重写历史才能清掉。
- `cache/_rapidocr_cfg/` 是本地生成的引擎配置，也属于 `cache/`，一并忽略。
- 如果之前已经不小心提交过大文件，用 [git-filter-repo](https://github.com/newren/git-filter-repo)
  清理，而不是 `git rm`（`git rm` 不会缩小历史体积）。
