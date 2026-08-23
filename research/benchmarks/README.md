# Public benchmark model framework — Phase 1

This package defines the common offline contracts for public-model comparison
experiments. It contains the frozen Phase 2A Low Volatility baseline and the
first offline Short-term v1 research model. It produces no live observation
list and remains separate from production screening.

## Boundaries

- `research/benchmarks/` is offline research infrastructure only.
- V2.1 remains the production screener and is not called by a workflow through
  this package.
- `research/archive.py` remains the immutable V2.1 signal archive. Its existing
  `signal_date | stock_code | model_version | batch_id` UUIDv5 contract is not
  changed or reinterpreted.
- A future outcome engine may read only the five-field adapter returned by
  `BenchmarkSignal.to_outcome_signal_core()`. Short-term v1 reserves the
  1/3/5/10/20-session research horizons through that existing adapter. This
  package does not import an outcome module and does not calculate returns,
  MFE, MAE, drawdown or win rate.
- No production workflow, Cloudflare Worker, reader, deep analysis, report or
  notification path imports this package.

## Phase 2B raw-history source acceptance

Raw daily-history research uses two fixed, independent public interfaces:
Baostock `query_history_k_data_plus` with `frequency="d"` and
`adjustflag="3"` as primary, and AKShare/Sina `stock_zh_a_daily` with
`adjust=""` as cross-source. Both are declared as CNY/share prices, share
volume and CNY amount. The research adapter has no provider fallback and no
cache; all network access is disabled unless the smoke command receives the
explicit `--allow-network` flag.

