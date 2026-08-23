import { fixtureBundle } from './fixture-data.mjs';

export const HORIZONS = Object.freeze(['1d', '3d', '5d', '10d', '20d']);
export const PUBLIC_ROOT = 'https://raw.githubusercontent.com/qiqimimi1002/daily_stock_analysis';
export const RESEARCH_URL = `${PUBLIC_ROOT}/main/research/results/short_term_v1_vs_phase2a_2026-08-21.json`;

const MODEL_SPECS = Object.freeze([
  ['short_term_relative_strength_daily_v1', 'Short-term v1'],
  ['low_volatility_daily_60d_v1', 'Phase 2A'],
]);

const numberOrNull = (value) => value === null || value === undefined || value === ''
  ? null : Number.isFinite(Number(value)) ? Number(value) : null;
const textOr = (value, fallback) => typeof value === 'string' && value.trim() ? value.trim() : fallback;
const cloneFixture = () => JSON.parse(JSON.stringify(fixtureBundle));

export class DashboardReadError extends Error {
  constructor(reasonCode, message) {
    super(message);
    this.name = 'DashboardReadError';
    this.reasonCode = reasonCode;
  }
}

export function screeningUrls(tradeDate = null) {
  const prefix = tradeDate ? `history/${tradeDate}` : 'latest';
  return {
    manifest: `${PUBLIC_ROOT}/screening-results/${prefix}/manifest.json`,
    screening: `${PUBLIC_ROOT}/screening-results/${prefix}/market_screening.json`,
  };
}

function outcomeFor(raw, horizon) {
  const supplied = raw && typeof raw === 'object' ? raw[horizon] : null;
  if (!supplied || typeof supplied !== 'object') {
    return {
      horizon,
      state: horizon === '20d' ? 'pending' : 'unavailable',
      reasonCode: horizon === '20d' ? 'outcome_20d_not_mature' : 'public_candidate_outcome_unavailable',
      grossReturnPct: null,
      netReturn30bpsPct: null,
      mfePct: null,
      maePct: null,
      maxDrawdownPct: null,
      hs300ExcessPct: null,
    };
  }
  const state = ['completed', 'pending', 'failed', 'unavailable'].includes(supplied.status)
    ? supplied.status : 'unavailable';
  const completed = state === 'completed';
  return {
    horizon,
    state,
    reasonCode: textOr(supplied.reason_code, completed ? null : 'outcome_state_unavailable'),
    grossReturnPct: completed ? numberOrNull(supplied.gross_return_pct) : null,
    netReturn30bpsPct: completed ? numberOrNull(supplied.net_return_30bps_pct) : null,
    mfePct: completed ? numberOrNull(supplied.mfe_pct) : null,
    maePct: completed ? numberOrNull(supplied.mae_pct) : null,
    maxDrawdownPct: completed ? numberOrNull(supplied.max_drawdown_pct) : null,
    hs300ExcessPct: completed ? numberOrNull(supplied.hs300_excess_pct) : null,
  };
}

export function adaptScreening(manifestInput, screeningInput, sourceUrl, fixture = false) {
  const manifest = manifestInput && typeof manifestInput === 'object' ? manifestInput : {};
  const screening = screeningInput && typeof screeningInput === 'object' ? screeningInput : {};
  const candidates = Array.isArray(screening.candidates) ? screening.candidates : [];
  return {
    fixture,
    tradeDate: textOr(manifest.trade_date, 'unavailable'),
    runNumber: textOr(String(manifest.run_number ?? ''), 'unavailable'),
    status: textOr(manifest.screening_outcome ?? manifest.status, 'unavailable'),
    reasonCodes: Array.isArray(manifest.reason_codes)
      ? manifest.reason_codes.filter((item) => typeof item === 'string').slice(0, 10) : [],
    generatedAt: textOr(manifest.screening_generated_at, 'unavailable'),
    marketDataAt: textOr(manifest.market_data_at, 'unavailable'),
    modelVersion: textOr(manifest.model_version, 'V2.1'),
    triggerSource: textOr(manifest.trigger_source, 'unavailable'),
    integrity: manifest.integrity?.ok === true ? 'verified' : manifest.integrity?.ok === false ? 'failed' : 'unavailable',
    sourceUrl,
    candidates: candidates.map((candidate) => ({
      code: textOr(candidate?.code, 'unavailable'),
      name: textOr(candidate?.name, '名称 unavailable'),
      score: numberOrNull(candidate?.score),
      coveragePct: numberOrNull(candidate?.score_coverage_pct),
      confidence: textOr(candidate?.confidence_label, 'unavailable'),
      riskGate: textOr(candidate?.risk_gate, 'unavailable'),
      shortTerm: { state: 'unavailable', reasonCode: 'public_short_term_candidate_rank_unavailable' },
      outcomes: Object.fromEntries(HORIZONS.map((horizon) => [horizon, outcomeFor(candidate?.public_outcomes, horizon)])),
    })),
  };
}

