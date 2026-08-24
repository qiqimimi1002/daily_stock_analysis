export const fixtureBundle = Object.freeze({
  manifest: {
    trade_date: '2099-01-08',
    run_number: 'fixture',
    status: 'partial_success',
    screening_outcome: 'partial_success',
    screening_generated_at: '2099-01-08T10:04:00+08:00',
    market_data_at: '2099-01-08T10:02:00+08:00',
    model_version: 'V2.1',
    trigger_source: 'synthetic_fixture',
    integrity: { ok: true },
    reason_codes: ['fixture_partial_result'],
  },
  screening: {
    candidates: [
      {
        code: 'DEMO-A', name: '虚构样本甲', score: 41.2, score_coverage_pct: 44,
        confidence_label: '低', risk_gate: '仅演示',
        public_outcomes: {
          '1d': { status: 'completed', gross_return_pct: 1.2, net_return_30bps_pct: .9, mfe_pct: 2.1, mae_pct: .4, max_drawdown_pct: .3, hs300_excess_pct: .6 },
          '3d': { status: 'pending', reason_code: 'fixture_3d_not_mature' },
        },
      },
      {
        code: 'DEMO-B', name: '虚构样本乙', score: 36.8, score_coverage_pct: 38,
        confidence_label: '低', risk_gate: '仅演示',
      },
    ],
  },
  research: {
    evidence_status: 'insufficient_evidence',
    conclusion_class: '表现接近/证据不足',
    cost_bps: 30,
    models: {
      short_term_relative_strength_daily_v1: {
        '1d': { sample_count: 0, net_win_rate_pct: 88.8 },
      },
      low_volatility_daily_60d_v1: {
        '1d': { sample_count: 0, net_win_rate_pct: 77.7 },
      },
    },
    pending_horizons: { '20d': 'fixture_20d_not_mature' },
  },
});
