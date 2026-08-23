import { HORIZONS, DashboardReadError, readDashboardData } from './data-adapter.mjs';

const fixtureMode = new URLSearchParams(location.search).get('fixture') === 'dashboard-v0.1';
const elements = Object.fromEntries([
  'fixture-banner', 'global-error', 'date-notice', 'summary-cards', 'candidate-list',
  'screening-source', 'history-form', 'history-date', 'history-list', 'research-notice',
  'model-list', 'research-source', 'refresh-button',
].map((id) => [id, document.getElementById(id)]));

const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
})[char]);
const pct = (value) => Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : 'N/A';
const dateTime = (value) => {
  if (value === 'unavailable') return value;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? escapeHtml(value) : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(parsed);
};
const statusPill = (state) => {
  const css = state === 'completed' || state === 'success' || state === 'verified'
    ? 'pill-ok' : state === 'failed' ? 'pill-error' : 'pill-warn';
  return `<span class="pill ${css}">${escapeHtml(state)}</span>`;
};
const sourceMarkup = (url) => url.startsWith('fixture://')
  ? `来源：${escapeHtml(url)}`
  : `来源：<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>`;

function renderScreening(run) {
  elements['history-date'].value = run.tradeDate === 'unavailable' ? '' : run.tradeDate;
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date());
  elements['date-notice'].classList.toggle('hidden', run.fixture || run.tradeDate === today || run.tradeDate === 'unavailable');
  elements['date-notice'].innerHTML = `<strong>非交易日说明</strong><span>今天是 ${escapeHtml(today)}；最新 Public 交易日是 ${escapeHtml(run.tradeDate)}，页面不会把历史结果冒充为实时结果。</span>`;
  const reasonNote = run.reasonCodes.length ? `reason: ${run.reasonCodes.map(escapeHtml).join(', ')}` : run.triggerSource;
  elements['summary-cards'].innerHTML = [
    ['公开交易日', run.tradeDate, '最新已发布交易日'],
    ['V2.1 候选', String(run.candidates.length), `Run #${run.runNumber} · ${run.status}`],
    ['筛选时间', dateTime(run.generatedAt), `行情截止 ${dateTime(run.marketDataAt)}`],
    ['完整性', run.integrity, reasonNote],
  ].map(([label, value, note]) => `<article class="card"><p class="metric-label">${escapeHtml(label)}</p><p class="metric-value">${escapeHtml(value)}</p><p class="metric-note">${escapeHtml(note)}</p></article>`).join('');

  elements['candidate-list'].innerHTML = run.candidates.length ? run.candidates.map((candidate) => `
    <article class="card">
      <div class="candidate-head"><div><p class="code">${escapeHtml(candidate.code)}</p><h3>${escapeHtml(candidate.name)}</h3></div><span class="pill pill-ok">V2.1 正式候选</span></div>
      <ul class="basis">
        <li>V2.1 评分 ${candidate.score ?? 'N/A'}</li>
        <li>证据覆盖率 ${candidate.coveragePct ?? 'N/A'}${candidate.coveragePct === null ? '' : '%'}</li>
        <li>置信状态 ${escapeHtml(candidate.confidence)}</li>
        <li>风险门禁 ${escapeHtml(candidate.riskGate)}</li>
      </ul>
      <div class="short-term"><strong>Short-term v1 研究侧</strong><br>${escapeHtml(candidate.shortTerm.state)} · ${escapeHtml(candidate.shortTerm.reasonCode)}</div>
    </article>`).join('') : '<div class="empty">该 Public 结果没有候选；页面不会补算或伪造股票。</div>';
  elements['screening-source'].innerHTML = sourceMarkup(run.sourceUrl);
  renderHistory(run);
}

