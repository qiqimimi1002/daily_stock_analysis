# Qlib Alpha158 + DoubleEnsemble research shadow

This path has one purpose: use Microsoft Qlib's official Alpha158 feature
handler and DoubleEnsemble model to rank the existing Shanghai/Shenzhen
main-board research universe. It is offline research only. It is not connected
to the 10:00 production screener, notifications, Signal Monitor, schedulers, or
order execution.

## Upstream implementation and licence

- Package: `pyqlib==0.9.7`
- Feature handler: `qlib.contrib.data.handler.Alpha158`
- Model: `qlib.contrib.model.double_ensemble.DEnsembleModel`
- Configuration: the exact model kwargs from Qlib's official
  `examples/benchmarks/DoubleEnsemble/workflow_config_doubleensemble_Alpha158.yaml`
- Licence: Microsoft Qlib is Copyright (c) Microsoft Corporation and released
  under the MIT License. See `THIRD_PARTY_NOTICES.md`.

The `base_model="gbm"`/LightGBM dependency is internal to Qlib's official
DoubleEnsemble implementation. It is not exposed as a second model, benchmark,
or tuning target. This integration does not copy, rewrite, tune, or search the
Alpha158 or DoubleEnsemble algorithm.

## Data contract

The private input is one raw, unadjusted daily CSV per security from an existing
Baostock (`adjustflag=3`), AKShare Eastmoney, or repository `PytdxFetcher`
history adapter, plus a verified exchange calendar. Required daily fields are
`open`, `high`, `low`, `close`, `preclose`, `volume`, `amount`, `turn`,
`pctChg`, `tradestatus`, and `isST`. Alpha158's required `vwap` is the only thin
derived field and is calculated as `amount / volume`; `factor` is fixed at 1.0
because prices remain raw and unadjusted. `turn` may be null only on Pytdx rows:
turnover is not an Alpha158 input, and those rows are fail-closed out of V2.1.

Model-universe eligibility reuses the existing main-board code and excluded-name
helpers: main-board prefixes only, active trading, and no ST row. ChiNext, STAR,
BSE, ST and `*ST` are therefore excluded. The frozen V2.1 baseline separately
calls its existing `apply_spot_filters()` and `build_candidate()` implementations.
Pytdx OHLCVA has no historical turnover field, so those rows are excluded from
the V2.1 baseline rather than filled with an invented value. The adapter writes
Qlib's own file storage format and does not create a parallel database or
market-data service.

Raw rows stay under `research/runtime/` or another ignored private directory.
Only aggregate evidence, hashes, versions, and redacted examples may be made
public. A current-provider historical backfill cannot prove point-in-time
constituent/name vintage and is therefore always labelled
`INSUFFICIENT EVIDENCE`, even when the model executes correctly.

## No-lookahead and output

Train, validation, and test segments must be chronological, non-overlapping,
supplied explicitly, and separated by a two-trading-session label embargo.
DoubleEnsemble fits only train/validation data and
predicts the test segment. A score formed on completed date T-1 is shifted to
candidate date T using the verified trading calendar. Test data is never used
for training, feature selection decisions, or parameter changes.

Each daily batch contains 0 or up to 5 candidates. Fewer than 3 finite scores
produces 0 candidates. Otherwise scores are ordered descending with stock code
as the deterministic tie-break. Candidate rows contain trade date, code, name,
rank, DoubleEnsemble score, data cutoff, model/config version, and data status.
They contain no buy/sell/stop price and no trading advice.

The sample-outcome adapter reuses the existing unified-race
`BacktestEngine`/aggregation semantics for 1d, 3d, 5d and 10d gross return,
30bps net return, HS300 excess, MFE, MAE, and date-level maximum drawdown. The
frozen V2.1 implementation is called as a baseline and is not modified.

## Offline commands

