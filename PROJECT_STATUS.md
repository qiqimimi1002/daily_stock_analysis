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
- Pull request: pending creation; must remain unmerged for human acceptance.
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

## Explicitly not implemented

- No forward 1/3/5/10/20-day returns, maximum rise, maximum drawdown, win rate,
  factor research, backtest library, performance library, or order execution.
- No intraday buy/sell points.
- No V2.1 scoring/weight changes.
- No 10:00 screening workflow, 10:30 review, formal report, or production
  dependency changes.

## Next actions

1. Commit and push only the V2.2 phase 1 files; do not include `deliverables/`.
2. Open a dedicated draft pull request targeting `main`; do not merge it.
3. Wait for full GitHub CI and record the final PR URL/check results here.
4. Human acceptance must occur before any V2.2 phase 2 outcome design begins.
