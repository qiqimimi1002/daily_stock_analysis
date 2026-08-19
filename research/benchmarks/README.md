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
fetched_at, generated_at, parameters, calculation_version,
snapshot_content_sha256
```

`score` is nullable. Models are not required to manufacture a 0–100 score;
their native measurement belongs in `raw_metric`.

`signal_id` identifies one **logical stock-selection signal**, not one exact
calculation snapshot. It is UUIDv5 over only:

```text
model_id + stock_code + signal_date
```

`model_id` already freezes model family, version, variant, calculation version
and canonical parameters. Therefore changing the model, parameters, stock or
signal date changes `signal_id`, while a rerun with a slightly different
capture time, price, metric, score or rank does not create another statistical
sample.

Every signal also stores `snapshot_content_sha256`. It is the SHA-256 of the
signal's canonical strict-JSON record with only the hash field omitted, so it
is independently reproducible. The covered record includes model-generation
time, market/data times, optional fetch-completion time, reference price, rank,
score, raw metric, reason and display metadata. Reruns can therefore share one
logical `signal_id` while retaining distinct auditable snapshot hashes. Batch
serialization rejects duplicate logical IDs so a rerun cannot be counted twice
inside one comparison batch. Immutable archive batch IDs and file/content
hashes remain the separate batch-level audit mechanism.

`serialize_signal_batch()` writes canonical strict JSON and sorts signals by
rank, stock code and signal ID. NaN, Infinity, naive timestamps, non-positive
prices, invalid codes and ambiguous dates are rejected rather than cleaned or
guessed.

## Time and no-lookahead rules

All datetimes must include a timezone and are normalized to `Asia/Shanghai`.
The contract requires:

```text
signal_date == market_data_at date
source_data_as_of <= market_data_at <= generated_at
```

`market_data_at` is the market decision snapshot represented by the signal.
`source_data_as_of` is the latest content time of data actually used by the
selection calculation, so it may not be later than that decision snapshot.
`generated_at` is the time the model result finished; it is copied from the
batch model identity into every signal for self-contained audit and never
authorizes using later data.

`fetched_at` is an optional acquisition/download/serialization completion
timestamp. It is audit metadata only and may be later than `market_data_at`;
it is deliberately excluded from both the no-lookahead authorization rule and
logical `signal_id`. Its value is nevertheless covered by
`snapshot_content_sha256`. All four values, when present, must be timezone-aware
and are normalized to `Asia/Shanghai`. Future models must additionally enforce
their field-level point-in-time rules; this framework does not query or repair
source data.

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

## Low Volatility Phase 2A contract

The first project benchmark is `low_volatility_daily_60d_v1`, family
`low_volatility`, variant `project_baseline_60d`. It is the project's own
engineering baseline and **is not an original implementation of any paper**.
A future paper reproduction must use a new variant and model identity rather
than overwrite this contract.

Phase 2A freezes specifications and deterministic synthetic tests only. It
does not fetch market data, run the full universe, emit real benchmark signals
or choose parameters from outcome results.

### Formula and ranking

For ordered completed closes, the only return definition is:

```text
r_t = close_t / close_(t-1) - 1
volatility_daily_60d = sample_std(last_60_daily_returns, ddof=1)
volatility_annualized = volatility_daily_60d * sqrt(252)
```

Exactly 60 returns require 61 closes. A 60-close window is
`insufficient_history`; the window is never shortened. The annualized value is
display/audit metadata only. The complete eligible ranking is frozen as:

```text
volatility_daily_60d ASC, stock_code ASC
```

No market cap, valuation, profitability, liquidity, industry or momentum
tie-breaker is allowed. `benchmark_top_n=5` is frozen for Phase 2B signal
selection, but the complete eligible ranking must be preserved before Top 5 is
chosen. `score` remains `null`; no artificial 0-100 score is created.

### History and decision-time contract

The input must declare the exact last 61 expected trading dates from a named
point-in-time calendar source, plus `previous_completed_trade_date`. The dates
must be unique, increasing, strictly earlier than `signal_date`, and the last
date must equal that declared previous completed session. A close is required
for every one of those dates. Extra older data cannot replace a missing date;
there is no forward fill, backward fill, interpolation or window expansion.

For a signal at T 10:00, factor history ends at T-1 or the most recent earlier
completed exchange session. A T intraday/daily bar or T close is rejected. The
three concepts remain separate:

- factor cutoff: `previous_completed_trade_date` and the 61-date history;
- decision snapshot: T `market_data_at`;
- reference price: the unified T snapshot price, supplied only when Phase 2B
  creates a signal.

All datetimes are timezone-aware and normalized to `Asia/Shanghai`. The
contract requires:

```text
history_data_as_of <= source_data_as_of <= market_data_at <= generated_at
```

`fetched_at` may be later and is audit metadata only; it never authorizes later
content.

### Price and corporate-action policy

Phase 2A accepts only explicitly declared `raw_unadjusted` closes with a named
`history_source`. It rejects `qfq`, `hfq` and unknown bases. It never assumes a
current adjusted series is point-in-time safe because later corporate actions
may have been written backward into history.

The first version performs no adjustment. A named corporate-action source and
its point-in-time data cutoff are mandatory. If review coverage is not proven,
or any dividend/ex-rights, bonus issue, split, rights issue or other material
action occurs inside the 61-close window, the status is
`corporate_action_review` and no volatility metric is emitted. Action knowledge
later than `market_data_at` is rejected as look-ahead data.

### Universe and model identity

The existing V2.1 adapter remains the only eligibility implementation.
Phase 2A records both:

```text
universe_contract_version = v2_1_mainboard_v1
universe_config_hash = SHA256(canonical semantic universe config)
```

The hash covers board/name policy and V2.1 price, change, turnover, amount,
five-day and history thresholds, while excluding retry/worker settings. Both
the version and hash enter model parameters and therefore `model_id`.

The full model identity also freezes `return_type=simple`,
`lookback_returns=60`, `required_close_observations=61`, `std_ddof=1`,
`annualization_factor=252`, ascending rank, `top_n=5`, the raw-price policy and
the corporate-action policy. Changing any one creates a distinct experiment;
the 60-day v1 identity is never overwritten by future 20/120/252-day variants.

An eligible future signal's `raw_metric` contains daily and annualized
volatility, return/close counts, window dates, price basis, history and calendar
sources, data cutoff, corporate-action audit state, and the Universe
version/hash. The synthetic fixture covers low/medium/high volatility, an exact
tie, 60-close insufficiency, a missing date and corporate-action review.

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

## Deferred beyond Phase 2A

- full-market Low Volatility execution and real benchmark signals;
- live/history data adapters and production integration;
- 12-1 Momentum, Value + Profitability or any other benchmark model;
- real-market benchmark runs and cross-model ranking;
- outcome, win-rate, factor optimization or trading analysis.

Phase 2B must start only after the Phase 2A contract is accepted.
