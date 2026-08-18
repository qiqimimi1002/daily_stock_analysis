# Public benchmark model framework — Phase 1

This package defines the common offline contracts for later public-model
comparison experiments. Phase 1 deliberately contains **no stock-selection
model** and produces no live observation list. Its purpose is to make future
model inputs, identities and outputs comparable before any model is added.

## Boundaries

- `research/benchmarks/` is offline research infrastructure only.
- V2.1 remains the production screener and is not called by a workflow through
  this package.
- `research/archive.py` remains the immutable V2.1 signal archive. Its existing
  `signal_date | stock_code | model_version | batch_id` UUIDv5 contract is not
  changed or reinterpreted.
- A future outcome engine may read only the five-field adapter returned by
  `BenchmarkSignal.to_outcome_signal_core()`. This package does not import an
  outcome module and does not calculate returns, MFE, MAE, drawdown or win rate.
- No production workflow, Cloudflare Worker, reader, deep analysis, report or
  notification path imports this package.

## Benchmark model identity

Use `BenchmarkModelIdentity.create(...)`. The complete serialized record is:

```text
model_id
model_name
model_version
model_family
variant
calculation_version
parameters
generated_at
```

`model_id` is UUIDv5 over canonical strict JSON containing:

```text
model_family + model_version + variant + calculation_version + parameters
```

`parameters` therefore contains only critical model parameters. Key order does
not affect identity; changing a parameter, version or variant does. Display
name and generation time are audit metadata and do not change model identity.
Use `variant=original` for a published model's unmodified rules and a stable,
descriptive variant string for any later controlled modification.

## Benchmark signal contract

Use `BenchmarkSignal.create(...)`. Every signal contains:

```text
signal_id, model_id, model_name, model_version, model_family, variant,
stock_code, stock_name, signal_date, market_data_at, reference_price,
rank, score, raw_metric, selection_reason, source_data_as_of,
parameters, calculation_version
```

`score` is nullable. Models are not required to manufacture a 0–100 score;
their native measurement belongs in `raw_metric`.

`signal_id` is UUIDv5 over the fields that define the research observation:
model ID, stock code, signal date, snapshot time, reference price, rank, score,
raw metric, data cutoff, calculation version and canonical parameters. Display
name and human-readable selection reason do not affect identity.

`serialize_signal_batch()` writes canonical strict JSON and sorts signals by
rank, stock code and signal ID. NaN, Infinity, naive timestamps, non-positive
prices, invalid codes and ambiguous dates are rejected rather than cleaned or
guessed.

## Time and no-lookahead rules

All datetimes must include a timezone and are normalized to `Asia/Shanghai`.
The contract requires:

```text
signal_date == market_data_at date
market_data_at <= source_data_as_of <= generated_at
```

Consequently, data dated after model generation cannot enter a signal. Future
models must additionally enforce their own field-level point-in-time rules;
this framework does not query or repair source data.

## Shared V2.1 universe adapter

`evaluate_v21_universe()` accepts an already supplied spot frame plus completed
history-row counts. It performs no network request. The adapter calls the
existing `src.services.market_screener.apply_spot_filters()` function as the
single source of truth for Shanghai/Shenzhen main-board prefixes, ST/new/exit
name exclusions, price/change/turnover ranges, positive trading activity and
the liquidity threshold.

Phase 1 does not invent a separate listing-age threshold. It exposes the
current V2.1 historical sufficiency rule through `min_history_rows`. Results
are deterministic and use these explicit statuses:

| Status | Meaning |
| --- | --- |
| `eligible` | Passed the V2.1 hard filter and history-row requirement |
| `insufficient_history` | Passed spot filters but has too few completed bars |
| `suspended` | Current snapshot has no positive volume or amount |
| `unavailable` | Excluded by V2.1 or history is unavailable |
| `invalid_data` | Required market value or history count is invalid |

The adapter widens only the internal `preselect_limit` to classify every input
row; it does not change V2.1 configuration or production behavior.

## Future outcome adapter

The only reserved handoff is:

```json
{
  "signal_id": "...",
  "stock_code": "600000",
  "signal_date": "YYYY-MM-DD",
  "market_data_at": "...+08:00",
  "reference_price": 0.0
}
```

This is a compatibility contract, not an implementation dependency. Phase 1
does not copy, import or rewrite any unmerged outcome engine.

## Verification

```bash
python -m pytest tests/test_benchmark_schema.py tests/test_benchmark_universe.py -q
python -m unittest tests.test_research_signal_archive
python -m pytest tests/test_market_screener.py tests/test_market_scoring.py -q
```

## Deferred to Phase 2

- implementations of Low Volatility, 12-1 Momentum, Value + Profitability or
  any other public model;
- model-specific data adapters and point-in-time factor calculations;
- real-market benchmark runs and cross-model ranking;
- outcome, win-rate, factor optimization or trading analysis.

Phase 2 must start only after this schema and identity contract is accepted.
