# V2.2 phase 2: immutable forward outcomes

This offline module reads immutable phase-1 signal batches and measures each
observation signal after 1, 3, 5, 10, and 20 exchange trading days. It measures
price behavior only. It does not claim that a trade was executable and does not
calculate aggregate or grouped win rates.

## Command

```bash
python -m research.cli calculate-outcomes \
  --signals research/data/signals \
  --prices local/unadjusted_prices.json \
  --output research/data/outcomes \
  --as-of 2026-08-04T16:00:00+08:00
```

The command is manual/local only. It is not called by the 10:00 screening or
10:30 review workflows.

## Price artifact contract

The price file is a UTF-8 JSON object. It must contain:

- `price_source`: provider or dataset label;
- `price_data_as_of`: timezone-aware data-fetch cutoff;
- `calendar_source`: source of the exchange calendar;
- `market_trade_dates`: sorted, unique exchange dates with weekends and market
  holidays omitted;
- `stocks`: mapping from six-digit stock code to `prices`, optional
  `corporate_actions`, and optional `data_conflicts`.

Each price row contains `trade_date`, unadjusted `open`, `high`, `low`, `close`,
and optional `volume`, `is_suspended`, `is_limit_up`, `is_limit_down`, and
`limit_up_price`. A suspended row has no usable OHLC. A missing date is not
forward-filled. The target date is never moved to the next transaction day.

The exact price-file SHA-256, canonical JSON SHA-256, source, fetch cutoff, and
calendar coverage are stored in the outcome manifest. `price_data_as_of` must
not be later than `--as-of`. Same-day daily bars are usable only at or after
15:00 Asia/Shanghai, preventing a partial bar from becoming a completed daily
outcome.

The synthetic fixture under `research/examples/` is test data only, not real
prices or an investment recommendation.

## Calculation contract

The reference price is copied from the immutable signal. Horizons use the Nth
entry in `market_trade_dates` strictly after `signal_date`.

- `future_return_pct`: target-day close / reference price - 1.
- `max_upside_pct`: maximum window high / reference price - 1.
- `max_adverse_excursion_pct`: minimum window low / reference price - 1,
  capped at zero when every observed low is above the reference.
- `max_drawdown_pct`: peak-to-later-trough drawdown over the close path
  `[reference_price, day-1 close, ..., target close]`. Daily OHLC cannot prove
  whether the high or low occurred first within one session, so intraday
  high-to-low order is deliberately not invented.

Only actual, finalized bars inside the horizon are used. `valid_market_days`
counts usable bars; `missing_price_days` counts elapsed exchange dates that are
missing or suspended. Pending future dates are not counted as missing.

## Outcome status

- `pending`: target exchange date is not covered or its session has not closed;
- `complete`: target and every window date have usable prices;
- `missing_price`: target or another window date lacks a usable price;
- `suspended`: the target date is explicitly suspended/no-transaction;
- `corporate_action_review`: the mature window contains an ex-rights,
  dividend, split, consolidation, or similar event;
- `data_conflict`: the price artifact explicitly records an unacceptable
  source disagreement inside the mature window.

Pending outcomes have no return or path metrics. Data conflicts also suppress
metrics. Corporate-action rows retain raw unadjusted observation metrics for
audit but remain excluded from any later clean performance population.

Target limit-up/limit-down flags, signal-price proximity to a supplied limit-up
price, and execution-risk labels are recorded. They do not simulate fills or
convert later price movement into an achievable trading return.

## Storage and immutability

Results are separate from phase-1 data:

```text
research/data/outcomes/YYYY/MM/DD/batch-<input-hash>/
|-- outcomes.json
|-- outcomes.parquet
`-- manifest.json
```

`outcome_id` is UUIDv5 over:

```text
signal_id | horizon_days | calculation_version
```

The batch identity additionally includes the exact signal-archive input hash,
exact price-file hash, evaluation cutoff, and calculation version. Identical
inputs return `exists`. A changed price file or calculation version creates a
new batch and preserves the old one. A damaged or internally inconsistent
existing batch raises `OutcomeConflictError`; it is never silently replaced.
The phase-1 JSON, Parquet, and manifest files are hash-verified and never
written by this command.

## Explicitly out of scope

- aggregate, grouped, or factor win-rate statistics;
- adjusted-price or total-return results;
- Alphalens, VectorBT, QuantStats, backtests, fill simulation, or orders;
- V2.1 score recalculation or weight changes;
- intraday buy/sell points;
- production workflows or formal report changes.
