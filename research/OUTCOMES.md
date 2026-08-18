# V2.2 phase 2: immutable forward outcomes

This offline research module reads the immutable phase-1 signal archive and
calculates each signal's 1, 3, 5, 10, and 20 exchange-trading-day outcome. It
does not alter the signal archive, claim an executable trade, calculate win
rates, or run from any production workflow.

## Command

```bash
python -m research.cli calculate-outcomes \
  --signals research/data/signals/YYYY/MM/DD/batch-... \
  --prices local/raw_unadjusted_prices.json \
  --output research/data/outcomes \
  --as-of 2026-08-18T16:00:00+08:00 \
  --round-trip-cost-bps 30
```

All timestamps must carry an offset and are normalized to `Asia/Shanghai`.
The cost-adjusted result is a scenario estimate only; it is not an achievable
or guaranteed profit.

## Price artifact contract

The UTF-8 JSON artifact declares:

- `price_basis=raw_unadjusted`; adjusted prices are rejected;
- `price_source`, `price_data_as_of`, and `calendar_source`;
- sorted, unique `market_trade_dates` with exchange holidays omitted;
- `stocks`, keyed by six-digit code, with raw OHLC rows and optional
  `corporate_actions` and `data_conflicts`;
- optional `benchmark_reference_snapshots` for CSI 300 (`000300`), each
  containing `captured_at` and `price`;
- optional `signal_snapshots`, keyed by signal ID or stock code, for
  contemporaneous limit-price risk flags.

The calendar, not the presence of an individual stock bar, advances the
horizon. Suspension or missing data never moves the target to a later session
and is never forward-filled. A same-day close is usable only at or after 15:00
Shanghai time and only when both `--as-of` and `price_data_as_of` permit it.

The exact input bytes and cleaned canonical price content receive separate
SHA-256 hashes. Corporate actions retain raw observations for audit but mark the
outcome `corporate_action_review`. Data conflicts suppress price metrics.

## Calculation contract

The reference price is the immutable signal's `reference_price`. The window
starts on the next exchange trading day. Decimal-return formulas are:

```text
gross_return = target_close / reference_price - 1
net_return = (target_close / reference_price) * (1 - cost_bps / 10000) - 1
mfe = max(window_high / reference_price - 1)
mae = min(0, min(window_low / reference_price - 1))
max_drawdown = minimum peak-to-later-close return over
               [reference_price, day-1 close, ..., target close]
excess_return = gross_return - benchmark_return
```

The output also records the highest/lowest raw price and its trade date.
Intraday high/low order is never invented for drawdown. Maximum drawdown uses
only the ordered close path.

For CSI 300, the engine chooses the latest supplied benchmark snapshot whose
timestamp is not later than the signal's `market_data_at`. If no such
snapshot or target close exists, benchmark and excess fields are null with an
explicit unavailable status; the stock result remains valid. It never substitutes
the signal-day index close.

## Status and execution-risk flags

- `pending`: target session is not covered or has not closed;
- `complete`: every elapsed window session has a usable raw bar;
- `missing_price`: target or another elapsed session lacks a usable bar;
- `suspended`: the target is explicitly suspended/no-transaction;
- `corporate_action_review`: raw comparability needs manual review;
- `data_conflict`: sources disagree beyond the declared tolerance.

`target_is_limit_up`, `target_is_limit_down`,
`signal_near_limit_up`, and `execution_risks` describe observable risk.
They do not claim that an order could have filled.

## Storage, identity, and immutability

```text
research/data/outcomes/YYYY/MM/DD/batch-<input-hash>/
|-- outcomes.json
|-- outcomes.parquet
`-- manifest.json
```

`outcome_id` is UUIDv5 over
`signal_id|horizon_days|calculation_version`. The batch identity includes
`--as-of`, calculation version, cost scenario, the verified phase-1 archive
hash, and exact price-file hash. Identical inputs return `exists`; changed
inputs create a distinct batch. If an existing batch's JSON, Parquet, manifest,
or content hash differs, `OutcomeConflictError` preserves the original and
refuses overwrite.

The manifest records signal JSON/Parquet/manifest hashes, price byte/content
hashes, source/basis/coverage, parameters, cost note, calculation timestamp and
version, outcome IDs, content hash, and output-file hashes.

## Scope boundaries

Not implemented here: aggregate/grouped win rate, factor optimization,
Alphalens, VectorBT, QuantStats, model training, auto-tuning, buy/sell points,
fill simulation, orders, V2.1 scoring changes, scheduler changes, deep-analysis
changes, or formal-report changes.