AKShare's [stock interface documentation](https://akshare.akfamily.xyz/data/stock/stock.html)
defines the empty adjustment mode as unadjusted and documents the daily fields.
Its [MIT software license](https://github.com/akfamily/akshare/blob/main/LICENSE)
does not grant redistribution rights for upstream data. The Baostock Python
package declares a [BSD software license](https://pypi.org/project/baostock/),
while the provider's [disclaimer](https://baostock.com/disclaimer) does not
establish a raw-data redistribution grant. These are software/interface
permissions, not a Public-repository market-data license.

Acceptance is fail-closed on non-raw adjustment, a date outside the frozen
Phase 2B-0B1 completed-session cutoff, natural-day substitution, missing active
dates, duplicates, unsorted rows, any OHLCV difference, or an amount difference
above CNY 0.50. A primary suspended row must be explicitly marked and omitted
by the cross-source. The public manifest contains metadata, counts and hashes
only; provider rows are never serialized.

The source decision is **CONDITIONAL PASS**. Current provider snapshots do not
offer a historical vintage proving what an earlier fetch would have returned,
and the packages' software licenses do not establish permission to redistribute
upstream raw market data. Raw captures therefore stay in a private/local
immutable archive; only sanitized manifests may enter this public repository.
Prospective capture is required before model use, and corporate-action review
remains a separate later acceptance stage.

## Phase 2B corporate-action source acceptance

The research-only corporate-action overlay is documented in
[`../../docs/research-corporate-action-acceptance.md`](../../docs/research-corporate-action-acceptance.md).
Its source decision is **CONDITIONAL PASS**. Issuer/exchange or CNINFO
implementation disclosures are the primary evidence; AKShare/Sina is the
independent cross-source, with Baostock dividend fields and `tradestatus` used
as additional term/suspension evidence. All are current snapshots, not
historical vintages.

Raw unadjusted bars remain unchanged. Cash/stock terms may be used only in a
versioned derived overlay; rights issues and unsupported/special actions stay
`review_required`. Suspended provider-carried OHLC is discarded, never
forward-filled. Any missing event, source conflict, future-known evidence,
calendar mismatch or network failure is fail-closed. Public outputs contain
metadata and hashes only, never upstream event or market rows.

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

## Short-term v1 first-stage contract

`short_term_relative_strength_daily_v1` is an offline, deterministic research
model. It reuses the V2.1 Universe contract and its frozen `top_n=5`; `amount`
may participate only in that existing liquidity eligibility gate and is never
a model score. No industry, market-cap, financial, news, money-flow, RSI, MACD,
moving-average, stop-loss, dynamic-weight or tuned-threshold input is present.

For the exact 21-session raw-unadjusted main-model window ending at T-1:

```text
ret_20 = close_last / close_20_sessions_ago - 1
ret_5  = close_last / close_5_sessions_ago - 1
gate   = ret_20 > 0
rank   = ret_5 DESC, stock_code ASC
```

The complete positive-trend ranking is formed before Top 5 selection. Signal
date, decision snapshot and reference price remain caller-supplied values from
the unified Benchmark race; the model never substitutes a historical close
for the unified reference. `score` remains null and the native measurements
stay in `raw_metric`.

The evaluator requires a successful `HistoryWindowContract` and a hash-matched
`RawHistoryAcceptance` plus its primary `RawHistoryObservation`. Only
`conditional_pass`, prospective-cutoff, Baostock-primary/AKShare-Sina-cross,
`raw_unadjusted` evidence is admitted. A current-snapshot backfill is not a
historical vintage and is rejected for model use. Public payload and licence
boundaries are checked; raw rows remain local/private and are never serialized
into repository outputs.

Short-term v1 is stricter than the calendar's after-close allowance: its daily
signal always uses T-1, so a T bar is rejected even after 15:00. Missing,
duplicate, unordered, future, non-trading or non-finite rows fail closed. The
model never switches to qfq/hfq or edits raw prices. Incomplete corporate-action
review, future-known action evidence, or any action inside the 61-session window
produces no metric and no ranking candidate. This conservative gate preserves
the corporate-action `CONDITIONAL PASS` boundary; it does not claim that current
snapshots provide a historical vintage.

Three ablation factors use the same accepted data contracts but are returned as
independent factor records and never enter the main model rank or weight. Their
separate window contains exactly 61 sessions so the 60-return volatility term
cannot tighten the main model's 21-session history requirement:

```text
vol_contraction_10_60 = sample_std(last_10_daily_returns, ddof=1)
                        / sample_std(last_60_daily_returns, ddof=1)
breakout_strength_20  = close_last / max(prior_20_closes) - 1
volume_ratio_5        = volume_last / mean(prior_5_volumes)
```

Undefined denominators produce an unavailable factor rather than NaN or a
changed main-model decision. The only evaluation handoff is the existing
five-field `BenchmarkSignal.to_outcome_signal_core()` adapter. Main currently
contains no merged Benchmark executor for the five reserved horizons, and the
production DecisionSignal outcome service supports only 1/3/5/10 days; this
research module therefore does not claim that a 20-day evaluation has run.

## Phase 2B-0B1 trade-calendar and no-lookahead contract

Phase 2B-0B1 adds research-only calendar/source acceptance infrastructure. It
does not implement a new factor or generate a stock signal. The production
`src/core/trading_calendar.py` remains untouched because its compatibility
paths intentionally fail open; those semantics are not valid research
evidence.

The fixed independent sources are:

```text
primary: baostock.query_trade_dates
cross:   akshare.tool_trade_date_hist_sina
```

`VerifiedTradeCalendar.create()` accepts only observations for the same
requested interval. Each source must identify the correct role and supply a
non-empty, canonical `YYYY-MM-DD`, strictly increasing, duplicate-free list
whose dates are all inside the request. Only after both lists are normalized to
their unique ascending representation are they compared, and they must match
exactly in both count and dates. Import/query/login failure, empty data, invalid
format, duplicate, source-order anomaly, out-of-range date, count difference or
specific-date difference raises `TradeCalendarContractError`. No partial
calendar object is returned, no source is promoted to sole authority, and
there is no weekday, natural-day or hard-coded-holiday fallback.

A successful calendar records the query interval, fixed source identifiers,
each normalized count, each `source_data_as_of` and `fetched_at`, the `pass`
consistency state, schema/calculation versions, normalized dates and a stable
SHA-256 over canonical JSON. Raw provider responses are never serialized by
the contract. Because neither provider exposes a native calendar publication
timestamp, the live adapters conservatively use response-observation completion
as both `source_data_as_of` and `fetched_at`; this limitation is explicit and
does not authorize later bars. Repository fixtures must state that they are
synthetic.

`HistoryWindowContract.create()` consumes that verified market calendar and
the Phase 1 timestamp names. All timestamps must be timezone-aware with
Asia/Shanghai (`+08:00`) semantics, and it enforces:

```text
history_data_as_of <= source_data_as_of <= market_data_at <= generated_at
```

`fetched_at` is optional audit metadata. It may be later than
`market_data_at`, but it cannot change the completed-bar cutoff or authorize
later content. Both calendar-source `source_data_as_of` values must also be no
later than `market_data_at`; a calendar observed only after the decision point
cannot be retroactively treated as point-in-time evidence. For an A-share
trading date T before the 15:00 completed-daily-
bar boundary, the cutoff is the preceding verified market session. At or after
the boundary, T may be used as a completed session. For a non-trading signal
date, the cutoff is the latest earlier verified market session. The signal date
must itself be covered by the verified query interval.

The observed history dates must equal the last N consecutive verified market
sessions through the cutoff. Missing one required session, shortening the
window, substituting an older or security-specific next session, inserting T
intraday, using a future date, or filling/interpolating is a hard error. The
result exposes `required_trade_dates` and `previous_completed_trade_date` using
the exact Phase 2A input names, so future 2B adapters can pass the accepted
window into the frozen Low Volatility contract without duplicating its formula.

External reads are explicit and separate from the pure contract:

```bash
python scripts/research_trade_calendar_smoke.py \
  --start-date 2026-04-01 \
  --end-date 2026-08-18 \
  --allow-network
```

The script requires `--allow-network`; default calls and all automated tests
make zero network requests. It writes only the verified normalized result to a
system temporary directory unless `--output` is supplied. `research/runtime/`
is gitignored for local evidence. A provider failure or disagreement exits
nonzero without writing a PASS artifact.

Phase 2A's `raw_unadjusted` policy remains frozen. This stage accepts neither a
raw-history source nor a corporate-action source. Until separate source
acceptance proves comparability and point-in-time coverage, incomplete review
continues to produce `corporate_action_review`; qfq/hfq, new return definitions
and automatic adjustment are not introduced here.

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

## Unified Short-term v1 versus Phase 2A race

The research-only aligned evaluator is documented in
[`../UNIFIED_RACE.md`](../UNIFIED_RACE.md). It accepts only common-date,
common-Universe, common-reference inputs that already passed the frozen
calendar, raw-history and corporate-action gates. It reuses the existing pure
1d/3d/5d/10d outcome calculation, applies a fixed 30 bps round-trip cost, and
writes sanitized aggregates only. The three Short-term ablation factors remain
independent Spearman-IC diagnostics and never enter either model's rank.
Both models' `raw_metric.window_end` must equal the batch's single verified
`previous_completed_trade_date`, which must be strictly earlier than T.

The 2026-08-21 real-environment inventory found no prospective/private/
immutable batch with retained raw rows and point-in-time corporate-action
evidence. Its committed result is therefore `insufficient_evidence` with zero
evaluable samples, not a model-performance claim. Benchmark 20d remains
pending until its unified execution chain is implemented separately.

## Prospective shared-batch capture

The phase-1 Private collector is documented in
[`../PROSPECTIVE_BATCH.md`](../PROSPECTIVE_BATCH.md). It validates one complete
T-1, raw-unadjusted, dual-source and corporate-action evidence bundle, then
binds the same immutable evidence hash to Short-term v1 and Phase 2A. It does
not fetch providers, run either model, schedule collection or permit historical
backfill. Only a sanitized manifest may cross the Public boundary.

The explicit raw-history acquisition coordinator is documented in
[`../PRIVATE_ACQUISITION.md`](../PRIVATE_ACQUISITION.md). It requires a
same-day Private V2.1 Universe plus complete reviewed corporate-action
evidence, fetches the fixed calendar and raw-history pairs with explicit
network opt-in, and delegates the only write to the immutable shared-batch
collector. It adds no scheduler or production path and cannot convert an empty
corporate-action response into reviewed-clear evidence.

## Verification

```bash
python -m pytest tests/test_benchmark_schema.py tests/test_benchmark_universe.py -q
python -m pytest tests/test_short_term_contract.py -q
python -m unittest tests.test_research_signal_archive
python -m pytest tests/test_market_screener.py tests/test_market_scoring.py -q
```

## Deferred beyond the current research-only stage

- full-market Low Volatility execution and real benchmark signals;
- live/history data adapters and production integration;
- 12-1 Momentum, Value + Profitability or any other additional benchmark model;
- real-market benchmark runs and cross-model ranking;
- outcome, win-rate, factor optimization or trading analysis.

Raw-history and corporate-action source acceptance remain conditional gates;
their current-snapshot and redistribution limits are not promoted to full
Phase 2B acceptance by this model implementation.
