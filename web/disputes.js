// 判档分歧页：单家判档 + 全批矩阵。无依赖。
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");
const tierText = (t) => (t === null || t === undefined ? "失败" : String(t));
const tierClass = (t) => t === 100 ? "t100" : t === 50 ? "t50" : t === 0 ? "t0" : "tna";
const short = (s, n) => (s && s.length > n ? s.slice(0, n) + "…" : s || "");

let SUMMARY = null;
let FILTER = "all";
let QUERY = "";

init();

async function init() {
  wireUpload();
  wireToolbar();
  try {
    const h = await (await fetch("/api/health")).json();
    const voters = (h.voters || "").split(",").filter(Boolean);
    $("#meta").textContent = `${h.llm || "无模型"} · ${voters.length || 3} 位投票人`;
    $("#meta").title = voters.length
      ? voters.join("、")
      : "deepseek-v4.1-flash-1017@0.0、qwen-max@0.7、qwen-plus@0.7";
    $("#cost").textContent = `${5 * (voters.length || 3)} 次模型调用`;
  } catch (e) { /* 健康检查失败不影响上传 */ }
  await loadSummary();
}

// ---------------------------------------------------------------- 上传
function wireUpload() {
  const drop = $("#drop"), input = $("#file");
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("hot"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("hot"); }));
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) upload(f);
  });
  input.addEventListener("change", () => {
    if (input.files && input.files[0]) upload(input.files[0]);
  });
}

