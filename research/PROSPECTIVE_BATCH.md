# Prospective shared-batch capture phase 1

## Scope

This research-only collector freezes one evidence batch for both
`short_term_relative_strength_daily_v1` and `low_volatility_daily_60d_v1`.
It does not calculate either model, fetch a provider, schedule a job, backfill
history, run the benchmark race or publish a recommendation.

The command consumes an already-acquired Private JSON bundle. It reconstructs
and executes the existing Phase 2B contracts before writing anything:

- V2.1 Universe contract version and current config hash;
- Baostock plus AKShare/Sina verified trading calendar;
- one exact 61-session raw-unadjusted history window ending at T-1;
- Baostock primary and AKShare/Sina cross-source full-row agreement;
- point-in-time corporate-action primary/cross snapshots, including event
  `known_at`, `source_data_as_of` and `fetched_at`;
- one evidence hash bound identically to both frozen model names.

The raw-history and corporate-action decisions remain **CONDITIONAL PASS**.
The collector does not upgrade their licences, historical-vintage guarantees
or provider reliability.

## Capture time and no-lookahead

For signal date T, `request_at`, `market_data_at` and `captured_at` must all be
on T in Asia/Shanghai and satisfy:

```text
request_at <= every provider fetched_at <= market_data_at <= captured_at
captured_at <= collector observed_at, on the same Asia/Shanghai signal date
history_window_end == previous_completed_trade_date < T
event.known_at <= market_data_at
source_data_as_of <= fetched_at <= market_data_at
```

Even after 15:00, a T daily bar is rejected: this collector intentionally
freezes T-1 and earlier completed sessions only. The recommended manual first-
stage capture is after all Private provider reads finish and before the chosen
same-day `market_data_at` decision snapshot.

## Private input schema

The top-level `prospective-shared-batch-input-v1` object contains:

```text
schema_version
signal_date
request_at
market_data_at
captured_at
universe
  contract_version, config_hash, stock_codes
calendar
  query range, normalized trading_dates, content_sha256
  primary_source and cross_source timestamps/source IDs
symbols[stock_code]
  raw_history.primary/cross
    requested range, raw_unadjusted declaration, units, source_data_as_of,
    fetched_at, raw bars
  corporate_actions.primary/cross
    source ID, source_data_as_of, fetched_at, events including known_at
```

`symbols` must match the sorted V2.1 Universe snapshot exactly. Every symbol
must pass every contract; the collector never silently shrinks the Universe or
lets the two models fetch independently.

## Immutable archive

The Private target is unique per signal date:

```text
<private-root>/YYYY/MM/DD/shared-batch-v1/
  private-batch.json
  manifest.json
  public-manifest.json
```

Validation always happens before checking an old archive. Therefore a failed
new attempt cannot be reported as the old success. After full validation:

- an absent target is written to a temporary directory and atomically renamed;
- byte-identical content returns `exists` and creates no extra files;
- different same-day content, a missing/corrupt file or an unexpected entry
  raises `immutable_archive_conflict` and preserves the original;
- the optional Public manifest is also create-once/identical-only.

The original Private input and `--private-root` must be outside the repository.
The CLI rejects repository-local raw storage even if a path is gitignored.

## Public boundary

The Public manifest may contain only schema/version, status, reason codes,
timestamps, model names, counts, source/evidence hashes, Universe/calendar
hashes and the Private manifest/content hashes. It contains no stock-code list,
OHLCV/amount rows, corporate-action rows or provider response bodies.

No real manifest is committed by this phase. Public CI uses generated fictional
fixtures only.

## Command

```powershell
python scripts/research_prospective_batch.py `
  --input D:\private-research\incoming\2026-08-24.json `
  --private-root D:\private-research\immutable-batches `
  --public-manifest research\runtime\prospective-2026-08-24.json
```

Network is absent from the command. A non-zero exit prints only a stable
`FAIL_CLOSED reason_code=...`; it does not fall back to adjusted, single-source
or older evidence.

## Fail-closed conditions

Failures include invalid/unsorted/duplicate Universe or dates, calendar source
or hash disagreement, non-T-1 windows, missing 61-session history, qfq/hfq,
raw field disagreement, incomplete or future-known corporate-action evidence,
provider timestamps outside the request/market interval, cross-day/backdated or
wall-clock-historical capture,
incomplete Universe evidence, immutable conflicts and storage errors.

Phase 1 makes the validation and archive boundary ready. Continuous real
collection still requires an explicitly authorized Private acquisition step
that can supply complete same-day licensed evidence for every Universe symbol;
no provider orchestration or schedule is added here.
