# Project Status

> Last updated: 2026-08-04 (Asia/Shanghai)
>
> Codex workflow rule: read this file before substantial project work and
> update it after every completed material task. Complete safe in-scope work
> directly instead of asking the user to edit files step by step.

## Current state

- Repository: `qiqimimi1002/daily_stock_analysis`
- Stable branch: `main`
- Stable V2.1 merge: `c56623f19256fe7b633aee579d7fceda31bd8659`
- Retained traceability branch: `agent/v2-2-signal-archive` (do not delete yet).
- V2.2 research phase 1 is complete and merged.
- Pull request [#5](https://github.com/qiqimimi1002/daily_stock_analysis/pull/5)
  was marked Ready and squash-merged into `main` on 2026-08-03.
- Phase-1 squash commit:
  `41d64f6ea504129bb93b78cb97694b0a553b43d8`.
- V2.2 research phase 2 is implemented on the independent branch
  `agent/v2-2-outcomes`, created from the latest `main` commit
  `e4809e974185292ba6acd62fe5df1f1dfb5bee14`.
- Phase 2 is awaiting review in a new Draft PR and must not be merged before
  human acceptance.
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
- Records both `source_file_sha256` (exact artifact bytes before parsing) and
  `source_content_sha256` (cleaned canonical JSON). A same-batch byte change,
  including formatting-only changes, is an immutable conflict even when the
  canonical content is unchanged.
- Records `market_data_at_source` and `market_data_at_precision` in every
  normalized signal, Parquet row, JSON batch, and manifest. CLI overrides must
  declare both values explicitly; no override is treated as an exact snapshot
  by default.
- Missing optional fields stay null/empty; non-finite optional numbers become
  null; required reference prices must be finite and positive.
- Same-day intraday signals cannot label the current reference price as a
  not-yet-formed closing price.
- Added two clearly labelled synthetic test signals under `research/examples/`.
- Research-only dependencies are isolated in `requirements-research.txt`;
  production `requirements.txt` is unchanged.

## Verification evidence

- Isolated research environment (`pyarrow` and `tzdata` only): 25 tests passed,
  including real Parquet write/read.
- Real CLI example run: first invocation returned `created`; identical second
  invocation returned `exists` with identical signal IDs and content hash.
- Repository pytest selection: 42 passed, 1 optional PyArrow test skipped in the
  production Python environment.
- Existing V2.1 suites included in that selection:
  `tests/test_market_scoring.py` and `tests/test_market_screener.py`, 18 passed.
- Python compilation, flake8 critical checks, and `git diff --check`: passed.
- GitHub Actions CI run
  [#30790563105](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30790563105)
  completed successfully on the implementation commit.
- Final PR-head CI run
  [#30796325156](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30796325156)
  completed successfully on `d50d2160a7ba4bf514c0fd1c4118330efef76698`.
- The repository CI workflow is configured for `pull_request` events only, so
  the squash merge did not create a separate push-triggered CI run on `main`.
  The merged commit and the CI-validated PR head have the identical Git tree
  `bcf2e6774427a97f30ba370391724d13da2a83d4`, which verifies that the exact
  merged code content is the content that passed CI.
- The only push-event run on the squash commit was Auto Tag
  [#30797667479](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30797667479),
  which completed with the expected `skipped` conclusion.

## Explicitly not implemented

- No aggregate or grouped win rate, factor research, backtest library,
  performance library, or order execution.
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
- The repeated acceptance used explicit legacy-artifact metadata:
  `market_data_at=2026-08-03T11:15:14+08:00`,
  `market_data_at_source=operator_override`, and
  `market_data_at_precision=batch_completion_upper_bound`.
- The first run returned `created`; the second returned `exists`, with the same
  archive path, content hash `deb91ebec...`, and all five signal IDs.
- The manifest's exact source-file hash
  `9649740870f31687e53e95c6e217f8d760d5e1c88a9309bef8e09c0175230c14`
  and canonical source-content hash
  `94b3a52fa9099da9c672111de5cd0a089bdbd07975000b6e8db94169cf52043f`
  independently matched recomputation from the real artifact.
- JSON, Parquet, and manifest agreed on time provenance/precision and on the
  sampled `600089` and `600309` identity, price, score, coverage, model, and
  signal-ID fields.
- Changing `600089` latest price from `21.27` to `21.28` returned exit code 3
  with `ArchiveConflictError`. The original JSON, Parquet, and manifest hashes
  remained unchanged, and exactly one batch directory remained.

## Phase-1 closure

- Status: **accepted, squash-merged, and closed** for the requested V2.2
  phase-1 scope.
- The two medium provenance findings are resolved: exact artifact bytes and
  canonical JSON have separate hashes, and quote-time provenance/precision are
  explicit and cross-file consistent.
- The real legacy artifact remains correctly labelled as an operator-supplied
  batch-completion upper bound, not an exact quote snapshot.
- PR #5 head was `d50d2160a7ba4bf514c0fd1c4118330efef76698`;
  squash commit is `41d64f6ea504129bb93b78cb97694b0a553b43d8`.
- The source branch remains available for audit and traceability.

## Next actions

1. Review and manually accept the V2.2 phase-2 Draft PR; do not merge it yet.
2. Keep `agent/v2-2-signal-archive` until the user authorizes deletion.
3. Do not begin aggregate win-rate or factor research until phase 2 passes
   human acceptance.

## V2.2 phase 2 implementation

- Added the manual/local `calculate-outcomes` CLI. It reads verified immutable
  phase-1 signal batches and never writes into their directories.
- Calculates independent 1, 3, 5, 10, and 20 exchange-trading-day observation
  outcomes for every `signal_id`, using its archived `reference_price`.
- Stores target close/return, maximum upside from actual highs, maximum adverse
  excursion from actual lows, and true peak-to-later-trough maximum drawdown
  over the reference-price-plus-close path.
- Uses an explicit exchange calendar supplied by the price artifact; weekends,
  holidays, and suspensions never extend the requested horizon.
- Supports `pending`, `complete`, `missing_price`, `suspended`,
  `corporate_action_review`, and `data_conflict` outcomes without filling or
  guessing missing target prices.
- Records target limit-up/down state, signal-price proximity to limit-up, and
  execution-risk labels while making no fill or achievable-return claim.
- Stores results independently under
  `research/data/outcomes/YYYY/MM/DD/batch-<hash>/` as JSON, Parquet, and a
  provenance/hash manifest.
- Stable result identity is UUIDv5 over
  `signal_id | horizon_days | calculation_version`; identical inputs are
  idempotent, while changed price bytes or calculation version produce a new
  preserved batch.
- Prevents future leakage with an explicit timezone-aware `--as-of`, immutable
  archived scores, a price-data cutoff, fixed target exchange dates, and a
  15:00 Asia/Shanghai daily-session maturity rule.
- Added a clearly labelled synthetic raw-OHLC fixture and three hand-calculated
  cases; they are test data, not recommendations or real performance.

## V2.2 phase 2 verification

- Isolated research environment with PyArrow: 48 tests passed across phase-1
  archive and phase-2 outcomes, including real Parquet write/read and CLI use.
- Repository regression selection: 63 passed, 3 optional PyArrow tests skipped
  only because the production Python environment lacks the isolated research
  dependency. This selection includes both research phases and existing V2.1
  scoring/screener suites.
- The three manual cases agree with the implementation: normal-path returns and
  excursions, true close-path drawdown, exact-date suspension without rollover,
  and corporate-action review using raw unadjusted observations.
- Python compilation, flake8 checks, JSON validation, and `git diff --check`
  passed before publication.
- No GitHub Actions, V2.1 scoring, 10:00 screening, 10:30 review, formal report,
  or production dependency file was modified.
