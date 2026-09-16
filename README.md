# PDF 快速检索阅读器（本地网页版）

面向「很大、很多页、可能没有文字层」的 PDF（如扫描书），先做一次 **OCR 建立文字层缓存**，
之后在本地网页里做**中文整词/分词检索**：毫秒级出结果，点击命中直接跳到对应页并**精确高亮命中词**。
支持在多个 PDF 之间随意切换，每个文件按内容自动对应自己的缓存，互不干扰。

- 纯本地运行：无外网依赖、不上传任何内容；用浏览器打开 localhost 页面操作。
- 渲染原 PDF 用本地 vendored 的 pdf.js；OCR 用 RapidOCR（PaddleOCR 模型 / onnxruntime）。
- 中文分词用 jieba；检索**短语优先**，避免把词切开造成整页误标。

---

## 一、功能一览

| 能力 | 说明 |
|---|---|
| 一键建缓存 | 首次打开自动后台 OCR（网页实时进度条 + 预计时间），支持**断点续跑** |
| 文字层自动识别 | 已有文字层的页直接抽取文字，不浪费 OCR |
| 中文智能检索 | **整词短语优先**，分词 AND/OR 兜底；多关键词用空格分隔 |
| 精确高亮 | 命中词切出字符级小框（不再整段高亮），随页渲染 |
| 多文档切换 | 顶栏“切换文档”：文档库 / 输入路径 / 扫描文件夹，缓存自动对应 |
| 检索设置 | 匹配模式、单字词开关、上下文长度、结果上限（自动记忆） |
| 书签目录跳转 | PDF 自带书签时工具栏出现“目录…”下拉 |
| GPU 加速 | 有 NVIDIA 显卡时可启用 CUDA，OCR 实测提速约 6 倍 |
| 本页文字查看 | 工具栏“文本”显示当前页 OCR/原生全文 |

---

## 二、快速开始（Windows）

```bat
:: 0) 准备（仅首次，需联网）
py -3.10 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
npm install        :: 下载 pdfjs-dist（离线渲染用）

:: 1) 一键启动：打开某个 PDF（首次会自动后台建缓存）
start_server.cmd --pdf "D:\docs\书.pdf"

:: 或分开两步（命令行建缓存，再启动服务器）
.venv\Scripts\python.exe tools\build_cache.py --pdf "D:\docs\书.pdf" --dpi 200
.venv\Scripts\python.exe tools\serve.py --pdf "D:\docs\书.pdf"

:: 想让程序自己挑书并自动开浏览器（等价于打包版双击）
.venv\Scripts\python.exe tools\launcher.py
```

浏览器打开 <http://127.0.0.1:8765/>：

1. 首次打开会在弹窗里显示 OCR 进度（页数 / 每页耗时 / 预计剩余），完成后自动进入检索界面；
2. 输入关键词回车 → 左侧结果列表 → 点击任意结果跳页并高亮命中词；
3. 顶栏 **切换文档** 可打开其它 PDF / 扫描整个文件夹；
4. 有书签的 PDF，阅读器工具栏会出现 **目录…** 下拉，可直接跳章节。

> 不带 `--pdf` 启动会默认打开上次使用的文档。`launcher.py`（以及打包版）会依次尝试
> 上次文档 → `books/` 里唯一一本 → 控制台编号选书 → 图形化选书框。

---

## 三、GPU 加速（可选，适合大书库）

默认 CPU OCR。若有 NVIDIA 显卡想启用 GPU（本仓库实测 RTX 4060 约快 6 倍）：

```bat
.venv\Scripts\python.exe -m pip uninstall -y onnxruntime
.venv\Scripts\python.exe -m pip install onnxruntime-gpu nvidia-cublas-cu12 ^
    nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 ^
    nvidia-curand-cu12 nvidia-cusolver-cu12 nvidia-cusparse-cu12
:: 启动时加 --gpu
start_server.cmd --pdf "D:\docs\书.pdf" --gpu
```

