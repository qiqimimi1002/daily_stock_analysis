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
schema_version = private-acquisition-request-v2
signal_date
request_at
private_archive_policy
  universe = private_only_no_redistribution
  raw_history = private_only_no_redistribution
  corporate_actions = private_only_no_redistribution
  provider_terms_reviewed_for_private_capture = true
universe_source
  schema/model/source IDs, source_data_as_of, fetched_at, generated_at
  current production V2.1 config and config_sha256
  full same-day Private spot_rows, row_count and spot_content_sha256
corporate_actions[stock_code]
  primary/cross source IDs, source_data_as_of and fetched_at
  either matched real events/known_at or explicit successful no_event queries
```

The production Public result does not retain the full source snapshot or full
eligible set, so it cannot safely reconstruct this input. A Private upstream
must provide the same full spot snapshot used immediately before screening.
The acquisition path normalizes that snapshot, verifies its stable content
hash and the unchanged default V2.1 config, then reuses the existing V2.1
hard-filter adapter. It fetches history for exactly that one sorted code set,
rechecks V2.1's history eligibility, and binds the source-manifest hash and
Universe snapshot hash into both models' shared evidence. No module may rebuild
or shrink the set independently.

`no_event` is not an empty fallback. Both independent action sources must state
that their query succeeded, name the same symbol, cover the exact raw-history
interval and return no event record. Only then is the symbol `reviewed_clear`.
Matched real events remain `review_required`; a failed source, event/no-event
disagreement, interval mismatch or future source state fails closed.

## Time and data flow

For a verified trading date T:

```text
Private request begins on T
  -> same-day full Private spot snapshot is verified and classified by V2.1
  -> one Universe/hash is bound to both models and all action checks
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
There is still no scheduler or unattended provider acquisition. The next gate
is one explicitly authorized, prospective, Private-only single-day acceptance
run; no historical backfill is permitted.