async function upload(file) {
  $("#error").hidden = true;
  if (!/\.pdf$/i.test(file.name)) return showError("只支持 PDF 文件。");
  if (file.size > 120 * 1024 * 1024) return showError("文件超过 120 MB。");
  setBusy(true, 0, `上传中… ${(file.size / 1048576).toFixed(1)} MB`);
  try {
    const res = await fetch(`/api/analyze?name=${encodeURIComponent(file.name)}`, {
      method: "POST", headers: { "Content-Type": "application/pdf" }, body: file,
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    await poll(data.job);
  } catch (e) {
    setBusy(false);
    showError(`上传失败：${e.message}`);
  }
}

async function poll(jobId) {
  for (let i = 0; i < 900; i++) {
    const st = await (await fetch(`/api/job?id=${jobId}`)).json();
    if (st.error) throw new Error(st.error);
    setBusy(true, st.pct || 0, st.step || "处理中…");
    if (st.status === "done") {
      setBusy(false);
      renderCompany(st.result);
      return;
    }
    if (st.status === "error") throw new Error(st.error || "判档失败");
    await new Promise((r) => setTimeout(r, 700));
  }
  throw new Error("等待超时（10 分钟）");
}

function setBusy(on, pct = 0, step = "") {
  $("#progress").hidden = !on;
  $("#bar").style.width = `${pct}%`;
  $("#step").textContent = step;
  $("#drop").style.opacity = on ? .5 : 1;
  $("#drop").style.pointerEvents = on ? "none" : "auto";
}

function showError(msg) {
  const box = $("#error");
  box.textContent = msg;
  box.hidden = false;
}

// ---------------------------------------------------------------- 单家结果
function renderCompany(data) {
  const s = data.summary;
  const judge = s.judgeable || 0;
  $("#company-name").textContent =
    `${data.company.name}${data.company.code ? "（" + data.company.code + "）" : ""}`;
  $("#company-meta").textContent =
    `${data.file.name} · ${data.file.pages} 页 · FY${data.file.report_year} · ${data.file.seconds}s`;
  $("#company-meta").title = data.file.name;

  const pills = [];
  if (s.split) pills.push(`<span class="pill sp">${s.split} / ${judge} 项分歧</span>`);
  else pills.push(`<span class="pill ok">${judge} 项一致</span>`);
  if (s.gate) pills.push(`<span class="pill gt">${s.gate} 项闸门</span>`);
  $("#company-verdict").innerHTML = pills.join("");

  $("#cards").innerHTML = data.indicators.map((it) => indCard(it, data.voters)).join("");
  $("#company").hidden = false;
  $("#company").scrollIntoView({ behavior: "smooth", block: "start" });
}

function indCard(it, voters) {
  const badge = it.split_type === "gate"
    ? `<span class="badge gt">闸门 · 判 0</span>`
    : it.split
      ? `<span class="badge sp">${esc(it.agreement)} 有分歧</span>`
      : `<span class="badge ok">3/3 一致</span>`;
  const votes = it.votes.map((v, i) => {
    const who = esc(voterLabel(voters, i));
    if (v.tier === null || v.source === "error") {
      return `<div class="vote err"><div class="who">${who}</div>
        <div>没有结论：${esc(v.error || "未知错误")}</div></div>`;
    }
    const ev = (v.evidence || []).slice(0, 3).map((e) => {
      const m = /^\[p(\d+)\]\s*([\s\S]*)$/.exec(e || "");
      const pg = m ? m[1] : "?";
      const tx = m ? m[2] : e;
      return `<li><span class="pg">p${pg}</span>${esc(short(tx, 140))}</li>`;
    }).join("");
    return `<div class="vote">
      <div class="who">${who}</div>
      <div class="tier ${tierClass(v.tier)}">${tierText(v.tier)}<small>分</small></div>
      <p class="why">${esc(v.reason || "（未给理由）")}</p>
      ${v.missing ? `<p class="miss">还缺：${esc(v.missing)}</p>` : ""}
      ${ev ? `<ul class="ev">${ev}</ul>` : ""}
    </div>`;
  }).join("");

  const foot = it.split_type === "gate"
    ? "闸门 · 未披露离职率"
    : `多数派 <b>${tierText(it.majority)}</b> · 召回 ${it.n_passages} 段`;
  return `<article class="ind${it.split ? " split" : ""}">
    <div class="ind-head">
      <span class="ind-key">${esc(it.key)}</span>
      <span class="ind-name">${esc(it.name)}</span>
      ${badge}
    </div>
    <div class="votes">${votes}</div>
    <p class="ind-foot">${foot}</p>
  </article>`;
}

function voterLabel(voters, i) {
  const v = voters && voters[i];
  if (!v) return `第 ${i + 1} 票`;
  if (typeof v === "string") return v;
  if (v.name) return v.name;
  const t = v.temperature;
  return `${v.model || "模型"}${t === 0 || t ? "@" + t : ""}`;
}

// ---------------------------------------------------------------- 总汇
async function loadSummary() {
  let s;
  try { s = await (await fetch("/api/summary")).json(); }
  catch (e) { $("#summary-meta").textContent = `读取失败：${e.message}`; return; }
  if (!s.ok) {
    $("#summary-meta").textContent = s.hint || "没有体检数据";
    return;
  }
  SUMMARY = s;
  const sum = s.summary || {};
  const names = s.tables.names || {};
  const voters = s.voters || [];
  $("#year-tag").textContent = `FY${s.year}`;
  $("#year-tag").hidden = false;
  $("#summary-meta").textContent =
    `FY${s.year} · ${Object.keys(names).length} 家 · ${voters.length} 位投票人 · ${s.version.rubric}`;
  $("#summary-meta").title =
    `出分模型 ${s.version.model}` + (voters.length ? `；投票人 ${voters.map((v) => v.name).join("、")}` : "");

  const agree = (sum.judgeable || 0) - (sum.split || 0);
  const rate = ((sum.rate || 0) * 100).toFixed(1);
  $("#stats").innerHTML = [
    ["可判项", sum.judgeable, ""],
    ["一致", agree, "ok"],
    [`分歧 ${rate}%`, sum.split, "sp"],
    ["闸门", sum.gate, "gt"],
    ["κ 一致性", sum.kappa == null ? "—" : sum.kappa.toFixed(2), ""],
  ].map(([label, value, cls]) =>
    `<div class="stat ${cls}"><b>${value}</b><span>${label}</span></div>`).join("");

  renderMatrix();
}

// ---------------------------------------------------------------- 矩阵
const IND_ORDER = ["S1.1.1-C", "S1.1.2-C", "S1.1.3", "S1.1.4", "S1.1.5"];

function cellInner(r) {
  if (r.split_type === "gate") return `<span class="gate">—</span>`;
  const ts = [r.t1, r.t2, r.t3];
  if (new Set(ts).size === 1) {
    return `<b class="t ${tierClass(ts[0])}">${tierText(ts[0])}</b>`;
  }
  return `<span class="trio">${ts.map((t) =>
    `<b class="t ${tierClass(t)}">${tierText(t)}</b>`).join("")}</span>`;
}

function renderMatrix() {
  if (!SUMMARY) return;
  const rows = SUMMARY.tables.rows || [];
  const names = SUMMARY.tables.names || {};
  const inds = SUMMARY.tables.indicators || {};
  const keys = IND_ORDER.filter((k) => inds[k]);
  const by = new Map(rows.map((r) => [`${r.code}|${r.key}`, r]));

  const perCode = {};
  rows.forEach((r) => {
    const c = perCode[r.code] || (perCode[r.code] = { split: 0, judge: 0 });
    if (r.split_type === "gate") return;
    c.judge++;
    if (r.agreement !== "3/3") c.split++;
  });
  const allCodes = Object.keys(perCode).sort((a, b) =>
    (perCode[b].split - perCode[a].split) || a.localeCompare(b));

  const q = QUERY.trim().toLowerCase();
  const codes = allCodes.filter((code) => {
    if (FILTER === "split" && !perCode[code].split) return false;
    if (!q) return true;
    return (names[code] || "").toLowerCase().includes(q) || code.includes(q);
  });
  $("#empty").hidden = codes.length > 0;
  if (!codes.length) { $("#matrix").innerHTML = ""; return; }

  const colHead = keys.map((k) => {
    const mine = rows.filter((r) => r.key === k && r.split_type !== "gate");
    const split = mine.filter((r) => r.agreement !== "3/3").length;
    return `<th title="${esc(inds[k].name || "")}">
      <b>${esc(k)}<em class="${split ? "hot" : ""}">${split}/${mine.length}</em></b>
      <span>${esc(short(inds[k].name || "", 10))}</span></th>`;
  }).join("");

  const body = codes.map((code) => {
    const cells = keys.map((k) => {
      const r = by.get(`${code}|${k}`);
      if (!r) return `<td class="cell gt"><div class="inner"><span class="gate">—</span></div></td>`;
      const split = r.split_type !== "gate" && r.agreement !== "3/3";
      const cls = r.split_type === "gate" ? "gt" : (split ? "sp" : "ok");
      const tip = `${names[code] || code} · ${k}：${[r.t1, r.t2, r.t3].map(tierText).join(" / ")}`;
      return `<td class="cell ${cls}" data-code="${esc(code)}" data-key="${esc(k)}"
        title="${esc(tip)}"><div class="inner">${cellInner(r)}</div></td>`;
    }).join("");
    const c = perCode[code];
    return `<tr><th class="rowhead" title="${esc(names[code] || code)}">
      <span>${esc(names[code] || code)}</span><small>${esc(code)}</small></th>${cells}
      <td class="count${c.split ? " has" : ""}"><b>${c.split}</b>/${c.judge}</td></tr>`;
  }).join("");

  $("#matrix").innerHTML = `<table class="mx">
    <thead><tr><th class="rowhead">公司</th>${colHead}<th>分歧</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}

// ---------------------------------------------------------------- 筛选
function wireToolbar() {
  $("#filter").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-filter]");
    if (!btn) return;
    FILTER = btn.dataset.filter;
    $("#filter").querySelectorAll("button").forEach((b) =>
      b.classList.toggle("on", b === btn));
    renderMatrix();
  });
  $("#search").addEventListener("input", (e) => {
    QUERY = e.target.value || "";
    renderMatrix();
  });
  $("#matrix").addEventListener("click", (e) => {
    const td = e.target.closest("td.cell[data-key]");
    if (td) showDetail(td.dataset.code, td.dataset.key);
  });
  $("#detail").addEventListener("click", (e) => {
    if (e.target.id === "dbackdrop" || e.target.id === "dclose") closeDetail();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#detail").hidden) closeDetail();
  });
}

// ---------------------------------------------------------------- 详情抽屉
function showDetail(code, key) {
  const rows = SUMMARY.tables.rows || [];
  const r = rows.find((x) => x.code === code && x.key === key);
  if (!r) return;
  const voters = SUMMARY.voters || [];
  const ind = (SUMMARY.tables.indicators || {})[key] || {};
  const name = (SUMMARY.tables.names || {})[code] || code;
  const tiers = [r.t1, r.t2, r.t3];

  const votes = tiers.map((t, i) => {
    const who = esc(voterLabel(voters, i));
    const why = r[`r${i + 1}`] || "";
    if (t === null || t === undefined) {
      return `<div class="vote err"><div class="who">${who}</div>
        <div>没有结论：${esc(why || "未知错误")}</div></div>`;
    }
    const ev = r[`e${i + 1}`] || "";
    const m = /^\[p(\d+)\]\s*([\s\S]*)$/.exec(ev);
    const evHtml = ev
      ? `<ul class="ev"><li><span class="pg">p${m ? m[1] : "?"}</span>
         ${esc(short(m ? m[2] : ev, 200))}</li></ul>` : "";
    return `<div class="vote"><div class="who">${who}</div>
      <div class="tier ${tierClass(t)}">${tierText(t)}<small>分</small></div>
      <p class="why">${esc(short(why || "（未给理由）", 260))}</p>${evHtml}</div>`;
  }).join("");

  const badge = r.split_type === "gate"
    ? `<span class="badge gt">闸门 · 判 0</span>`
    : r.agreement === "3/3"
      ? `<span class="badge ok">3/3 一致</span>`
      : `<span class="badge sp">${esc(r.agreement)} 有分歧</span>`;

  $("#detail-body").innerHTML = `
    <div class="d-head">
      <div>
        <h3>${esc(name)}<span class="code">${esc(code)}</span></h3>
        <p class="d-sub">${esc(key)} · ${esc(ind.name || "")}</p>
      </div>
      <button class="d-close" id="dclose" type="button" aria-label="关闭">✕</button>
    </div>
    <div class="d-badges">${badge}</div>
    <div class="votes">${votes}</div>
    <p class="d-foot">多数派 <b>${r.majority == null ? "—" : r.majority}</b>
      　·　总汇仅存每票一句理由与首条引用</p>`;
  $("#detail").hidden = false;
  document.body.classList.add("lock");
  $("#dclose").focus();
}

function closeDetail() {
  $("#detail").hidden = true;
  document.body.classList.remove("lock");
}
