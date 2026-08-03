# Project Status

> Last updated: 2026-08-03 (Asia/Shanghai)
>
> Codex workflow rule: read this file before substantial project work and
> update it after every completed material task. Complete safe in-scope work
> directly instead of asking the user to edit files step by step.

## Current state

- Repository: `qiqimimi1002/daily_stock_analysis`
- Stable branch: `main`
- Stable V2.1 merge: `c56623f19256fe7b633aee579d7fceda31bd8659`
- Active development branch: `agent/v2-2-signal-archive`
- Current objective: V2.2 research phase 1, immutable structured signal archive.
- Draft pull request: [#5](https://github.com/qiqimimi1002/daily_stock_analysis/pull/5);
  must remain unmerged for human acceptance.
- Do not use or merge the abandoned branch `v2-1-market-scoring`.

## V2.2 phase 1 delivered on the branch

- Added an isolated `research/` package and CLI:
  `python -m research.cli archive-signals`.
- Reads existing V2.1 JSON candidates without recalculation or score changes.
- Stores normalized records and the cleaned raw source snapshot as JSON,
  structured rows as Parquet, and provenance/file hashes in a manifest.
- Uses date-partitioned immutable batch paths:
  `research/data/signals/YYYY/MM/DD/batch-<hash>/`.
- Uses stable UUIDv5 signal identity over signal date, six-digit stock code,
  source model version, and stable batch ID.
- Distinguishes signal generation, quote snapshot, and first archive times;
  requires timezone-aware values and normalizes them to `Asia/Shanghai`.
- Legacy V2.1 artifacts without `market_data_at` require the caller to provide
  `--market-data-at`; the archiver never guesses it.
- Repeated identical batches return `exists`; changed content raises a conflict
  without replacing the original directory or files.
- Existing JSON/Parquet file hashes, manifest consistency, and normalized
  content hash are verified before an archive is reported as existing.
- Missing optional fields stay null/empty; non-finite optional numbers become
  null; required reference prices must be finite and positive.
- Same-day intraday signals cannot label the current reference price as a
  not-yet-formed closing price.
- Added two clearly labelled synthetic test signals under `research/examples/`.
- Research-only dependencies are isolated in `requirements-research.txt`;
  production `requirements.txt` is unchanged.

## Verification evidence

- Isolated research environment (`pyarrow` and `tzdata` only): 18 tests passed,
  including real Parquet write/read.
- Real CLI example run: first invocation returned `created`; identical second
  invocation returned `exists` with identical signal IDs and content hash.
- Repository pytest selection: 35 passed, 1 optional PyArrow test skipped in the
  production Python environment.
- Existing V2.1 suites included in that selection:
  `tests/test_market_scoring.py` and `tests/test_market_screener.py`, 18 passed.
- Python compilation, flake8 critical checks, and `git diff --check`: passed.
- GitHub Actions CI run
  [#30790563105](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30790563105)
  completed successfully on the implementation commit.

## Explicitly not implemented

- No forward 1/3/5/10/20-day returns, maximum rise, maximum drawdown, win rate,
  factor research, backtest library, performance library, or order execution.
- No intraday buy/sell points.
- No V2.1 scoring/weight changes.
- No 10:00 screening workflow, 10:30 review, formal report, or production
  dependency changes.

## Pre-merge acceptance evidence

- Real source: GitHub Actions `全市场初筛` run number 12, run ID
  `30781220470`, artifact `market-screening-12`, file
  `data/market_screening_20260803_1115.json` (5 V2.1 candidates).
- First archive invocation returned `created`; the identical second invocation
  returned `exists`. Both returned the same path, content hash, and five stable
  signal IDs. The archive still contained exactly five unique records.
- Changing `600089` latest price from `21.27` to `21.28` with the same batch
  identity returned exit code 3 / `ArchiveConflictError`. The JSON, Parquet,
  and manifest SHA-256 hashes and batch-directory count were unchanged.
- Cross-file checks passed for `600089` and `600309`: source V2.1 JSON,
  `signals.json`, `signals.parquet`, and manifest agreed on the requested
  identity, price, score, coverage, model, time, and signal-ID fields.
- All three stored timestamps were timezone-aware `+08:00` values. Temporary
  acceptance output was kept outside the repository and was not committed.

## Acceptance findings

- Status: **changes required before acceptance**; PR #5 must remain a draft
  and unmerged.
- Medium: `manifest.raw_source_hash` matches the canonicalized parsed JSON
  object (`94b3a52f...`), but not the original artifact file bytes
  (`96497408...`). Add a separately named byte-level source artifact SHA-256
  while retaining the semantic/canonical hash.
- Medium provenance limitation: the real legacy V2.1 artifact has no
  `market_data_at`. The acceptance run used the explicitly supplied
  `2026-08-03T11:15:14+08:00`, supported by the artifact generation time and
  workflow completion log, but this is an auditable upper bound rather than a
  captured per-quote timestamp. Do not describe it as an exact quote time.

## Next actions

1. Correct only the two V2.2 phase-1 provenance findings above; do not expand
   scope or change V2.1, workflows, reports, or screening/review schedules.
2. Repeat the same real-artifact acceptance checks; keep PR #5 draft and
   unmerged until they pass.
3. Human acceptance must occur before any V2.2 phase 2 outcome design begins.