注意：
- 需要支持 CUDA 12.x 的 NVIDIA 驱动；程序已自动处理 NVIDIA 运行库 DLL 的搜索路径。
- 换回 CPU：`pip uninstall -y onnxruntime-gpu && pip install onnxruntime`，启动时不加 `--gpu`。

---

## 四、缓存机制（核心概念）

**缓存按“文件内容哈希”存放**，与路径无关：同一文件换目录/重命名后仍秒开；不同文件各自独立。

```
cache/<docid=内容sha256前16位>/
    meta.json       文档元信息(页数/DPI/坐标空间/时间)
    progress.json   构建进度（网页进度条读取）
    index.json      jieba 倒排索引（term -> 页 -> 词频）
    pages/p00001.json …
        每页 { pt: 页面尺寸(点), text: 阅读顺序全文,
               regions: 文本块(文本+PDF点坐标框+字符偏移), mode: ocr|native }
```

要点：
- **断点续跑**：OCR 中断/索引丢失后再次构建只补缺失部分（372 页全量缓存复用约 0.4s）。
- **坐标空间**：region 统一存 **PDF 用户空间（左下原点 / Y 向上）**，与 pdf.js 高亮直接对齐。
- 重跑整本：`python tools\build_cache.py --pdf xxx.pdf --force`；清缓存：删除对应 `cache/<docid>`。

### 为什么要 OCR 缓存？

原 PDF 无文字层 → 无法检索。OCR 一次后，检索只查索引/JSON（毫秒级），不再反复解码大 PDF；
页面渲染仍由 pdf.js 按需读原 PDF（HTTP Range），不额外存整页图片。

---

## 五、工具与参数

| 命令 | 作用 |
|---|---|
| `tools/serve.py` | 本地服务器（多文档）。`--pdf --port --dpi --workers --gpu --no-auto-build` |
| `tools/build_cache.py` | 命令行建缓存。`--pdf --dpi --workers --backend proc\|thread --intra N --gpu --force --no-index` |
| `tools/make_sample.py` | 生成“无文字层”合成扫描样本（自测用） |
| `tools/preflight.py` | 预扫描：页数 / 文字层情况 / 内嵌图等效 DPI |
| `tools/fix_coords.py` | 旧缓存坐标升级（→PDF 空间），幂等 |
| `start_server.cmd` | Windows 启动脚本（透传参数）；另有 `start_build.cmd` 纯建缓存 |

参数建议：
- `--dpi`：OCR 渲染分辨率，默认 200。用 `preflight.py` 看源图等效 DPI 后照填（如 190），太大无收益且慢。
- `--workers`：并行 OCR 引擎数，默认 `min(CPU核,6)`；GPU 下自动限制 ≤4。
- `--backend`：`thread`（服务器内默认；受限环境/免命名管道）或 `proc`（独立批处理可用）。

---

## 六、HTTP API（内置，仅供前端/调试）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/meta` | 当前文档状态（就绪/页数/进度/DPI） |
| GET | `/api/library` | 文档库（含每文档就绪/构建状态） |
| GET | `/api/pdfs?folder=` | 列出某文件夹内的 PDF |
| POST | `/api/open` `{pdf}` | 切换当前文档（自动入库/建缓存） |
| POST | `/api/library/add` `{pdf}` | 只加入文档库 |
| GET | `/api/search?q=&limit=&mode=&single=&ctx=` | 检索（mode: auto\|phrase\|terms） |
| GET | `/api/page?n=` | 某页全文 + regions |
| GET | `/api/pdf` | 原 PDF（HTTP Range，pdf.js 用） |
| GET | `/api/cache/progress` | 构建进度 |
| POST | `/api/cache/build` | 手动触发构建 |

---

## 七、检索行为说明

