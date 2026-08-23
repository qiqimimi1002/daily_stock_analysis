# Private acquisition phase 1

## Scope

This research-only command performs one explicit, manual or otherwise
controlled acquisition before the signal date's formal full-market screening.
It fetches the accepted Baostock/AKShare-Sina trading-calendar pair and, once
per V2.1 Universe symbol, the accepted Baostock-primary/AKShare-Sina-cross
`raw_unadjusted` daily-history pair. It then hands one in-memory bundle to the
merged prospective shared-batch collector.

It does not run either model, change V2.1 or either frozen formula, schedule a
job, backfill a date, run the benchmark race, publish a recommendation or use a
production provider/fallback path. Network access requires `--allow-network`.

## Private acquisition request

The request itself is Private and must be created on signal date T after the
acquisition request starts. It contains:

```text
schema_version = private-acquisition-request-v1
signal_date
request_at
private_archive_policy
  raw_history = private_only_no_redistribution
  corporate_actions = private_only_no_redistribution
  provider_terms_reviewed_for_private_capture = true
universe
  contract_version, config_hash, sorted stock_codes, snapshot_sha256
corporate_actions[stock_code]
  primary/cross source IDs, source_data_as_of, fetched_at, events and known_at
```

The Universe hash covers exactly its contract version, current V2.1 config
hash and sorted stock codes. The corporate-action map must cover the same
codes exactly. These two inputs are not reacquired by this command: the current
accepted corporate-action contract has no safe fallback and does not treat an
empty provider response as reviewed-clear evidence. Inventing an empty event,
shrinking the Universe or silently declaring a no-event result would weaken
the frozen contract, so the command fails closed instead.

Consequently, this phase is ready to acquire and freeze raw-history pairs only
when an authorized same-day Private upstream has already supplied the exact
V2.1 Universe snapshot and complete dual-source, reviewed-clear corporate-
action evidence. It is not yet an unattended end-to-end collector.

## Time and data flow

For a verified trading date T:

```text
Private request begins on T
  -> same-day Universe/action evidence is bound to the request
  -> dual-source calendar is fetched with no fallback
  -> cutoff = verified previous completed trading date (always before T)
  -> exact last 61 sessions are fetched from both raw sources per symbol
  -> market_data_at is recorded after all reads
  -> captured_at is recorded
  -> existing shared-batch contracts validate the complete bundle
  -> Private immutable archive is created or byte-identical `exists` is returned
  -> optional Public output contains only the sanitized manifest
```

The command deliberately rejects a non-trading signal date. It queries no T
daily bar, even when run after the close. A source error, different cutoff,
missing session, suspended-row conflict, OHLCV/amount disagreement, adjusted
series, action conflict, future-known event or bad timestamp produces no new
successful batch.

## Command

```powershell
python scripts/research_private_acquisition.py `
  --request D:\private-research\requests\2026-08-24.json `
  --private-root D:\private-research\immutable-batches `
  --public-manifest research\runtime\prospective-2026-08-24.json `
  --allow-network
```

The request and Private root must be outside the repository. Raw provider rows,
stock codes and corporate-action details never appear in stdout or the Public
manifest. A failure prints only a stable reason code. No real provider call is
made by Public CI; all tests use fictional injected sources.

## Immutability and retries

The existing shared-batch writer remains the only archive writer. It validates
the newly acquired evidence before consulting an old archive. Identical content
returns `exists`; changed same-day content, damage or unexpected files raise a
conflict and never overwrite the old success. A failed retry cannot return or
publish an earlier success as the current attempt.

The raw-history and corporate-action source decisions remain **CONDITIONAL
PASS**. The explicit policy assertion records the operator's authorization for
Private storage; it does not create or broaden upstream redistribution rights.