function modelHorizon(raw, horizon, pendingReason) {
  if (horizon === '20d') {
    return { horizon, state: 'pending', reasonCode: textOr(pendingReason, 'outcome_20d_not_mature'), sampleCount: 0,
      grossReturnMeanPct: null, netReturnMeanPct: null, netWinRatePct: null, mfeMeanPct: null,
      maeMeanPct: null, maxDrawdownPct: null, hs300ExcessMeanPct: null };
  }
  const sampleCount = Math.max(0, Number.parseInt(raw?.sample_count ?? 0, 10) || 0);
  const completed = sampleCount > 0;
  return {
    horizon,
    state: completed ? 'completed' : 'unavailable',
    reasonCode: completed ? null : 'insufficient_evidence_zero_samples',
    sampleCount,
    grossReturnMeanPct: completed ? numberOrNull(raw?.gross_return_mean_pct) : null,
    netReturnMeanPct: completed ? numberOrNull(raw?.net_return_mean_pct) : null,
    netWinRatePct: completed ? numberOrNull(raw?.net_win_rate_pct) : null,
    mfeMeanPct: completed ? numberOrNull(raw?.mfe_mean_pct) : null,
    maeMeanPct: completed ? numberOrNull(raw?.mae_mean_pct) : null,
    maxDrawdownPct: completed ? numberOrNull(raw?.max_drawdown_pct) : null,
    hs300ExcessMeanPct: completed ? numberOrNull(raw?.hs300_excess_mean_pct) : null,
  };
}

export function adaptResearch(input, sourceUrl, fixture = false) {
  const raw = input && typeof input === 'object' ? input : {};
  const pending20d = raw.pending_horizons?.['20d'];
  return {
    fixture,
    evidenceStatus: textOr(raw.evidence_status, 'insufficient_evidence').toUpperCase().replaceAll('_', ' '),
    conclusion: textOr(raw.conclusion_class, '表现接近/证据不足'),
    costBps: numberOrNull(raw.cost_bps) ?? 30,
    sourceUrl,
    models: MODEL_SPECS.map(([modelId, displayName]) => ({
      modelId,
      displayName,
      researchStatus: textOr(raw.evidence_status, 'insufficient_evidence').toUpperCase().replaceAll('_', ' '),
      horizons: HORIZONS.map((horizon) => modelHorizon(raw.models?.[modelId]?.[horizon], horizon, pending20d)),
    })),
  };
}

async function readJson(fetchImpl, url) {
  let response;
  try {
    response = await fetchImpl(url, { method: 'GET', cache: 'no-store', credentials: 'omit' });
  } catch (error) {
    throw new DashboardReadError('public_fetch_failed', `无法读取 Public 结果：${error instanceof Error ? error.message : 'network error'}`);
  }
  if (!response?.ok) {
    throw new DashboardReadError('public_http_error', `Public 结果返回 HTTP ${response?.status ?? 'unknown'}`);
  }
  try {
    return await response.json();
  } catch {
    throw new DashboardReadError('public_json_invalid', 'Public 结果不是有效 JSON');
  }
}

export async function readDashboardData({ tradeDate = null, fixture = false, fetchImpl = globalThis.fetch } = {}) {
  if (fixture) {
    const bundle = cloneFixture();
    return {
      screening: adaptScreening(bundle.manifest, bundle.screening, 'fixture://dashboard-v0.1/screening', true),
      research: adaptResearch(bundle.research, 'fixture://dashboard-v0.1/research', true),
    };
  }
  if (typeof fetchImpl !== 'function') {
    throw new DashboardReadError('fetch_unavailable', '当前预览环境不支持读取 Public JSON');
  }
  const urls = screeningUrls(tradeDate);
  const [manifest, screening, research] = await Promise.all([
    readJson(fetchImpl, urls.manifest),
    readJson(fetchImpl, urls.screening),
    readJson(fetchImpl, RESEARCH_URL),
  ]);
  return {
    screening: adaptScreening(manifest, screening, urls.manifest, false),
    research: adaptResearch(research, RESEARCH_URL, false),
  };
}