- 匹配模式：`auto`（默认，先整串精确匹配、有结果只标完整词，无结果才退分词）；
  `phrase`（仅整串）；`terms`（仅 jieba 分词）。
- 单字词默认不参与分词匹配，避免搜「位示图」退化成满页标「图」；想搜单字请在设置里勾选。
- OCR 错字会让整词匹配漏掉该词，页面会提示“未命中词”；可切 `terms` 或勾选单字辅助排查。
- 首次搜索需把全文载入内存（数百毫秒），之后每次约 10~30ms。

---

## 八、常见问题（FAQ）

- **网页一直停在右上角“加载文档…/加载文件”**：通常是没执行 `npm install`（缺 `node_modules/pdfjs-dist`）。
  在项目根目录执行 `npm install` 后 `Ctrl+Shift+R` 强刷；服务器启动时控制台也会打印对应警告。
- **弹窗一直“准备中 / 空进度条”**：`Ctrl+Shift+R` 强刷。仍异常时看页面顶部黑色诊断条：
  `ready=true` 说明服务器正常，多为旧页面缓存；`ready=false` 说明连的不是该服务器进程
  （端口被另一实例占用等），关掉多余实例只留一个。
- **端口占用**：启动失败就换端口 `--port 8766`。
- **高亮对不上文字**：新缓存已正确；若拷入旧版缓存目录，运行
  `python tools\fix_coords.py --cache cache\<docid>` 修复。
- **GPU 报缺 DLL**：确认 NVIDIA 运行库包已安装、驱动较新；CPU 模式不涉及。
- **个别页识别空/乱**：多为扫描页模糊/折页；点工具栏“文本”可查看该页 OCR 结果对照。
- **双栏报纸版式**：阅读顺序按“行→左→右”近似，跨栏衔接可能不完美，检索与高亮不受影响。

---

## 九、目录结构

```
pdfreader/
  tools/            OCR 管线 / 索引 / 服务器 / 小工具（见上表）
  web/              前端（index.html / app.js / style.css）
  node_modules/     pdfjs-dist（本地渲染依赖，npm install 生成）
  cache/<docid>/    OCR 缓存（按文件内容哈希；可安全删除重建）
  sample/           合成样本
  requirements.txt  Python 基础依赖（CPU 可用；GPU 额外包见“三”）
  start_server.cmd / start_build.cmd   一键脚本
```

## 十、打包分享（给别人用，无需装 Python）

一条命令产出绿色包：

```bat
package.cmd              :: 构建 + 组装 dist\PDFReader\
package.cmd --7z         :: 额外产出 dist\PDFReader-win64.7z（最小，需 7-Zip）
package.cmd --release    :: 再额外产出 dist\pdfreader-cache.zip（两个都传 Release）
package.cmd --all        :: 预置 cache\ 里的全部缓存（不止 4 本大书）
```

产物结构（解压双击即用，Windows 10/11 64 位，不需 Python / npm / 联网）：

```
dist/PDFReader/                  解压后 329 MB
  PDF阅读器.exe      双击：挑书 → 起服务 → 自动开浏览器
  _internal/         PyInstaller 运行时 + web/ + pdfjs 渲染库（302 MB）
  cache/             预置的 OCR 缓存（4 本，21.7 MB）
  books/             让对方把 PDF 放这里
  使用说明.txt        给非技术用户的说明
dist/PDFReader-win64.7z          实际发出去的东西：103 MB（zip 是 143 MB）
dist/pdfreader-cache.zip         只含预置缓存，供自行构建的人取用：6.6 MB
```

实测（本机 Python 3.10.11 / Win11）：预置 4 本王道 2027 教材缓存，
对方把同版本 PDF 丢进 `books/` 双击 exe → 直接 `ready=True`、秒出检索结果，全程无 OCR。
打包后自动校验方式见 `tools/smoke_release.py`（服务器就绪 / 前端资源 / 缓存命中 / Range 四项）。

