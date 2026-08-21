# V2.2 Research: immutable signal archive

## Public-model comparison framework

The Phase 1 benchmark contract lives in
[`research/benchmarks/README.md`](benchmarks/README.md). It defines deterministic
model identity, logical signal identity plus exact snapshot hashes, strict
point-in-time serialization, a V2.1-aligned offline universe adapter and a
five-field future-outcome handoff. Phase 2A adds the frozen offline contract
for `low_volatility_daily_60d_v1`, but does not run the model, create real
signals, calculate outcomes or integrate with production.

Phase 2B-0B1 adds a strict research-only dual-source A-share trade-calendar and
completed-history-window guard. It requires exact Baostock/AKShare agreement,
is network-disabled by default, and blocks naive/wrong-zone timestamps, future
content and intraday T daily-bar leakage. It does not change the Phase 2A
formula, accept a history/corporate-action source, or modify production paths.
The full contract is documented in
[`research/benchmarks/README.md`](benchmarks/README.md).

The later raw-history and corporate-action source gates are both conditional
research baselines. Corporate actions remain a separate overlay on immutable
raw prices; they do not introduce adjusted bars or production behavior.

This first V2.2 stage records the V2.1 observation list exactly as it existed
when the signal was generated. It does not recalculate candidates and does not
calculate forward returns, drawdowns, win rates, or trading points.

## Input contract

The input is the JSON produced by `scripts/run_market_screener.py`. Required
batch-level evidence is:

- `generated_at`: signal-generation time with an explicit timezone;
- `market_data_at`: quote snapshot time with an explicit timezone;
- `market_data_at_precision`: explicit precision when the timestamp comes from
  the artifact;
- `data_source` and `model_version`;
- `candidates`: the existing V2.1 observation candidates.

All timestamps are converted to `Asia/Shanghai`. A naive timestamp is rejected.
`market_data_at` cannot be later than `generated_at`. A same-day signal created
before 15:00 cannot label its reference price as the current day's close.

An artifact-provided timestamp is recorded with
`market_data_at_source=artifact_field`; its precision is never guessed. A
legacy artifact without `market_data_at` may use `--market-data-at`, but the
operator must also supply `--market-data-at-source` and
`--market-data-at-precision`. Allowed source values are `artifact_field`,
`operator_override`, `workflow_metadata`, and `unknown`. Allowed precision
values are `exact_snapshot`, `batch_level`, `batch_completion_upper_bound`, and
`unknown`. An operator override is not automatically classified as an exact
snapshot.

## Run

Install the isolated research dependencies without changing the production
environment:

```bash
python -m venv .venv-research
.venv-research/bin/python -m pip install -r requirements-research.txt
```

On Windows, use `.venv-research\Scripts\python.exe` instead.

Run the isolated research tests without importing production test fixtures:

```bash
.venv-research/bin/python -m unittest tests.test_research_signal_archive
```

On Windows, replace the executable with
`.venv-research\Scripts\python.exe`.

Archive an existing V2.1 artifact:

```bash
python -m research.cli archive-signals \
  --input data/market_screening_20260803_1000.json \
  --market-data-at 2026-08-03T10:00:00+08:00 \
  --market-data-at-source operator_override \
  --market-data-at-precision batch_completion_upper_bound \
  --output research/data/signals
```

The example under `research/examples/` is synthetic test data and is not an
investment recommendation.

## Stable identity and immutability

`signal_id` is UUIDv5 over this canonical identity:

```text
signal_date | stock_code | model_version | batch_id
```

`batch_id` is read from `signal_batch_id`/`batch_id`, supplied by CLI, or falls
back to the original `generated_at`. Repeating the same batch therefore yields
the same IDs.

The archive is stored at:

```text
research/data/signals/YYYY/MM/DD/batch-<batch-hash>/
├── signals.parquet
├── signals.json
└── manifest.json
```

Before parsing, the CLI computes `source_file_sha256` directly from the input
artifact bytes. Separately, `source_content_sha256` hashes the cleaned,
canonical JSON value. Whitespace, newline, or key-order-only changes therefore
change the file hash but not the content hash. The archive uses the strict audit
policy: for the same stable batch, any source byte change is a conflict, even
when the JSON meaning is unchanged. It never silently replaces the original.

The normalized signal content hash excludes only `archived_at` and includes
both source hashes. An identical source file returns `exists`; changed content,
changed source bytes, damaged archive files, or inconsistent metadata raises
`ArchiveConflictError`. Files are written to a sibling temporary directory and
renamed only after JSON, Parquet, and manifest creation succeeds.

## Field representation

`signals.json` retains normalized records, the cleaned raw candidate snapshots,
and the complete cleaned V2.1 source object. Parquet keeps scalar fields
directly; list and mapping fields use canonical JSON strings so later
statistical tools receive a stable schema. `manifest.json` records the batch
times and their provenance, source, model version, signal IDs, normalized
content hash, exact source-file hash, canonical source-content hash, and the
SHA-256 hashes of both data files. The former ambiguous `raw_source_hash` field
is not written by schema V2.2.2.

| Field | Meaning |
|---|---|
| `signal_id` | Stable UUIDv5 for date, code, source model, and batch |
| `signal_date` | Trading date of the source signal |
| `signal_generated_at` | Time the V2.1 observation signal was produced |
| `market_data_at` | Time represented by the quote snapshot |
| `market_data_at_source` | Provenance of the market-data timestamp |
| `market_data_at_precision` | Declared precision of the market-data timestamp |
| `stock_code`, `stock_name` | Source security identity |
| `reference_price`, `reference_price_type` | Positive price captured by the source and its explicit meaning |
| `total_score`, `raw_score`, `available_max_score` | V2.1 score values without recalculation |
| `score_coverage_pct`, `confidence_label` | Source evidence coverage and confidence |
| `score_breakdown` | Complete source component breakdown |
| `latest_price`, `daily_pct`, `five_day_pct` | Source quote and already-formed historical change fields |
| `amount_yi`, `avg_amount_20d_yi`, `turnover_pct` | Source liquidity fields |
| `ma5`, `ma10`, `ma20`, `trend_label` | Source technical state |
| `watch_zone` | V2.1 observation zone, not a buy range |
| `trigger_conditions`, `abandon_conditions` | Source observation conditions |
| `risk_gate`, `risks`, `evidence_gaps` | Source risk and missing-evidence state |
| `data_source`, `model_version`, `source_artifact` | Provenance |
| `archived_at` | Time the immutable archive was first created |

Manifest-only audit fields:

| Field | Meaning |
|---|---|
| `source_file_sha256` | SHA-256 of the exact input artifact bytes before parsing |
| `source_content_sha256` | SHA-256 of the cleaned canonical source JSON |
| `files.signals.json` | SHA-256 of the archived JSON file |
| `files.signals.parquet` | SHA-256 of the archived Parquet file |

Missing optional values are stored as `null`, empty objects, or empty lists.
NaN and Infinity become `null`; required positive reference prices instead
fail validation. No missing evidence is guessed or imputed.

## Explicitly out of scope

- forward 1/3/5/10/20-day returns;
- maximum rise, drawdown, win rate, or performance statistics;
- Alphalens, VectorBT, QuantStats, or order execution;
- V2.1 scoring, 10:00 workflow, 10:30 review, or formal report changes.
