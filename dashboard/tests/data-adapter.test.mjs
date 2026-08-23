import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import {
  DashboardReadError,
  HORIZONS,
  adaptResearch,
  adaptScreening,
  readDashboardData,
  screeningUrls,
} from '../data-adapter.mjs';

const manifest = (overrides = {}) => ({
  trade_date: '2026-08-21', run_number: 59, status: 'success', screening_outcome: 'success',
  screening_generated_at: '2026-08-21T10:04:16+08:00', market_data_at: '2026-08-21T10:02:35+08:00',
  model_version: 'V2.1', trigger_source: 'external_scheduler_cloudflare', integrity: { ok: true },
  reason_codes: [], ...overrides,
});
const research = (sampleCount = 0) => ({
  evidence_status: sampleCount ? 'sufficient_evidence' : 'insufficient_evidence',
  conclusion_class: sampleCount ? 'fixture only' : '表现接近/证据不足', cost_bps: 30,
  models: {
    short_term_relative_strength_daily_v1: {
      '1d': { sample_count: sampleCount, gross_return_mean_pct: 1.2, net_return_mean_pct: .9, net_win_rate_pct: 55,
        mfe_mean_pct: 2, mae_mean_pct: .5, max_drawdown_pct: .4, hs300_excess_mean_pct: .2 },
    },
    low_volatility_daily_60d_v1: { '1d': { sample_count: sampleCount } },
  },
  pending_horizons: { '20d': 'merged_20d_chain_unavailable' },
});
const response = (body, ok = true, status = 200) => ({ ok, status, json: async () => body });

test('adapter exposes only the candidate allowlist and drops all Private/raw fields', () => {
  const privateSentinel = 'PRIVATE-RAW-MUST-NOT-LEAK';
  const output = adaptScreening(manifest(), { candidates: [{
    code: '600000', name: '浦发银行', score: 40, score_coverage_pct: 44, confidence_label: '低', risk_gate: '复核',
    amount: privateSentinel, amount_yi: privateSentinel, open: privateSentinel, high: privateSentinel,
    low: privateSentinel, close: privateSentinel, volume: privateSentinel, raw_rows: [privateSentinel],
    universe: [privateSentinel], corporate_actions: [privateSentinel], reasons: [privateSentinel],
  } ] }, 'public://manifest');
  const serialized = JSON.stringify(output);
  assert.equal(serialized.includes(privateSentinel), false);
  for (const key of ['amount', 'amount_yi', 'open', 'high', 'low', 'close', 'volume', 'raw_rows', 'universe', 'corporate_actions', 'reasons']) {
    assert.equal(Object.hasOwn(output.candidates[0], key), false, key);
  }
});

test('missing Public candidate outcomes stay unavailable and 20d stays pending', () => {
  const output = adaptScreening(manifest(), { candidates: [{ code: '600000', name: '浦发银行' }] }, 'public://manifest');
  assert.deepEqual(Object.keys(output.candidates[0].outcomes), HORIZONS);
  assert.equal(output.candidates[0].outcomes['1d'].state, 'unavailable');
  assert.equal(output.candidates[0].outcomes['1d'].reasonCode, 'public_candidate_outcome_unavailable');
  assert.equal(output.candidates[0].outcomes['20d'].state, 'pending');
  assert.equal(output.candidates[0].outcomes['20d'].reasonCode, 'outcome_20d_not_mature');
});

test('completed fixture outcome maps only the approved aggregate metrics', () => {
  const output = adaptScreening(manifest(), { candidates: [{ code: 'DEMO', public_outcomes: {
    '1d': { status: 'completed', gross_return_pct: 1, net_return_30bps_pct: .7, mfe_pct: 2, mae_pct: .5,
      max_drawdown_pct: .4, hs300_excess_pct: .2, private_rows: ['secret'] },
  } }] }, 'fixture://screening', true);
  assert.equal(output.candidates[0].outcomes['1d'].netReturn30bpsPct, .7);
  assert.equal(JSON.stringify(output).includes('private_rows'), false);
});

