# Short-term v1 versus Phase 2A unified race

## Frozen comparison

This research-only evaluator compares exactly two unchanged models:

- `short_term_relative_strength_daily_v1`: `ret_20 > 0`, then `ret_5 DESC`
  and `stock_code ASC`;
- `low_volatility_daily_60d_v1`: the existing 60-return sample-standard-
  deviation formula, ascending, then `stock_code ASC`.

Every accepted signal-date batch must use the same V2.1 Universe codes and
hash, Phase 2B-0B1 calendar, T-1 cutoff, raw-unadjusted prospective history,
corporate-action review, Top 5, decision timestamp, source cutoff, reference
snapshot and forward outcome calendar. A missing forward bar for either
model's selected stocks excludes that signal date from both models for the
affected horizon. There is no qfq/hfq, single-source, old-snapshot or partial-
sample fallback.

The batch carries one `previous_completed_trade_date`. It must precede the
signal date, and every accepted signal from both models must report that exact
date as `raw_metric.window_end`; a different or T-date window fails closed.

The evaluator calls the existing pure `BacktestEngine.evaluate_decision_signal`
calculation for 1d/3d/5d/10d. The merged Benchmark 20d execution chain remains
unavailable, so 20d is explicitly pending rather than reimplemented here.

## Metrics

- gross return: horizon end close divided by the shared signal reference price;
- net return: gross return minus a fixed 30 bps round-trip cost;
- net win: net return strictly greater than zero;
- MFE: non-negative best high excursion from the shared reference price;
- MAE: non-negative worst low excursion from the shared reference price;
- maximum drawdown: peak-to-trough drawdown of the ordered signal-date,
  equal-weight mean net-return series;
- HS300 excess: model net return minus the same-date HS300 gross return;
- stability: positive/negative/flat counts over signal-date mean net returns.

The machine status remains `insufficient_evidence` until every supported
horizon has at least 20 common signal dates. This is a conservative evidence
gate, not a model parameter, and it does not convert a sufficiently sized run
into an automatic efficacy claim.

The three ablation factors are never used in model eligibility, rank or weight.
Their only diagnostic is global and per-date Spearman IC against future net
return. No threshold or parameter search is performed.

The offline runner accepts a private bundle and writes aggregates only:

```bash
python scripts/research_unified_race.py \
  --input <private-immutable-bundle.json> \
  --output research/runtime/unified-race-summary.json
```

Raw prices, bars, corporate-action rows and individual factor observations are
never copied to the output. The default output directory is gitignored.

## Real-environment inventory result on 2026-08-21

Baseline: `main@211de4fb8c8df1ae8c5e227ba9dc91e08d3c7228`.

Research status: **INSUFFICIENT EVIDENCE**. Conclusion class:
**表现接近/证据不足**. This is not evidence that the two models are economically
similar; it means no contract-admissible comparison can yet be made.

- prospective/private/immutable evaluation batches found: 0;
- candidate signals generated from admissible real evidence: 0 for each model;
- common evaluable samples: 0 for every horizon;
- signal-date range: unavailable;
- date-stability observations: 0;
- no model or factor efficacy claim is permitted.

The only located real raw-history manifest contains four current-snapshot
smoke samples. It is excluded because it declares
`acquisition_mode=backfill_current_snapshot`, `raw_rows_persisted=false`, and
does not represent a private immutable historical vintage. No matching
prospective/private/immutable corporate-action capture was found. Re-fetching
current provider history cannot repair those historical point-in-time gaps and
was not attempted.

### Model outcome table

All return, win-rate, excursion, drawdown and excess fields are `N/A` because
the admissible sample count is zero; zero is not substituted for a missing
statistic.

| Model | Horizon | Samples | Dates | Gross mean/median | Net mean/median | Net win rate | MFE | MAE | Max drawdown | HS300 excess mean/median | Excess win rate |
| --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| Short-term v1 | 1d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Short-term v1 | 3d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Short-term v1 | 5d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Short-term v1 | 10d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Phase 2A | 1d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Phase 2A | 3d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Phase 2A | 5d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Phase 2A | 10d | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

### Independent ablation diagnostics

| Factor | 1d samples/IC | 3d samples/IC | 5d samples/IC | 10d samples/IC | Decision |
| --- | --- | --- | --- | --- | --- |
| `vol_contraction_10_60` | 0 / N/A | 0 / N/A | 0 / N/A | 0 / N/A | insufficient evidence |
| `breakout_strength_20` | 0 / N/A | 0 / N/A | 0 / N/A | 0 / N/A | insufficient evidence |
| `volume_ratio_5` | 0 / N/A | 0 / N/A | 0 / N/A | 0 / N/A | insufficient evidence |

The machine-readable aggregate is
[`results/short_term_v1_vs_phase2a_2026-08-21.json`](results/short_term_v1_vs_phase2a_2026-08-21.json).
It contains only counts, reason codes, hashes, metric definitions and null/zero
aggregates; it contains no restricted market rows.