### 传到 GitHub 的正确姿势

**仓库只放源码（实测 27 个文件、182 KB），exe 和缓存都走 Release 资产。** 仓库普通文件超过
100 MB 会被直接拒收；Git LFS 免费额度按存储+带宽计费，143 MB 的包一次下载就吃掉大半。
Release 单资产上限 2 GB 且走 CDN，这才是二进制该待的地方。

不想构建的话，直接去 **[Releases 页面](https://github.com/Ce-ReAL-ai/pdfreader/releases)** 下载现成的包。

```bat
:: 手动发布（推荐，最稳）
package.cmd --release
python tools\publish_release.py --tag v1.0.0 --asset "dist\PDFReader-win64.7z"

:: 或者打 tag 让 CI 自动构建
git tag v1.0.0 && git push origin v1.0.0     # 触发 .github/workflows/release.yml
```

CI 只会去 `cache-latest` 那个 Release 里抓预置缓存（runner 上没有你那几本书，不可能现场 OCR）。
缓存的更新、`fetch_cache.ps1` 的用法、以及为什么不把缓存提交进仓库，见
[`release/README.md`](release/README.md)。

> 资产名必须是 **ASCII**：GitHub 上传接口的 `?name=` 会把非 ASCII 字符吞掉
> （实测 `PDFReader-分享包.7z` 上去后变成 `PDFReader-.7z`），所以归档固定叫
> `PDFReader-win64.7z`。压缩包**内部**的解压目录仍是 `PDFReader\`。

设计要点：

- **onedir 而不是 onefile**：包内 400+ MB（onnxruntime/opencv/pymupdf），onefile 每次启动
  都要解压到临时目录，冷启动多等 5~15 秒且极易被杀软误报。
- **预置缓存是这套方案的关键**：`docid = 文件前 4 MB 的 sha256 前 16 位`，与路径无关。
  对方只要拿到**同一份** PDF，打开就是毫秒级检索，完全跳过 OCR（否则 372 页要等十几分钟）。
  书的内容不同（别的版本/重扫）会自动重新建缓存，不会拿错缓存出错误高亮。
- **可写数据一律落在 exe 同级目录**：`cache/` 由 `serve.set_cache_root()` /
  `pdfpipeline.set_cache_root()` 在启动时定位（`_MEIPASS` 是只读的，不能往那里写）；
  程序目录不可写时自动回退到 `%LOCALAPPDATA%\PDFReader\cache`。
- **默认 CPU 版**：GPU 版要额外塞 `onnxruntime-gpu` + 6 个 CUDA 轮子（多 1.5 GB+），
  且必须对方也是 NVIDIA 卡，只在确有需要时单独出包。
- **归档格式用 7z 而不是 zip**：同一份包 zip 143 MB、tar.zst 134 MB、7z(LZMA2) **103 MB**。
  `make_release.py` 会自动找 7-Zip，找不到就退回 zip 并提示。
- `.cmd` 脚本保持 **ASCII-only**：中文注释存成 UTF-8 后，在默认 GBK 控制台代码页下会被
  误解码，残留字节可能把下一行命令吞掉（`chcp 65001` 也救不了，因为 cmd 读文件用初始代码页）。

---

## 十一、技术要点（维护者速记）

- 坐标：OCR 像素坐标 /(dpi/72) → “左上原点/Y向下”点坐标 → 构建时翻转为 PDF 空间
  （`_flip_regions_y`）；前端用 `viewport.convertToViewportRectangle` 叠加高亮。
- 词级高亮：命中字符在 region 文本内的比例位置映射到 region 横坐标（近似字宽模型）。
- 并行：每线程独立 RapidOCR 实例（onnx 推理释放 GIL），按 `cpu/workers` 分配每引擎线程数防超订。
- 服务器多文档：`App.doc` 不可变快照承载当前文档，切换原子；同一时刻只允许一个后台构建。
