// app.js — PDF 快速检索阅读器前端逻辑
const $ = (id) => document.getElementById(id);
const qEl = $("q"), btnSearch = $("btnSearch"), hitList = $("hitList");
const scrollWrap = $("scrollWrap"), pageHost = $("pageHost");
const pageNoEl = $("pageNo"), pageTotalEl = $("pageTotal");

const state = {
  meta: null, pdfDoc: null, pdfjs: null,
  pageNum: 1, scale: 1.0, renderSeq: 0,
  highlights: new Map(),      // page -> rects(高亮框, PDF 点坐标)
  resultPages: [], resultIndex: -1,
  textMode: false,
};

async function api(path, opt) {
  const r = await fetch(path, opt);
  if (!r.ok) {
    let e = {};
    try { e = await r.json(); } catch (_) {}
    throw new Error(e.error || `${r.status} ${r.statusText}`);
  }
  return r.json();
}
const esc = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ================= 构建缓存界面 ================= */
function setPill(txt, cls) {
  const p = $("statusPill"); p.textContent = txt;
  p.className = "pill" + (cls ? " " + cls : "");
}

// 诊断条：把“页面看到的服务器状态”直接显示出来，便于排查
function dbg(text) {
  const el = $("dbg");
  el.hidden = false;
  el.textContent = "diagnose: " + text;
}

function metaToDbg(m) {
  if (!m) return "meta 为空";
  return `${location.pathname} | ready=${m.ready} building=${m.building} ` +
    `pages=${m.pages ?? "?"} ocr=${m.ocr_pages ?? "?"} docid=${(m.docid || "").slice(0, 8)} ` +
    `prog=${(m.progress && m.progress.state) || "-"}`;
}

function delay(ms) { return new Promise((r) => setTimeout(r, ms)); }

