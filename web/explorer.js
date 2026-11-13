/* S1.1 报告段落浏览器 —— 上传 → 解析 → 指标 × 段落。无框架、无构建步骤。 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = (n) => Number(n || 0).toLocaleString("zh-CN");

  const MAX_MB = 120;
  const S = { file: null, url: null, data: null, view: null, sort: "hits", q: "", sel: null };
  const mq = window.matchMedia("(max-width: 1180px)");
  const STEPS = [
    "读取 PDF 文本层…",
    "切分段落（句末标点 → 380 字块）…",
    "按指标召回段落（命中词 × 是否带数据）…",
  ];

  const el = {
    drop: $("#drop"),
    idle: $("#dropIdle"),
    loading: $("#dropLoading"),
    error: $("#dropError"),
    loadName: $("#loadName"),
    loadStep: $("#loadStep"),
    loadTime: $("#loadTime"),
    bar: $("#bar"),
    errMsg: $("#errMsg"),
    file: $("#file"),
    ws: $("#ws"),
    rail: $("#rail"),
    pane: $("#pane"),
    pdfFrame: $("#pdfFrame"),
    pdfLoading: $("#pdfLoading"),
    pdfLoadingText: $("#pdfLoadingText"),
    pdfNewTab: $("#pdfNewTab"),
    btnPdfToggle: $("#btnPdfToggle"),
    docMeta: $("#docMeta"),
    btnNew: $("#btnNew"),
    btnJson: $("#btnJson"),
    btnPdf: $("#btnPdf"),
    toast: $("#toast"),
  };

  let loadTimer = null;
  let searchTimer = null;
  let toastTimer = null;
  let jumpToken = 0;
  let pdfLoadToken = 0;

  /* ------------------------------------------------------------ 上传 */
  function bindUpload() {
    $("#btnPick").addEventListener("click", () => el.file.click());
    $("#btnRetry").addEventListener("click", () => el.file.click());
    el.file.addEventListener("change", () => {
      const f = el.file.files && el.file.files[0];
      el.file.value = "";
      if (f) start(f);
    });

    let depth = 0;
    ["dragenter", "dragover"].forEach((ev) => el.drop.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "dragenter") depth += 1;
      el.drop.classList.add("dragging");
    }));
    ["dragleave", "drop"].forEach((ev) => el.drop.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "dragleave") depth = Math.max(0, depth - 1);
      else depth = 0;
      if (depth === 0) el.drop.classList.remove("dragging");
    }));
    el.drop.addEventListener("drop", (e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) start(f);
    });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());
  }

  async function start(file) {
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
      return toast("只支持 PDF 文件");
    }
    if (file.size > MAX_MB * 1024 * 1024) {
      return toast(`文件 ${(file.size / 1048576).toFixed(1)} MB，超过 ${MAX_MB} MB 上限`);
    }
    stopLoading();
    if (S.url) URL.revokeObjectURL(S.url);
    S.file = file;
    S.url = URL.createObjectURL(file);
    S.data = null;
    S.view = null;
    S.q = "";
    S.sel = null;

    showLoading(file);
    let res = null;
    let data = null;
    try {
      res = await fetch(`/api/analyze?name=${encodeURIComponent(file.name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/pdf" },
        body: file,
      });
      data = await res.json().catch(() => null);
    } catch (err) {
      stopLoading();
      return fail("连不上本地服务。确认 <code>python -m esg_s11_scorer.explorer</code> 还在运行。", true);
    }
    if (!res.ok) {
      stopLoading();
      return fail((data && data.error) || `服务返回 HTTP ${res.status}`);
    }
    stopLoading();
    render(data);
  }

  function showLoading(file) {
    el.idle.hidden = true;
    el.error.hidden = true;
    el.loading.hidden = false;
    el.drop.hidden = false;
    el.ws.hidden = true;
    el.loadName.textContent = file.name;
    el.loadStep.textContent = STEPS[0];
    el.loadTime.textContent = "已用 0.0s";
    el.bar.className = "s0";
    const t0 = performance.now();
    loadTimer = setInterval(() => {
      const s = (performance.now() - t0) / 1000;
      el.loadTime.textContent = `已用 ${s.toFixed(1)}s`;
      const step = s < 2.5 ? 0 : s < 8 ? 1 : 2;
      el.loadStep.textContent = STEPS[step];
      el.bar.className = `s${step}`;
    }, 250);
  }

  function stopLoading() {
    if (loadTimer) clearInterval(loadTimer);
    loadTimer = null;
  }

  function fail(msg, trusted) {
    el.loading.hidden = true;
    el.idle.hidden = true;
    el.error.hidden = false;
    // 服务端消息原样转义；只有本文件自带的提示才允许带 <code>
    el.errMsg.innerHTML = trusted ? msg : esc(msg);
  }

  /* ------------------------------------------------------------ 渲染 */
  function render(data) {
    S.data = data;
    S.view = pickDefault(data);
    el.drop.hidden = true;
    el.ws.hidden = false;
    el.btnNew.hidden = false;
    el.btnJson.hidden = false;
    renderMeta();
    renderRail();
    renderPane();
    setupPdf();
    document.title = `${data.file.name} · S1.1 段落浏览器`;
  }

  function pickDefault(data) {
    const hit = data.indicators.find((i) => i.n_passages > 0);
    return (hit || data.indicators[0]).key;
  }

  function renderMeta() {
    const f = S.data.file;
    const q = f.quality || {};
    const parts = [
      `<span class="name" title="${esc(f.name)}">${esc(f.name)}</span>`,
      `<span>${f.pages} 页</span>`,
      `<span>${fmt(f.chars)} 字</span>`,
    ];
    if (q.lang) parts.push(`<span>${esc(q.lang)}报告</span>`);
    parts.push(`<span class="${q.level && q.level !== "好" ? "warn" : ""}" title="${esc((q.reasons || []).join("；"))}">文本层 ${esc(q.level || "—")}</span>`);
    parts.push(`<span>${f.cached ? "缓存命中" : `解析 ${f.parse_seconds}s`}</span>`);
    el.docMeta.innerHTML = parts.join("");
  }

  function renderRail() {
    const items = S.data.indicators.map((ind) => {
      const sub = ind.part
        ? `${ind.part} · ${ind.name}`
        : (ind.name === ind.ind_name ? "整项 · 三档判据" : ind.name);
      const quiet = ind.n_passages ? "" : " quiet";
      const on = ind.key === S.view ? " on" : "";
      return `<button class="rail-item${on}${quiet}" data-key="${esc(ind.key)}">
        <span class="ri-head"><span class="ri-code">${esc(ind.ind)} · ${ind.weight}%</span>
          <span class="ri-count">${ind.n_passages}${ind.truncated ? "+" : ""}</span></span>
        <span class="ri-name">${esc(ind.ind_name)}</span>
        <span class="ri-sub">${esc(sub)}</span>
      </button>`;
    }).join("");
    const nf = (S.data.facts && S.data.facts.facts.length) || 0;
    const onF = S.view === "facts" ? " on" : "";
    el.rail.innerHTML = `<div class="rail-label">定性指标</div>${items}
      <div class="rail-label">定量</div>
      <button class="rail-item${onF}" data-key="facts">
        <span class="ri-head"><span class="ri-code">数值</span><span class="ri-count">${nf}</span></span>
        <span class="ri-name">数值证据</span>
        <span class="ri-sub">流失率 / 薪酬差距 / 员工数</span>
      </button>`;
  }

  function renderPane() {
    if (!S.data) return;
    if (S.view === "facts") {
      el.pane.innerHTML = factsHtml();
      el.pane.scrollTop = 0;
      return;
    }
    const ind = S.data.indicators.find((i) => i.key === S.view);
    if (!ind) return;
    const hitGroups = ind.groups.filter((g) => g.hits > 0).length;
    const partText = ind.part ? `定性分项 ${ind.part}` : "整项定性";
    const subName = ind.name === ind.ind_name ? "三档判据" : ind.name;
    const capped = ind.truncated ? `，仅显示前 ${ind.n_passages} 段` : "";
    el.pane.innerHTML = `
      <div class="pane-head">
        <div class="pane-kicker">${esc(ind.ind)} · ${esc(partText)}</div>
        <h1>${esc(ind.ind_name)}</h1>
        <p class="pane-sub">${esc(subName)} · 权重 <b>${ind.weight}%</b></p>
        <p class="pane-stat">召回 ${ind.n_passages} 段${capped} · 涉及 ${hitGroups}/${ind.groups.length} 组证据 · 点击段落跳到 PDF 对应页</p>
      </div>
      ${rubricHtml(ind)}
      ${kwHtml(ind)}
      <div class="toolbar">
        <input class="search" type="search" placeholder="在当前指标的 ${ind.n_passages} 段里搜原文或命中词…" value="${esc(S.q)}">
        <div class="seg">
          <button type="button" data-sort="hits" class="${S.sort === "hits" ? "on" : ""}">按命中</button>
          <button type="button" data-sort="page" class="${S.sort === "page" ? "on" : ""}">按页码</button>
        </div>
      </div>
      <div id="paras"></div>`;
    renderList(ind);
    el.pane.scrollTop = 0;
  }

  function rubricHtml(ind) {
    const tiers = ["100", "50", "0"].filter((t) => ind.tiers && ind.tiers[t] !== undefined);
    return `<details class="rubric">
      <summary>评分标准（100 / 50 / 0 三档判据）</summary>
      <div class="rubric-body">
        ${tiers.map((t) => `<div class="tier"><span class="tier-n">${t}</span><p>${esc(ind.tiers[t])}</p></div>`).join("")}
        ${ind.note ? `<p class="rubric-note">${esc(ind.note)}</p>` : ""}
        ${ind.gate ? `<p class="rubric-note">0 分前置条件：${esc(ind.gate)}</p>` : ""}
      </div>
    </details>`;
  }

  function kwHtml(ind) {
    const words = ind.groups.reduce((n, g) => n + g.words, 0);
    return `<details class="rubric">
      <summary>检索词表 · ${ind.groups.length} 组 / ${words} 词（英文按词首、中文按子串）</summary>
      <div class="rubric-body">${kwBody(ind)}</div>
    </details>`;
  }

  function kwBody(ind) {
    return `<div class="kwlist">${ind.groups.map((g) => `
      <div class="kwgroup"><b>${esc(g.name)} · ${g.words} 词</b>
        <span>${g.list.map(esc).join("、")}</span></div>`).join("")}</div>`;
  }

  function renderList(ind) {
    const box = $("#paras", el.pane);
    if (!box) return;
    let rows = ind.passages.slice();
    const q = S.q.trim().toLowerCase();
    if (q) {
      rows = rows.filter((r) => r.text.toLowerCase().includes(q)
        || r.words.some((w) => w.toLowerCase().includes(q)));
    }
    rows.sort(S.sort === "page"
      ? (a, b) => a.page - b.page || b.hits - a.hits
      : (a, b) => b.hits - a.hits || a.page - b.page);
    box.innerHTML = rows.length
      ? rows.map(paraHtml).join("")
      : emptyHtml(ind, q);
  }

  function markText(text, spans) {
    if (!spans || !spans.length) return esc(text);
    let out = "";
    let i = 0;
    for (const span of spans) {
      const s = span[0];
      const e = span[1];
      if (s < i) continue;
      out += esc(text.slice(i, s)) + `<mark>${esc(text.slice(s, e))}</mark>`;
      i = e;
    }
    return out + esc(text.slice(i));
  }

  function paraHtml(r) {
    // 同一页可能有两段：选中态用「页码 + 段首」而不是页码，避免整页一起高亮
    const key = `${r.page}:${r.text.slice(0, 40)}`;
    const sel = S.sel === key ? " sel" : "";
    const chips = r.words.slice(0, 8).map((w) => `<span class="chip">${esc(w)}</span>`).join("")
      + (r.words.length > 8 ? `<span class="chip more">+${r.words.length - 8}</span>` : "");
    return `<article class="para${sel}" data-page="${r.page}" data-key="${esc(key)}">
      <div class="para-head">
        <span class="pg">p${r.page}</span>
        <span class="hit">命中 ${r.hits} 词</span>
        <span class="chips">${chips}</span>
        <span class="spacer"></span>
        <span class="para-actions">
          <button type="button" class="link copy">复制</button>
          <button type="button" class="link jump" data-page="${r.page}">定位 ↗</button>
        </span>
      </div>
      <p class="txt">${markText(r.text, r.spans)}</p>
      <div class="para-foot">${r.groups.map(esc).join(" · ")}</div>
    </article>`;
  }

  function emptyHtml(ind, q) {
    if (q) {
      return `<div class="empty"><h3>没有匹配「${esc(q)}」的段落</h3>
        <p>清空搜索框，可以回去看这个指标召回的 ${ind.n_passages} 段原文。</p></div>`;
    }
    const words = ind.groups.reduce((n, g) => n + g.words, 0);
    return `<div class="empty">
      <h3>这个指标没有召回任何段落</h3>
      <p>常见原因：报告确实没写相关内容 · 词表里没有报告用的那种说法 · PDF 是扫描件（没有文本层）。<br>
        词表在 <code>keywords.csv</code>，加词后重启服务即可（匹配规则只有一份，改词不用改代码）。</p>
      <details><summary>看该指标的 ${ind.groups.length} 组检索词（${words} 个）</summary>${kwBody(ind)}</details>
    </div>`;
  }

  /* ------------------------------------------------------------ 数值证据 */
  function factValue(x) {
    if (x.unit === "%") return `${Number(Number(x.value).toFixed(2))}%`;
    return `${fmt(Math.round(x.value))} 人`;
  }

  function factsHtml() {
    const pack = S.data.facts || { facts: [], warnings: [] };
    const rows = pack.facts.slice().sort((a, b) => (b.best - a.best) || (a.field < b.field ? -1 : 1));
    const head = `<div class="pane-head">
      <div class="pane-kicker">定量证据 · 报告年度 ${pack.report_year || "未识别"}</div>
      <h1>数值证据</h1>
      <p class="pane-sub">S1.1.1 A/B 与 S1.1.2 A/B 用到的数字。标「采用」的是同年同字段去重择优后的取值；
        所有数字都能回到页码与原句。</p>
    </div>`;
    if (!rows.length) {
      return `${head}<div class="empty"><h3>没有抽到数值</h3>
        <p>报告里没找到可对位到年份的流失率 / 薪酬差距 / 员工数，S1.1.1 与 S1.1.2 的 A/B 会记 N/A。</p></div>
        ${warnHtml(pack.warnings)}`;
    }
    const body = rows.map((x) => {
      const conf = x.confidence === "高" ? "hi" : x.confidence === "低" ? "lo" : "";
      return `<tr class="${x.best ? "best" : ""}">
        <td><div>${esc(x.label)} ${x.best ? '<span class="badge hi">采用</span>' : ""}</div>
          <span class="quote">${esc(x.method_cn || x.method)}</span></td>
        <td class="num">${factValue(x)}</td>
        <td>${x.year ?? "—"}<br><button type="button" class="link jump" data-page="${x.page}">p${x.page}</button></td>
        <td><span class="badge ${conf}">${esc(x.confidence)}</span>
          <span class="quote">${esc(x.quote)}</span></td>
      </tr>`;
    }).join("");
    return `${head}
      <div class="facts-wrap">
      <table class="facts">
        <thead><tr><th>字段</th><th>数值</th><th>年度 / 页</th><th>置信 / 原文</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
      </div>
      ${pack.denom_note ? `<p class="facts-intro" style="margin-top:18px">分母口径：${esc(pack.denom_note)}</p>` : ""}
      ${warnHtml(pack.warnings)}`;
  }

  function warnHtml(warnings) {
    if (!warnings || !warnings.length) return "";
    return `<div class="warnings"><h4>抽取诊断（${warnings.length}）</h4>
      <ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`;
  }

  /* ------------------------------------------------------------ PDF 预览 */
  function setupPdf() {
    if (!S.url) return;
    showPdfLoading("正在载入 PDF…");
    el.pdfFrame.src = `${S.url}#page=1`;
    el.pdfNewTab.href = `${S.url}#page=1`;
    if (mq.matches) document.body.classList.remove("pdf-open");
    else document.body.classList.remove("pdf-hidden");
    syncPdfBtn();
  }

  function showPdfLoading(text) {
    pdfLoadToken += 1;
    el.pdfLoading.dataset.seq = String(pdfLoadToken);
    el.pdfLoadingText.textContent = text;
    el.pdfLoading.hidden = false;
  }

  function hidePdfLoadingSoon(seq) {
    // 大文件从 load 到真正画出第一页还有 1–3 秒（Edge 顶部那条蓝线就是这段），
    // 按文件大小估一个停留时间，避免遮罩撤早后露出白屏。
    const mb = (S.file ? S.file.size : 0) / 1048576;
    const wait = Math.min(6000, 700 + mb * 90);
    setTimeout(() => {
      if (seq === pdfLoadToken) el.pdfLoading.hidden = true;
    }, wait);
  }

  function jumpTo(page) {
    if (!S.url) return;
    openPdf();
    const url = `${S.url}#page=${page}`;
    el.pdfNewTab.href = url;
    showPdfLoading(`正在打开第 ${page} 页…`);
    // Chromium/Edge 的 PDF 阅读器不响应「只改 fragment」的导航（实测停在原页），
    // 所以先清空再载入：blob 是本地的，重排一次约几百毫秒，换来稳定定位。
    const token = ++jumpToken;
    el.pdfFrame.src = "about:blank";
    setTimeout(() => {
      if (token === jumpToken) el.pdfFrame.src = url;
    }, 40);
  }

  function openPdf() {
    if (mq.matches) document.body.classList.add("pdf-open");
    else document.body.classList.remove("pdf-hidden");
    syncPdfBtn();
  }

  function togglePdf() {
    if (mq.matches) document.body.classList.toggle("pdf-open");
    else document.body.classList.toggle("pdf-hidden");
    syncPdfBtn();
  }

  function syncPdfBtn() {
    const open = mq.matches
      ? document.body.classList.contains("pdf-open")
      : !document.body.classList.contains("pdf-hidden");
    el.btnPdfToggle.textContent = open ? "收起" : "预览";
  }

  /* ------------------------------------------------------------ 交互 */
  function bindWorkspace() {
    el.rail.addEventListener("click", (e) => {
      const b = e.target.closest(".rail-item");
      if (!b || !S.data) return;
      if (b.dataset.key === S.view) return;
      S.view = b.dataset.key;
      S.q = "";
      S.sel = null;
      renderRail();
      renderPane();
    });

    el.pane.addEventListener("click", (e) => {
      const jump = e.target.closest(".jump");
      if (jump) {
        jumpTo(Number(jump.dataset.page));
        return;
      }
      const copy = e.target.closest(".copy");
      if (copy) {
        const card = copy.closest(".para");
        const txt = card ? card.querySelector(".txt").innerText : "";
        navigator.clipboard.writeText(txt).then(() => toast("已复制这段原文"),
          () => toast("浏览器不允许剪贴板，手动选中即可"));
        return;
      }
      const sort = e.target.closest("[data-sort]");
      if (sort) {
        S.sort = sort.dataset.sort;
        $$("[data-sort]", el.pane).forEach((b) => b.classList.toggle("on", b === sort));
        const ind = S.data.indicators.find((i) => i.key === S.view);
        if (ind) renderList(ind);
        return;
      }
      const card = e.target.closest(".para");
      if (card) {
        $$(".para.sel", el.pane).forEach((p) => p.classList.remove("sel"));
        card.classList.add("sel");
        S.sel = card.dataset.key;
        jumpTo(Number(card.dataset.page));
      }
    });

    el.pane.addEventListener("input", (e) => {
      if (!e.target.classList.contains("search")) return;
      S.q = e.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        const ind = S.data && S.data.indicators.find((i) => i.key === S.view);
        if (ind) renderList(ind);
      }, 120);
    });

    el.btnPdfToggle.addEventListener("click", togglePdf);
    el.btnPdf.addEventListener("click", () => { togglePdf(); });

    el.pdfFrame.addEventListener("load", () => {
      if (!el.pdfFrame.src || el.pdfFrame.src === "about:blank") return;
      const seq = Number(el.pdfLoading.dataset.seq || 0);
      hidePdfLoadingSoon(seq);
    });

    el.btnNew.addEventListener("click", reset);
    el.btnJson.addEventListener("click", downloadJson);

    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input, textarea")) return;
      if (e.key === "Escape") {
        document.body.classList.remove("pdf-open");
        syncPdfBtn();
      }
      const n = Number(e.key);
      if (S.data && n >= 1 && n <= S.data.indicators.length) {
        const ind = S.data.indicators[n - 1];
        if (ind.key !== S.view) {
          S.view = ind.key;
          S.q = "";
          S.sel = null;
          renderRail();
          renderPane();
        }
      }
    });

    const onMq = () => {
      document.body.classList.remove("pdf-open");
      document.body.classList.toggle("pdf-hidden", false);
      syncPdfBtn();
    };
    if (mq.addEventListener) mq.addEventListener("change", onMq);
  }

  function reset() {
    if (S.url) URL.revokeObjectURL(S.url);
    S.file = null;
    S.url = null;
    S.data = null;
    S.view = null;
    S.q = "";
    S.sel = null;
    el.file.value = "";
    el.loading.hidden = true;
    el.error.hidden = true;
    el.idle.hidden = false;
    el.drop.hidden = false;
    el.ws.hidden = true;
    el.btnNew.hidden = true;
    el.btnJson.hidden = true;
    el.docMeta.innerHTML = "";
    el.pdfFrame.src = "about:blank";
    el.pdfLoading.hidden = true;
    pdfLoadToken += 1;             // 让还在路上的「隐藏遮罩」定时器失效
    document.body.classList.remove("pdf-open");
    document.body.classList.remove("pdf-hidden");
    document.title = "S1.1 报告段落浏览器";
  }

  function downloadJson() {
    if (!S.data) return;
    const name = (S.data.file.name || "report").replace(/\.pdf$/i, "");
    const blob = new Blob([JSON.stringify(S.data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name}_段落召回.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  function toast(msg) {
    el.toast.textContent = msg;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 2600);
  }

  bindUpload();
  bindWorkspace();
  syncPdfBtn();
})();