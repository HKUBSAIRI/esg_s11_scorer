(function () {
  'use strict';

  const DATA = window.BOARD;
  if (!DATA || !Array.isArray(DATA.companies) || !DATA.companies.length) return;

  const railEl = document.getElementById('rail');
  const mainEl = document.getElementById('main');
  const metaEl = document.getElementById('meta');
  const meta = DATA.meta;
  const state = { code: DATA.companies[0].code, open: null };

  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  const fmt = (v, digits) => {
    if (v == null || v === '' || Number.isNaN(Number(v))) return '—';
    const s = Number(v).toFixed(digits == null ? 1 : digits);
    return s.endsWith('.0') ? s.slice(0, -2) : s;
  };

  metaEl.innerHTML = [`FY${esc(meta.year)}`, `${esc(meta.count)} 家`, esc(meta.model)]
    .map((t) => `<span>${t}</span>`).join('');

  // ---------------------------------------------------------- 路由
  function readHash() {
    const raw = decodeURIComponent((location.hash || '').replace(/^#\/?/, ''));
    const parts = raw.split('/');
    const company = DATA.companies.find((c) => c.code === parts[0]);
    return company ? { code: company.code, open: parts[1] || null } : null;
  }

  function writeHash() {
    const h = '#' + state.code + (state.open ? '/' + state.open : '');
    if (location.hash !== h) history.replaceState(null, '', h);
  }

  // ---------------------------------------------------------- 公司列表
  function renderRail() {
    railEl.innerHTML = DATA.companies.map((c) => `
      <button class="rail-item${c.code === state.code ? ' is-active' : ''}"
              type="button" data-code="${esc(c.code)}"
              aria-current="${c.code === state.code ? 'true' : 'false'}">
        <span>
          <span class="rail-name">${esc(c.name)}</span>
          <span class="rail-code">${esc(c.code)}</span>
        </span>
        <span class="rail-score">${fmt(c.total)}</span>
      </button>`).join('');
  }

  // ---------------------------------------------------------- 分项与依据
  function partHTML(p) {
    const key = p.key ? `<span class="part-key">${esc(p.key)}</span>`
                      : '<span class="part-key"></span>';
    const weight = p.weight ? `<span class="part-weight">${p.weight}%</span>`
                            : '<span class="part-weight"></span>';
    return `<div class="part">${key}<div>
        <div class="part-line">
          <span class="part-name">${esc(p.name)}</span>${weight}
          <span class="part-score">${p.score == null ? 'N/A' : esc(p.score)}</span>
        </div>
        ${p.note ? `<p class="part-note">${esc(p.note)}</p>` : ''}
        ${p.missing ? `<p class="part-gap">缺口：${esc(p.missing)}</p>` : ''}
      </div></div>`;
  }

  function evidenceHTML(list) {
    if (!list || !list.length) return '';
    return `<div class="ev"><div class="ev-title">依据原文</div><ul>${
      list.map((e) => `<li>${e.page ? `<span class="page">p${e.page}</span>` : ''}${
        esc(e.text)}</li>`).join('')
    }</ul></div>`;
  }

  function detailHTML(ind) {
    const extra = ind.reachable < 100
      ? `<div class="detail-meta">
           <span>严格 <b>${fmt(ind.score)}</b></span>
           <span>归一 <b>${fmt(ind.norm)}</b></span>
           <span>可达上限 <b>${fmt(ind.reachable)}</b></span>
         </div>`
      : '';
    return `<div class="ind-detail">${extra}
        ${ind.parts.map(partHTML).join('')}
        ${evidenceHTML(ind.evidence)}
      </div>`;
  }

  function indHTML(ind, isOpen) {
    return `<div class="ind${isOpen ? ' is-open' : ''}" data-ind="${esc(ind.ind)}"
                 role="button" tabindex="0" aria-expanded="${isOpen ? 'true' : 'false'}">
        <span class="ind-code">${esc(ind.ind)}</span>
        <span class="ind-name">${esc(ind.name)}</span>
        <span class="num weight">${ind.weight}%</span>
        <span class="num score">${fmt(ind.score)}</span>
        <span class="num contrib">${fmt(ind.contribution)}</span>
        <span class="chev">${isOpen ? '−' : '+'}</span>
      </div>${isOpen ? detailHTML(ind) : ''}`;
  }

  // ---------------------------------------------------------- 取值
  function valuesHTML(values) {
    return `<dl class="values">${values.map((v) => `
      <div class="value">
        <dt class="label">${esc(v.label)}</dt>
        <dd class="val${v.missing ? ' is-missing' : ''}">${esc(v.value)}</dd>
        <dd class="sub">${v.sub ? esc(v.sub) : ''}${
          v.page ? `<span class="page">p${v.page}</span>` : ''}</dd>
      </div>`).join('')}</dl>`;
  }

  // ---------------------------------------------------------- 主区
  function renderMain() {
    const c = DATA.companies.find((x) => x.code === state.code) || DATA.companies[0];
    const norm = c.total_norm !== c.total
      ? `<span class="total-norm">归一 ${fmt(c.total_norm)}</span>` : '';

    mainEl.innerHTML = `
      <section class="company">
        <div>
          <h1>${esc(c.name)}</h1>
          <div class="company-sub">
            <span class="code">${esc(c.code)}.HK</span>
            <span>${esc(c.name_en)}</span>
          </div>
        </div>
        <div class="total">
          <span class="total-label">综合得分</span>
          <div class="total-line">
            <span class="total-num">${fmt(c.total)}</span>${norm}
          </div>
          <div class="total-bar">
            <span style="width:${Math.max(0, Math.min(100, c.total))}%"></span>
          </div>
        </div>
      </section>

      ${valuesHTML(c.values)}

      <section class="indicators" aria-label="指标">
        <div class="ind-head">
          <span></span><span>指标</span>
          <span class="num">权重</span><span class="num">得分</span>
          <span class="num col-contrib">贡献</span><span></span>
        </div>
        ${c.inds.map((ind) => indHTML(ind, ind.ind === state.open)).join('')}
      </section>

      <p class="foot">
        <span>FY${esc(meta.year)}</span>
        <span>定性 ${meta.mode === 'llm' ? 'LLM 判档' : '关键词兜底'}</span>
        <span>对标 n=${esc(meta.bench_n)}${meta.bench_n_improve != null && meta.bench_n_improve !== meta.bench_n
          ? `（改善率 ${esc(meta.bench_n_improve)}）` : ''}</span>
        <span>生成 ${esc(meta.generated)}</span>
      </p>`;
    writeHash();
  }

  function render() {
    renderRail();
    renderMain();
  }

  // ---------------------------------------------------------- 交互
  railEl.addEventListener('click', (e) => {
    const btn = e.target.closest('.rail-item');
    if (!btn) return;
    state.code = btn.dataset.code;
    state.open = null;
    render();
  });

  mainEl.addEventListener('click', (e) => {
    const row = e.target.closest('.ind');
    if (!row) return;
    state.open = state.open === row.dataset.ind ? null : row.dataset.ind;
    render();
  });

  mainEl.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const row = e.target.closest('.ind');
    if (!row) return;
    e.preventDefault();
    state.open = state.open === row.dataset.ind ? null : row.dataset.ind;
    render();
    const again = mainEl.querySelector(`.ind[data-ind="${state.open || row.dataset.ind}"]`);
    if (again) again.focus();
  });

  window.addEventListener('hashchange', () => {
    const next = readHash();
    if (!next) return;
    if (next.code === state.code && next.open === state.open) return;
    state.code = next.code;
    state.open = next.open;
    render();
  });

  const initial = readHash();
  if (initial) {
    state.code = initial.code;
    state.open = initial.open;
  }
  render();
})();