```bash
python scripts/research_qlib_doubleensemble.py prepare \
  --source research/runtime/qlib/raw \
  --provider research/runtime/qlib/provider \
  --start 2023-09-01

python scripts/research_qlib_doubleensemble.py run \
  --source research/runtime/qlib/raw \
  --provider research/runtime/qlib/provider \
  --output research/runtime/qlib/result \
  --train-start 2024-01-02 --train-end 2024-12-25 \
  --valid-start 2025-01-02 --valid-end 2025-06-25 \
  --test-start 2025-07-01 --test-end 2026-08-21
```

The source directory is populated through the repository's existing stable
history adapters; this integration adds no acquisition command or market-data
service. Both commands above are offline and neither is called by a production
workflow.

## Frozen-model and daily prospective entry

The research CLI also contains two deliberately manual commands:

```bash
python scripts/research_qlib_doubleensemble.py freeze-model \
  --provider research/runtime/qlib/provider \
  --artifact research/runtime/qlib/frozen-doubleensemble-v1 \
  --expected-candidates research/runtime/qlib/result/candidates.json

python scripts/research_qlib_doubleensemble.py shadow \
  --provider research/runtime/qlib/provider \
  --artifact research/runtime/qlib/frozen-doubleensemble-v1 \
  --archive-root research/runtime/qlib/prospective-shadow \
  --trade-date YYYY-MM-DD --data-as-of YYYY-MM-DD
```

`freeze-model` uses only the approved segments, Qlib 0.9.7, random seed 0,
and the unchanged official configuration. A valid artifact is created
atomically only after the 2026-08-24 code/rank replay and every score pass the
declared absolute tolerance of `1e-10`. A failure receipt prohibits another
fit. `shadow` contains no fit call: it verifies and loads that artifact, checks
that `data-as-of` is exactly the completed T-1 trading session, and archives
only `result.json` plus a hash manifest. Same-input reruns verify and return the
first result; input, output, or file conflicts preserve the original and fail
closed. Fewer than three finite scores produces zero candidates. No outcome is
pre-filled; the existing Outcome Engine can evaluate naturally matured days
later.

The one permitted real replay on 2026-08-24 did **not** pass: at least one
score exceeded `1e-10`. The implementation rejected it before serializing or
archiving the model. It did not persist rejected predictions, so the exact
score delta is intentionally unavailable and no second replay is authorized.
The committed failure evidence is
`research/results/qlib_doubleensemble_freeze_replay_2026-08-24.json`; the
private receipt is
`research/runtime/qlib/frozen-doubleensemble-v1.replay-failed.json`.
Consequently the `freeze-model` and `shadow` CLI commands now reject before
training or inference. No prospective archive exists, and this branch is not
ready to accumulate next-trading-day shadow samples.

## Frozen sample-out evidence (2026-08-24)

The first strict chronological run used train `2024-01-02..2024-12-25`,
validation `2025-01-02..2025-06-25`, and test
`2025-07-01..2026-08-21`, with a two-session embargo between segments. The
provider contains 3,046 eligible instruments and 2,256,450 raw rows from
3,193 current-active main-board source files. Alpha158 produced all 158
features, with 99.987343% non-null test features. DoubleEnsemble produced
844,501 predictions and 280/280 non-empty daily Top-5 batches.

After 30 bps cost, DoubleEnsemble mean net returns were 1.2057%, 1.6382%,
1.6412%, and 1.5079% for 1d/3d/5d/10d. Mean HS300 excess returns were
1.1471%, 1.4563%, 1.3223%, and 0.9025%. However, 3d/5d/10d net medians were
negative and the 10d date-level maximum drawdown was 92.1676%. The complete
aggregate evidence and candidate example are frozen in
`research/results/qlib_alpha158_doubleensemble_oos_2026-08-24.json`.

Evidence status remains **INSUFFICIENT EVIDENCE** because the backfill uses a
current-active universe and cannot reconstruct point-in-time constituent/name
vintage or immutable dual-source history. These results justify only a real
prospective research shadow; they do not justify production use or replacing
V2.1.