// 带超时的进度请求：服务器卡住时不会让页面永远停在“准备中…”
async function fetchProgress(timeoutMs = 10000) {
  const c = new AbortController();
  const t = setTimeout(() => c.abort(), timeoutMs);
  try {
    const r = await fetch("/api/cache/progress", { signal: c.signal, cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}

function wireStartButton(btn, errEl, label) {
  btn.hidden = false;
  btn.textContent = label || "开始构建缓存";
  btn.disabled = false;
  btn.onclick = async () => {
    btn.disabled = true;
    if (errEl) errEl.hidden = true;
    try {
      const r = await fetch("/api/cache/build", { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (j && j.building && !j.started) {
        if (errEl) {
          errEl.hidden = false;
          errEl.textContent = "另一个文档正在后台构建缓存，请稍等它完成后再点这里（或刷新页面）。";
        }
        btn.disabled = false;
        return;
      }
      if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
      setPill("正在建缓存", "busy");
      $("buildTitle").textContent = "正在为文档建立 OCR 文字层缓存…";
      pollBuild();
    } catch (e) {
      if (errEl) { errEl.hidden = false; errEl.textContent = e.message; }
      btn.disabled = false;
    }
  };
}

function showOverlayMessage(txt) {
  $("buildOverlay").hidden = false;
  $("buildDetail").textContent = txt;
  $("buildError").hidden = true;
  $("btnStartBuild").hidden = true;
}

async function pollBuild() {
  const ov = $("buildOverlay");
  ov.hidden = false;
  const bar = $("buildBar"), detail = $("buildDetail"), err = $("buildError");
  $("btnStartBuild").hidden = true;
  let fails = 0;
  for (;;) {
    // 每轮都重新拉 meta：构建可能已完成、被取消或根本没在跑
    let m = null;
    try { m = await api("/api/meta", { cache: "no-store" }); } catch (_) {}
    if (m) {
      state.meta = m;
      dbg(metaToDbg(m));
      if (m.ready) {
        ov.hidden = true;
        setPill("索引就绪", "ok");
        initViewer();
        return;
      }
      // 服务器其实没在构建（如 --no-auto-build 或构建进程已退出）
      if (!m.building && (!m.progress || (m.progress.state !== "ocr" &&
          m.progress.state !== "scan" && m.progress.state !== "indexing"))) {
        setPill("缓存缺失", "");
        $("buildTitle").textContent = "文档还没有可检索的缓存";
        detail.textContent = "服务器当前没有在构建。你可以手动开始，或检查启动命令是否带 --no-auto-build。";
        err.hidden = true;
        wireStartButton($("btnStartBuild"), err);
        return;
      }
    }
    let pr = null;
    try { pr = await fetchProgress(); fails = 0; }
    catch (_) {
      if (++fails >= 3) {
        setPill("连接异常", "");
        detail.textContent = "连续多次无法读取构建进度，服务器可能已停止。";
        err.hidden = false;
        err.textContent = "请确认服务器进程还在运行（启动窗口未关闭），然后刷新页面。";
        wireStartButton($("btnStartBuild"), err, "重试连接");
        return;
      }
      await delay(900);
      continue;
    }
    if (pr.state === "error") {
      err.hidden = false;
      err.textContent = pr.error || "构建失败，点击下方按钮重试";
      setPill("构建失败", "");
      wireStartButton($("btnStartBuild"), err, "重新构建");
      return;
    }
    if (pr.state === "done") {
      // 等 meta.ready 跟上（索引刚落盘）
      await delay(400);
      continue;
    }
    const total = pr.total || 1, done = pr.done || 0;
    const pct = Math.min(100, Math.round((done / total) * 100));
    bar.style.width = pct + "%";
    let d = pr.phase || "";
    if (pr.state === "ocr" && pr.avg_ms) {
      const eta = pr.eta_s ? `，预计还需 ${Math.ceil(pr.eta_s / 60)} 分钟` : "";
      d += `　已完成 ${done}/${total} 页 · 平均 ${pr.avg_ms}ms/页${eta}`;
    } else if (pr.state === "indexing") {
      d += `　${pct}%`;
    }
    detail.textContent = d;
    await delay(900);
  }
}

async function boot() {
  try {
    state.meta = await api("/api/meta");
  } catch (e) {
    $("docInfo").textContent = "服务器不可用：" + e.message;
    dbg("fetch /api/meta 失败: " + e.message);
    return;
  }
  const m = state.meta;
  dbg(metaToDbg(m));
  const mb = (m.size / 1e6).toFixed(0);
  $("docInfo").textContent = `${m.file}　·　${mb} MB　·　共 ${m.pages || "?"} 页　·　OCR ${m.ocr_pages ?? 0} 页`;
  if (m.ready) {
    setPill("索引就绪", "ok");
    initViewer();
  } else if (m.building) {
    setPill("正在建缓存", "busy");
    pollBuild();
  } else {
    // 未就绪且未在构建：给手动按钮
    setPill("缓存缺失", "");
    showOverlayMessage("该文档还没有 OCR 文字层缓存。首次构建会逐页识别文字（大文件耗时较长，可后台进行）。");
    wireStartButton($("btnStartBuild"), $("buildError"));
  }
}

/* ================= pdf.js 阅读器 ================= */
async function loadPdfjs() {
  if (state.pdfjs) return state.pdfjs;
  const lib = await import("/vendor/pdfjs/build/pdf.mjs");
  lib.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/build/pdf.worker.mjs";
  state.pdfjs = lib;
  return lib;
}

async function initViewer() {
  setPill("加载文档…", "busy");
  const lib = await loadPdfjs();
  try {
    const task = lib.getDocument({
      url: "/api/pdf",
      disableAutoFetch: false,
      isEvalSupported: false,
    });
    state.pdfDoc = await task.promise;
  } catch (e) {
    setPill("文档加载失败");
    $("docInfo").textContent = "PDF 加载失败：" + e.message;
    return;
  }
  pageTotalEl.textContent = String(state.pdfDoc.numPages);
  setPill(`共 ${state.pdfDoc.numPages} 页`, "ok");
  if (!state.meta.pages) state.meta.pages = state.pdfDoc.numPages;
  bindViewerEvents();
  loadToc();          // 有书签则启用“目录…”下拉
  setZoomFit();       // 首屏按容器宽度自适应并渲染第 1 页
}

/* ---------- 书签目录跳转 ---------- */
let tocEntries = [];
async function loadToc() {
  const sel = $("tocSel");
  try {
    const outline = await state.pdfDoc.getOutline();
    if (!outline || !outline.length) return;
    const entries = [];
    const walk = (items, depth) => {
      for (const it of items) {
        if (!it.title) continue;
        entries.push({ title: it.title, dest: it.dest, depth });
        if (depth < 2) walk(it.items || [], depth + 1);
      }
    };
    walk(outline, 1);
    if (!entries.length) return;
    tocEntries = entries;
    sel.innerHTML = "";
    const ph = document.createElement("option");
    ph.value = ""; ph.textContent = "目录…";
    sel.appendChild(ph);
    entries.forEach((e, i) => {
      const op = document.createElement("option");
      op.value = String(i);
      op.textContent = "　".repeat(Math.max(0, e.depth - 1)) + e.title;
      op.title = e.title;
      sel.appendChild(op);
    });
    sel.hidden = false;
    sel.addEventListener("change", () => {
      const v = parseInt(sel.value, 10);
      if (!isNaN(v)) gotoOutline(v);
      sel.value = "";
    });
  } catch (e) {
    console.warn("outline:", e);
  }
}

async function gotoOutline(i) {
  const e = tocEntries[i];
  if (!e) return;
  try {
    let dest = e.dest;
    if (typeof dest === "string") dest = await state.pdfDoc.getDestination(dest);
    const ref = dest && dest[0];
    if (ref && typeof ref === "object" && "num" in ref) {
      const pi = await state.pdfDoc.getPageIndex(ref);
      goPage(pi + 1);
      return;
    }
  } catch (_) {}
  // 解析失败：尝试按页码粗跳（部分书签带数字页码）
  const m = /第\s*(\d+)\s*页/.exec(e.title || "");
  if (m) goPage(parseInt(m[1], 10));
}

/* ---------- 切换文档 ---------- */
function fmtSize(n) {
  if (n < 0) return "";
  if (n < 1e6) return Math.max(1, Math.round(n / 1024)) + "KB";
  return (n / 1e6).toFixed(0) + "MB";
}

async function openPdfNow(path, errEl) {
  if (errEl) errEl.hidden = true;
  try {
    const r = await fetch("/api/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pdf: path }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.switched) {
      if (errEl) { errEl.hidden = false; errEl.textContent = (j && j.error) || ("HTTP " + r.status); }
      return;
    }
    location.reload();
  } catch (e) {
    if (errEl) { errEl.hidden = false; errEl.textContent = String(e); }
  }
}

async function refreshLibrary() {
  const box = $("dmLibrary");
  box.innerHTML = '<div class="dm-item" style="color:var(--muted)">加载中…</div>';
  let data;
  try { data = await api("/api/library"); }
  catch (e) { box.innerHTML = ""; return; }
  box.innerHTML = "";
  const cur = data.current_docid;
  if (!data.items || !data.items.length) {
    box.innerHTML = '<div class="dm-item" style="color:var(--muted)">（暂无文档，用上面输入框添加）</div>';
    return;
  }
  data.items.forEach((it) => {
    const row = document.createElement("div");
    row.className = "dm-item";
    const st = it.ready ? "ok" : (it.building ? "busy" : "none");
    const stTxt = it.ready ? `就绪 · ${it.pages}页`
      : (it.building ? "构建中…" : "未建缓存");
    row.innerHTML =
      `<span class="dot ${st}"></span>` +
      `<span class="nm" title="${esc(it.path)}">${esc(it.name)}${it.exists ? "" : "（文件缺失）"}</span>` +
      `<span class="meta">${stTxt} · ${fmtSize(it.size)}</span>` +
      `<span class="act">${cur === it.docid ? "当前 ✓" : "打开 →"}</span>`;
    row.onclick = () => {
      if (cur === it.docid) { $("docModal").hidden = true; return; }
      openPdfNow(it.path, $("dmErr"));
    };
    box.appendChild(row);
  });
}

async function scanFolder() {
  const f = $("dmFolder").value.trim(), err = $("dmErr"), box = $("dmFolderList");
  err.hidden = true;
  if (!f) { err.hidden = false; err.textContent = "请输入文件夹路径"; return; }
  box.hidden = false;
  box.innerHTML = '<div class="dm-item">扫描中…</div>';
  try {
    const data = await api("/api/pdfs?folder=" + encodeURIComponent(f));
    box.innerHTML = "";
    if (!data.items || !data.items.length) {
      box.innerHTML = '<div class="dm-item">该文件夹没有 PDF 文件</div>';
      return;
    }
    data.items.forEach((it) => {
      const row = document.createElement("div");
      row.className = "dm-item";
      row.innerHTML =
        `<span class="dot none"></span><span class="nm" title="${esc(it.path)}">${esc(it.name)}</span>` +
        `<span class="meta">${fmtSize(it.size)}</span><span class="act">打开 →</span>`;
      row.onclick = () => openPdfNow(it.path, err);
      box.appendChild(row);
    });
  } catch (e) {
    box.innerHTML = "";
    err.hidden = false;
    err.textContent = "扫描失败：" + e.message;
  }
}

function openDocModal() {
  $("docModal").hidden = false;
  refreshLibrary();
}

function containerFitScale(page) {
  const avail = scrollWrap.clientWidth - 16;
  const view1 = page.getViewport({ scale: 1 });
  return Math.max(0.2, (avail / view1.width) * 0.98);
}

async function renderPage(n) {
  if (!state.pdfDoc) return;
  const seq = ++state.renderSeq;
  n = Math.min(Math.max(1, n), state.pdfDoc.numPages);
  state.pageNum = n;
  pageNoEl.value = String(n);
  pageHost.innerHTML = "";
  if (state.textMode) { $("textPanel").hidden = true; }

  try {
    const page = await state.pdfDoc.getPage(n);
    if (seq !== state.renderSeq) return;
    if (state.scale <= 0.01) state.scale = containerFitScale(page);

    const viewport = page.getViewport({ scale: state.scale });
    const dpr = window.devicePixelRatio || 1;

    const wrap = document.createElement("div");
    wrap.className = "page-wrap";
    wrap.style.width = viewport.width + "px";

    const canvas = document.createElement("canvas");
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = viewport.width + "px";
    canvas.style.height = viewport.height + "px";
    const ctx = canvas.getContext("2d");
    if (dpr !== 1) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);  // HiDPI：先放大坐标系
    wrap.appendChild(canvas);

    const overlay = document.createElement("div");
    overlay.className = "overlay";
    overlay.style.width = viewport.width + "px";
    overlay.style.height = viewport.height + "px";
    wrap.appendChild(overlay);

    pageHost.appendChild(wrap);

    await page.render({
      canvasContext: ctx,
      viewport,
    }).promise;
    if (seq !== state.renderSeq) return;

    // 高亮命中框（PDF 点坐标 -> viewport CSS 坐标）
    const rects = state.highlights.get(n) || [];
    for (const r of rects) {
      const c = viewport.convertToViewportRectangle([r.x0, r.y0, r.x1, r.y1]);
      const minX = Math.min(c[0], c[2]), minY = Math.min(c[1], c[3]);
      const maxX = Math.max(c[0], c[2]), maxY = Math.max(c[1], c[3]);
      const div = document.createElement("div");
      div.className = "hl";
      div.style.left = minX + "px";
      div.style.top = minY + "px";
      div.style.width = (maxX - minX) + "px";
      div.style.height = (maxY - minY) + "px";
      if (r.t) div.title = r.t;
      overlay.appendChild(div);
    }
    markActiveResult(n);
  } catch (e) {
    if (seq === state.renderSeq) console.error(e);
  }
}

function goPage(n, opts) {
  opts = opts || {};
  if (state.pdfDoc && n >= 1 && n <= state.pdfDoc.numPages) {
    state.pageNum = n;
    pageNoEl.value = String(n);
    scrollWrap.scrollTop = 0;
    renderPage(n);
  }
}

function zoomBy(f) {
  state.scale = Math.min(8, Math.max(0.2, state.scale * f));
  $("zoomPct").textContent = Math.round(state.scale * 100) + "%";
  renderPage(state.pageNum);
}

function setZoomFit() {
  if (!state.pdfDoc) return;
  (async () => {
    const page = await state.pdfDoc.getPage(state.pageNum);
    state.scale = containerFitScale(page);
    $("zoomPct").textContent = Math.round(state.scale * 100) + "%";
    renderPage(state.pageNum);
  })();
}

function bindViewerEvents() {
  $("btnPrev").onclick = () => goPage(state.pageNum - 1);
  $("btnNext").onclick = () => goPage(state.pageNum + 1);
  $("zoomIn").onclick = () => zoomBy(1.25);
  $("zoomOut").onclick = () => zoomBy(0.8);
  $("fitPage").onclick = setZoomFit;
  pageNoEl.addEventListener("change", () => {
    const v = parseInt(pageNoEl.value, 10);
    if (!isNaN(v)) goPage(v);
  });
  $("btnText").onclick = toggleText;
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") {
      if (e.key === "Escape") e.target.blur();
      return;
    }
    if (e.key === "ArrowRight" || e.key === "PageDown") goPage(state.pageNum + 1);
    else if (e.key === "ArrowLeft" || e.key === "PageUp") goPage(state.pageNum - 1);
    else if (e.key === "/") { e.preventDefault(); qEl.focus(); }
    else if (e.key === "j") goPage(state.pageNum + 1);
    else if (e.key === "k") goPage(state.pageNum - 1);
  });
  let rzT;
  window.addEventListener("resize", () => {
    clearTimeout(rzT); rzT = setTimeout(setZoomFit, 250);
  });
}

/* ================= 文本面板 ================= */
async function toggleText() {
  state.textMode = !state.textMode;
  const tp = $("textPanel");
  if (!state.textMode) { tp.hidden = true; return; }
  tp.hidden = false;
  try {
    const d = await api("/api/page?n=" + state.pageNum);
    let html = `———— 第 ${state.pageNum} 页文字（${d.mode === "ocr" ? "OCR 识别" : "原生文字层"}）————\n\n`;
    html += esc(d.text);
    tp.innerHTML = html;
    tp.scrollIntoView({ block: "start" });
  } catch (e) { tp.textContent = "获取失败：" + e.message; }
}

/* ================= 检索 ================= */
function buildSnippet(html, marks) {
  const out = [];
  let pos = 0;
  for (const [s, e] of marks || []) {
    if (s < pos) continue;
    out.push(esc(html.slice(pos, s)));
    out.push("<mark>" + esc(html.slice(s, e)) + "</mark>");
    pos = e;
  }
  out.push(esc(html.slice(pos)));
  return out.join("");
}

const OPT_KEY = "pdfreader_opts";

function readOpts() {
  return {
    mode: $("optMode").value,
    single: $("optSingle").checked ? "1" : "0",
    ctx: $("optCtx").value,
    limit: $("optLimit").value,
  };
}
function persistOpts() {
  try { localStorage.setItem(OPT_KEY, JSON.stringify(readOpts())); } catch (_) {}
}
function applyOptsToUI(o) {
  if (!o) return;
  if (o.mode) $("optMode").value = o.mode;
  $("optSingle").checked = o.single === "1";
  if (o.ctx) $("optCtx").value = String(o.ctx);
  if (o.limit) $("optLimit").value = String(o.limit);
}

function showResultSummary(res) {
  const el = $("resultSummary");
  if (res.error) { el.textContent = res.error; return; }
  const miss = res.missing && res.missing.length ? `（未命中词：${res.missing.join("、")}）` : "";
  const how = res.match_type === "phrase" ? "· 整词命中" : "· 分词匹配";
  el.textContent = res.total > 0
    ? `${how}　共 ${res.total} 页命中 · 用时 ${res.took_ms}ms${miss}`
    : `无命中${miss ? " " + miss : ""} · ${res.took_ms}ms`;
}

async function doSearch() {
  const q = qEl.value.trim();
  if (!q) return;
  const o = readOpts();
  persistOpts();
  const p = new URLSearchParams({ q, limit: o.limit, mode: o.mode, single: o.single, ctx: o.ctx });
  let res;
  try {
    res = await api("/api/search?" + p.toString());
  } catch (e) {
    showResultSummary({ error: e.message });
    return;
  }
  showResultSummary(res);
  hitList.innerHTML = "";
  state.resultPages = [];
  state.highlights.clear();
  state.resultIndex = -1;

  if (!res.hits || !res.hits.length) {
    hitList.innerHTML = '<div class="empty">没有找到匹配内容。试试其它关键词，或查看是否仍处于索引构建中。</div>';
    return;
  }
  res.hits.forEach((h, i) => {
    state.highlights.set(h.page, h.regions || []);
    const item = document.createElement("div");
    item.className = "hit";
    item.dataset.i = String(i);
    item.innerHTML =
      `<div class="h-top"><span class="h-page">第 ${h.page} 页 · ${h.n} 处</span>` +
      `<span class="h-terms">${esc((h.terms || []).join(" / "))}</span></div>` +
      `<div class="h-snip">${buildSnippet(h.snippet || "", h.marks)}</div>`;
    item.addEventListener("click", () => navToResult(i));
    hitList.appendChild(item);
    state.resultPages.push(h.page);
  });
  navToResult(0);
}

function navToResult(i) {
  if (!state.resultPages.length) return;
  i = Math.max(0, Math.min(i, state.resultPages.length - 1));
  state.resultIndex = i;
  const pg = state.resultPages[i];
  goPage(pg);
  markActiveResult(pg);
  const item = hitList.querySelector(`[data-i="${i}"]`);
  if (item) {
    item.classList.add("active");
    item.scrollIntoView({ block: "nearest" });
  }
}

function markActiveResult(_pg) {
  hitList.querySelectorAll(".hit").forEach((el) => {
    el.classList.toggle("active", parseInt(el.dataset.i, 10) === state.resultIndex);
  });
}

$("btnSearch").onclick = doSearch;
qEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    doSearch();
  }
});
$("prevHit").onclick = () => navToResult(state.resultIndex - 1);
$("nextHit").onclick = () => navToResult(state.resultIndex + 1);
$("btnStartBuild").onclick = null; // boot 里绑定
$("btnCloseOverlay").onclick = () => { $("buildOverlay").hidden = true; };

// ---- 切换文档
$("btnSwitch").onclick = openDocModal;
$("btnCloseDoc").onclick = () => { $("docModal").hidden = true; };
$("docModal").addEventListener("click", (e) => {
  if (e.target && e.target.id === "docModal") $("docModal").hidden = true;
});
$("dmOpen").onclick = () => {
  const p = $("dmPath").value.trim();
  if (p) openPdfNow(p, $("dmErr"));
};
$("dmPath").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("dmOpen").click(); }
});
$("dmScan").onclick = scanFolder;
$("dmFolder").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); scanFolder(); }
});

// ---- 检索选项：本地保存；修改后若有查询词则自动重搜
(function initOpts() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(OPT_KEY) || "{}"); } catch (_) {}
  applyOptsToUI(saved);
  const rerun = () => {
    persistOpts();
    if (qEl.value.trim() && state.meta && state.meta.ready) doSearch();
  };
  ["optMode", "optCtx", "optLimit"].forEach((id) => {
    $(id).addEventListener("change", rerun);
  });
  $("optSingle").addEventListener("change", rerun);
})();

boot();