function outcomeMarkup(outcome) {
  const show = outcome.state === 'completed';
  return `<div class="outcome">
    <strong>${escapeHtml(outcome.horizon)}</strong> ${statusPill(outcome.state)}
    ${show ? `<dl>
      <dt>收益</dt><dd>${pct(outcome.grossReturnPct)}</dd>
      <dt>净收益 30bps</dt><dd>${pct(outcome.netReturn30bpsPct)}</dd>
      <dt>MFE</dt><dd>${pct(outcome.mfePct)}</dd>
      <dt>MAE</dt><dd>${pct(outcome.maePct)}</dd>
      <dt>最大回撤</dt><dd>${pct(outcome.maxDrawdownPct)}</dd>
      <dt>沪深300超额</dt><dd>${pct(outcome.hs300ExcessPct)}</dd>
    </dl>` : `<p class="metric-note">${escapeHtml(outcome.reasonCode)}</p>`}
  </div>`;
}

function renderHistory(run) {
  elements['history-list'].innerHTML = run.candidates.length ? run.candidates.map((candidate) => `
    <article class="card">
      <div class="candidate-head"><div><p class="code">${escapeHtml(candidate.code)}</p><h3>${escapeHtml(candidate.name)}</h3></div><span class="pill">Outcome Engine · 只读</span></div>
      <div class="outcome-strip">${HORIZONS.map((horizon) => outcomeMarkup(candidate.outcomes[horizon])).join('')}</div>
    </article>`).join('') : '<div class="empty">该交易日没有可展示的 Public 候选。</div>';
}

function renderResearch(research) {
  elements['research-notice'].innerHTML = `<strong>${escapeHtml(research.evidenceStatus)}</strong><span>研究结论：${escapeHtml(research.conclusion)}。成本口径：${research.costBps}bps；样本为 0 时所有百分比保持 N/A。</span>`;
  elements['model-list'].innerHTML = research.models.map((model) => {
    const sampleCount = Math.max(...model.horizons.map((row) => row.sampleCount));
    return `<article class="card">
      <div class="model-head"><div><p class="code">${escapeHtml(model.modelId)}</p><h3>${escapeHtml(model.displayName)}</h3></div><div>${statusPill(model.researchStatus)} <span class="pill">样本 ${sampleCount}</span></div></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Horizon</th><th>状态</th><th>样本</th><th>收益</th><th>30bps 净收益</th><th>净胜率</th><th>MFE</th><th>MAE</th><th>最大回撤</th><th>沪深300超额</th></tr></thead>
        <tbody>${model.horizons.map((row) => `<tr><td>${row.horizon}</td><td>${escapeHtml(row.state)}</td><td>${row.sampleCount}</td><td>${pct(row.grossReturnMeanPct)}</td><td>${pct(row.netReturnMeanPct)}</td><td>${pct(row.netWinRatePct)}</td><td>${pct(row.mfeMeanPct)}</td><td>${pct(row.maeMeanPct)}</td><td>${pct(row.maxDrawdownPct)}</td><td>${pct(row.hs300ExcessMeanPct)}</td></tr>`).join('')}</tbody>
      </table></div>
    </article>`;
  }).join('');
  elements['research-source'].innerHTML = sourceMarkup(research.sourceUrl);
}

async function load(tradeDate = null) {
  elements['refresh-button'].disabled = true;
  elements['global-error'].classList.add('hidden');
  try {
    const data = await readDashboardData({ tradeDate, fixture: fixtureMode });
    renderScreening(data.screening);
    renderResearch(data.research);
  } catch (error) {
    const reason = error instanceof DashboardReadError ? error.reasonCode : 'dashboard_read_failed';
    elements['global-error'].innerHTML = `<strong>读取失败 · ${escapeHtml(reason)}</strong><span>${escapeHtml(error instanceof Error ? error.message : 'unknown error')}</span>`;
    elements['global-error'].classList.remove('hidden');
    elements['candidate-list'].innerHTML = '<div class="empty">没有可展示的数据；失败时不会自动切换到 fixture。</div>';
    elements['history-list'].innerHTML = '<div class="empty">unavailable</div>';
    elements['model-list'].innerHTML = '<div class="empty">unavailable</div>';
  } finally {
    elements['refresh-button'].disabled = false;
  }
}

elements['fixture-banner'].classList.toggle('hidden', !fixtureMode);
elements['refresh-button'].addEventListener('click', () => load(null));
elements['history-form'].addEventListener('submit', (event) => {
  event.preventDefault();
  load(elements['history-date'].value || null);
});
load();