test('empty candidate list remains empty', () => {
  assert.deepEqual(adaptScreening(manifest(), { candidates: [] }, 'public://manifest').candidates, []);
});

test('failure status and reason codes are retained without inventing candidates', () => {
  const output = adaptScreening(manifest({ status: 'failure', screening_outcome: 'failure', integrity: { ok: false }, reason_codes: ['manifest_hash_mismatch'] }), {}, 'public://manifest');
  assert.equal(output.status, 'failure');
  assert.equal(output.integrity, 'failed');
  assert.deepEqual(output.reasonCodes, ['manifest_hash_mismatch']);
  assert.equal(output.candidates.length, 0);
});

test('zero model samples suppress every percentage including an input win rate', () => {
  const output = adaptResearch(research(0), 'public://research');
  const row = output.models[0].horizons.find((item) => item.horizon === '1d');
  assert.equal(row.sampleCount, 0);
  assert.equal(row.state, 'unavailable');
  assert.equal(row.netWinRatePct, null);
  assert.equal(row.grossReturnMeanPct, null);
  assert.equal(output.evidenceStatus, 'INSUFFICIENT EVIDENCE');
});

test('non-zero model fixture maps approved aggregate fields', () => {
  const row = adaptResearch(research(3), 'fixture://research', true).models[0].horizons[0];
  assert.equal(row.state, 'completed');
  assert.equal(row.sampleCount, 3);
  assert.equal(row.netWinRatePct, 55);
});

test('model 20d is pending even when the source has no 20d metrics', () => {
  const row = adaptResearch(research(0), 'public://research').models[0].horizons.at(-1);
  assert.equal(row.horizon, '20d');
  assert.equal(row.state, 'pending');
  assert.equal(row.reasonCode, 'merged_20d_chain_unavailable');
});

test('historical date builds fixed Public history URLs', () => {
  assert.deepEqual(screeningUrls('2026-08-20'), {
    manifest: 'https://raw.githubusercontent.com/qiqimimi1002/daily_stock_analysis/screening-results/history/2026-08-20/manifest.json',
    screening: 'https://raw.githubusercontent.com/qiqimimi1002/daily_stock_analysis/screening-results/history/2026-08-20/market_screening.json',
  });
});

test('reader date switch performs only three static GET requests', async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('manifest.json')) return response(manifest());
    if (url.endsWith('market_screening.json')) return response({ candidates: [] });
    return response(research(0));
  };
  await readDashboardData({ tradeDate: '2026-08-20', fetchImpl });
  assert.equal(calls.length, 3);
  assert.equal(calls.every(({ options }) => options.method === 'GET' && options.credentials === 'omit'), true);
  assert.equal(calls.filter(({ url }) => url.includes('/history/2026-08-20/')).length, 2);
  assert.equal(calls.every(({ url }) => url.startsWith('https://raw.githubusercontent.com/')), true);
});

test('reader never silently falls back to fixture after a Public failure', async () => {
  await assert.rejects(
    readDashboardData({ fetchImpl: async () => response({}, false, 404) }),
    (error) => error instanceof DashboardReadError && error.reasonCode === 'public_http_error',
  );
});

test('fixture mode is explicit and makes no network call', async () => {
  let calls = 0;
  const output = await readDashboardData({ fixture: true, fetchImpl: async () => { calls += 1; } });
  assert.equal(calls, 0);
  assert.equal(output.screening.fixture, true);
  assert.equal(output.screening.tradeDate.startsWith('2099-'), true);
  assert.equal(output.screening.candidates.every(({ code }) => code.startsWith('DEMO-')), true);
});

test('static implementation has no backend, provider, workflow, model or screening trigger calls', async () => {
  const source = await Promise.all([
    readFile(new URL('../app.mjs', import.meta.url), 'utf8'),
    readFile(new URL('../data-adapter.mjs', import.meta.url), 'utf8'),
  ]).then((parts) => parts.join('\n').toLowerCase());
  for (const forbidden of ['/api/', 'tushare', 'akshare', 'baostock', 'workflow_dispatch', 'screen_market', 'run_screen', 'calculate_model', 'generate_universe', '../src/']) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
});
