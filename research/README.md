# V2.2 Research: immutable signal archive

This first V2.2 stage records the V2.1 observation list exactly as it existed
when the signal was generated. It does not recalculate candidates and does not
calculate forward returns, drawdowns, win rates, or trading points.

## Input contract

The input is the JSON produced by `scripts/run_market_screener.py`. Required
batch-level evidence is:

- `generated_at`: signal-generation time with an explicit timezone;
- `market_data_at`: quote snapshot time with an explicit timezone, or the same
  value supplied through `--market-data-at` for legacy V2.1 artifacts;
- `data_source` and `model_version`;
- `candidates`: the existing V2.1 observation candidates.

All timestamps are converted to `Asia/Shanghai`. A naive timestamp is rejected.
`market_data_at` cannot be later than `generated_at`. A same-day signal created
before 15:00 cannot label its reference price as the current day's close.

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

Before writing, the archiver hashes the normalized signals and cleaned raw
source snapshot without `archived_at`. If the target contains the same hash it
returns `exists`. If the hash differs it raises `ArchiveConflictError` and
preserves every existing file. Files are first written to a sibling temporary
directory and renamed only after JSON, Parquet, and manifest creation succeeds.

## Field representation

`signals.json` retains normalized records, the cleaned raw candidate snapshots,
and the complete cleaned V2.1 source object. Parquet keeps scalar fields directly; list and mapping fields use
canonical JSON strings so later statistical tools receive a stable schema.
`manifest.json` records the batch times, source, model version, signal IDs,
content hash, and SHA-256 hash of both data files.

| Field | Meaning |
|---|---|
| `signal_id` | Stable UUIDv5 for date, code, source model, and batch |
| `signal_date` | Trading date of the source signal |
| `signal_generated_at` | Time the V2.1 observation signal was produced |
| `market_data_at` | Time represented by the quote snapshot |
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

Missing optional values are stored as `null`, empty objects, or empty lists.
NaN and Infinity become `null`; required positive reference prices instead
fail validation. No missing evidence is guessed or imputed.

## Explicitly out of scope

- forward 1/3/5/10/20-day returns;
- maximum rise, drawdown, win rate, or performance statistics;
- Alphalens, VectorBT, QuantStats, or order execution;
- V2.1 scoring, 10:00 workflow, 10:30 review, or formal report changes.
