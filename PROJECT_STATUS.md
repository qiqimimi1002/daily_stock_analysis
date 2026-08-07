# Project Status

> Last updated: 2026-08-07 (Asia/Shanghai)
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
- V2.2 phase 2 is under review in Draft PR
  [#6](https://github.com/qiqimimi1002/daily_stock_analysis/pull/6), branch
  `agent/v2-2-outcomes`; its current CI checks pass. This work is separate from
  the screening-result publication change below and was not modified here.
- Do not use or merge the abandoned branch `v2-1-market-scoring`.

## 2026-08-04 executable reader and Gemini retry work

- Baseline: latest `main` commit
  `086fe4b749b3fcb839861391958d9dde60981ac9`; this includes PR #7 and its
  verified fixed `screening-results` entry.
- Implementation branch: `agent/screening-reader-retry` (retained for traceability).
- Added `scripts/read_screening_status.py`. It queries the current trade day's
  Actions runs first, then validates `trade_date`, `run_id`, and `run_number`
  before accepting the fixed manifest; a valid dynamic Artifact is the fallback.
- The executable reader returns `not_started`, `queued`, `in_progress`,
  `failure`, `artifact_read_failure`, `screening_completed`, `partial_success`,
  or `success`, with run identity, workflow conclusion, screening/deep-analysis
  states, availability flags, candidate count, manifest source, and reason codes.
- A live read of 2026-08-04 run 14 / ID `30881432666` returned
  `partial_success`: workflow `completed/failure`, screening `success`, deep
  analysis `incomplete`, five candidates, and a valid run-matched fixed entry.
- Added a bounded Gemini retry policy. Only explicit 429 and 503 failures are
  retried with exponential backoff and a maximum delay. A second configured
  Gemini key is selected only after an explicit 429; 503 and ordinary business
  errors never rotate keys. Retries are finite and secret values are excluded
  from logs and manifests.
- The screening workflow uses two retries by default (5-second base, 20-second
  maximum), writes sanitized JSONL retry events, and still publishes the fixed
  entry and dynamic Artifact when deep analysis remains incomplete.
- Manifest schema 1.1 records sanitized retry events, exhausted per-stock
  failures, and `gemini_429` / `gemini_503` reason codes. Missing deep reports
  remain `partial_success`; no empty or fabricated report is produced.
- Local verification: 50 reader/retry/manifest/V2.1 tests passed; 72 affected
  analyzer regression tests passed; Python compilation, YAML parsing, critical
  flake8 checks, and `git diff --check` passed.
- The 2026-08-07 11:00 reader observation correctly found no current-day result
  and refused to reuse Run 17 from 2026-08-06. A later live check found main
  Run 18 / ID `31152495332`: four candidates, completed deep analysis, valid
  fixed entry, normal data quality, and final reader status `success`.
- Authenticated historical fallback was verified against Run 17 / ID
  `31095796250` after `latest` advanced to Run 18. The reader downloaded
  Artifact `market-screening-17`, returned `manifest_source=artifact`, and
  reported `data_quality_status=degraded` / `history_data_all_failed` because
  all 60 preselected symbols failed history retrieval. This warning does not
  alter V2.1 scoring or falsely turn file-integrity success into a code failure.
- The real Artifact test exposed and fixed a cross-host redirect issue: the
  GitHub bearer token is now removed before following the signed blob URL, so
  credentials are not leaked and authenticated Artifact fallback works.
- No V2.1 score/filter/weight, stock-universe rule, 09:40 cron, formal report,
  research module, or production dependency file changed. Draft PR #6 remains
  outside this branch and must not be modified or merged.
- PR [#8](https://github.com/qiqimimi1002/daily_stock_analysis/pull/8)
  was opened from `agent/screening-reader-retry`; implementation commit is
  `420e6ad9ae7ff5d23ff4e04a13e43a9d49b093f1` and the final PR head was
  `e36f3650a668d5520f22f0fc08d4db939ce7228b`.
- GitHub CI run
  [#31154295127](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/31154295127)
  passed on the implementation commit: change detection, AI governance,
  backend-gate (including the full offline suite), Docker build, and Docker
  smoke all succeeded; unrelated desktop/web jobs were correctly skipped.
- Final PR-head CI run
  [#31154741247](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/31154741247)
  also passed. PR #8 was marked Ready and squash-merged into `main` on
  2026-08-07; squash commit is
  `09d8c73309c6c49cb1a426d23cf323c4efeace92`. The source branch was retained.

### PR #8 production acceptance

- Manual `main` validation used run number 19 / ID `31155790869`, commit
  `09d8c73309c6c49cb1a426d23cf323c4efeace92`, with `top_n=5`,
  `run_deep_analysis=true`, and `force_run=false`.
- Runtime was 2026-08-07 14:57:13 to 15:09:27 Asia/Shanghai. Screening
  succeeded with five candidates; 19 of 60 preselected symbols returned
  history data and all eight evidence-enrichment requests succeeded.
- Gemini naturally returned 503. `000933` retried after 5 and 10 seconds and
  exhausted on attempt 3 without key rotation; `000807` retried once after
  5 seconds and recovered. Reports were produced for `000807` and `600089`;
  no blank report was fabricated for `000933`.
- Manifest schema 1.1 correctly recorded sanitized retry events and returned
  `partial_success` / `deep_analysis_incomplete` / `gemini_503`. The reader
  returned `partial_success`, normal screening data-quality status, and a valid
  run-matched fixed entry. Overall Actions conclusion remained `failure`
  because strict integrity validation rejects an incomplete deep-analysis set.
- Dynamic Artifact `market-screening-19` (ID `8985350349`) and the
  `screening-results` fixed entry both published successfully. All five
  manifest-listed Artifact hashes matched, and the fixed-entry Git blobs for
  the manifest, screening JSON, screened codes, and two reports matched the
  same source bytes. No secret-like key value was found in the saved logs or
  manifest.
- The merged reader classified Run 18 / ID `31152495332` as `success`; Run 17
  / ID `31095796250` through its matching Artifact as `success` with degraded
  data quality and `history_data_all_failed`; and Run 14 / ID `30881432666` as
  `partial_success`. The current Run 19 is `partial_success`.
- Post-merge local verification repeated 50 reader/retry/manifest/V2.1 tests,
  all passing, plus Python compilation and diff checks. `main` still contains
  no daily runtime output. Draft PR #6 remained unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120`.
- Acceptance conclusion: no P0 code or publication blocker. The 10:20 reader
  can be restored with its documented handling for queued, in-progress, and
  partial-success runs. External Gemini availability and partial history-data
  coverage remain non-blocking operational risks to monitor.

## 2026-08-07 historical-price coverage reliability work

- Baseline: `main` commit `09d8c73309c6c49cb1a426d23cf323c4efeace92`.
- Implementation branch: `agent/history-coverage-reliability`; it does not
  contain or modify Draft PR #6 (`agent/v2-2-outcomes`).
- Run 19 / ID `31155790869` was used as the incident sample. Of the 60
  preselected symbols, 19 fetched history and 41 failed. Actions-log evidence
  classified every final failure as an Eastmoney remote disconnect. The
  apparent AKShare primary and efinance fallback both used the same Eastmoney
  `push2his.eastmoney.com` backend, so the old fallback did not provide an
  independent failure domain. No Run-19 evidence indicated code-mapping,
  missing-field, timeout, or insufficient-row failures.
- The minimal implementation keeps the existing filters, scoring, thresholds,
  and adjusted-history convention. It adds an independent Sina history source,
  finite transient retries, an in-process immutable-result cache, per-provider
  attempts/retries/success rates, stable per-symbol failure categories, and a
  latest-common-close consistency check across independent providers.
- Eastmoney-family wrappers are not tried twice after a remote outage. Missing,
  invalid, or conflicting histories are still rejected; no value is guessed,
  carried forward, or admitted by relaxing V2.1 standards.
- Screening JSON and manifest schema 1.2 now include
  `history_success_rate`, `history_failure_reasons`,
  `history_source_stats`, `history_consistency`, and
  `history_data_quality`. Coverage below 70% or any cross-source conflict marks
  the run `insufficient` / low confidence; 70-90% is `degraded`; at least 90%
  is `ok`. These are data-quality diagnostics and do not rewrite the workflow
  or candidate score.
- A real local full-market run at 2026-08-07 16:06 Asia/Shanghai fetched 5,538
  spot rows, preselected 60, and fetched 60/60 histories. Eastmoney failed all
  120 bounded attempts (60 initial plus 60 retries); Sina succeeded 60/60 and
  efinance was skipped as the same unavailable Eastmoney family. The run
  produced five candidates and three analysis codes without deep analysis.
  Its generated schema-1.2 manifest reported `screening_completed`, 100%
  history coverage, `history_data_quality=ok`, and `integrity.ok=true`.
- Live-source caveat: Eastmoney remained unavailable during this validation,
  so all 60 accepted histories came from the independent Sina source and the
  live run recorded `single_backend`. Automated tests cover both matching and
  conflicting independent-source paths; the run did not fabricate a second
  source merely to report a match.
- Verification so far: 54 focused history/V2.1/manifest/reader tests passed;
  Python compilation, critical flake8 checks, workflow YAML parsing, and
  `git diff --check` passed. A desktop attempt to collect the full offline suite
  stopped before execution because this local Python environment lacks existing
  production dependencies such as `markdown2`; GitHub backend-gate remains the
  authoritative full dependency-backed suite.
- Draft PR [#9](https://github.com/qiqimimi1002/daily_stock_analysis/pull/9)
  was opened from `agent/history-coverage-reliability` and remains unmerged.
  The first normalized PR head was
  `f728f50f47959414d59d302e82dcdbcbe8ed1ec0`; all eight remote file blob SHAs
  matched the locally tested commit, and the final diff contained only the
  expected eight files without line-ending noise.
- GitHub CI run
  [#31162832930](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/31162832930)
  passed on that PR head: change detection, AI governance, backend-gate,
  Docker build, Docker smoke, and Docker import checks succeeded; unrelated
  desktop and web jobs were correctly skipped.
- Delivery status: implementation, Run-19 diagnosis, local real-market
  validation, Draft PR publication, and complete CI are finished. Keep PR #9
  as Draft for human acceptance; do not merge it directly into `main`.

## 2026-08-04 screening-result readability work

- Baseline: latest `main` commit
  `e4809e974185292ba6acd62fe5df1f1dfb5bee14`.
- Implementation branch: `agent/screening-results-manifest`.
- Actual incident finding: as of 2026-08-04 12:59 Asia/Shanghai, no scheduled
  “全市场初筛” run existed for 2026-08-04. Therefore the 10:00 reader state was
  `not_started`; there was no run ID, queue record, Artifact, or candidate file
  to read. This is not evidence that V2.1 screening code failed.
- The workflow remains active and its cron remains `40 1 * * 1-5` (09:40
  Asia/Shanghai). The prior scheduled run 13 (ID `30814967880`) did not start
  until 2026-08-03 20:46:24 Asia/Shanghai, showing that GitHub schedule
  creation can be substantially delayed even when cron and timezone are right.
- Added `scripts/build_screening_run_manifest.py` to create and validate
  `data/screening_run_manifest.json`, including run identity/times, source and
  model, counts, candidate/deep-analysis status, SHA-256 hashes, evidence
  coverage, fixed-entry paths, and machine-readable integrity errors.
- Updated `.github/workflows/01-market-screening.yml` to upload the manifest
  with the existing Artifact and publish `latest/` plus date-partitioned
  `history/` results to the dedicated `screening-results` branch. Publication
  is non-blocking and never writes daily outputs to `main`.
- Added live-reader state guidance: query Actions first for `not_started`,
  `queued`, `in_progress`, or workflow `failure`; use the final manifest for
  `screening_completed`, `success`, or `partial_success`; distinguish a
  successful run whose output cannot be fetched as `artifact_read_failure`.
- No V2.1 filter/score/weight, main-board/ST rule, 09:40 cron, formal report,
  research dependency, 10:00/10:30 logic, or production analysis code changed.

### Screening-result verification

- New manifest tests plus V2.1 regression suites: 27 passed.
- Python compile, critical flake8 checks, YAML parse, and `git diff --check`:
  passed.
- Real Artifact run 12 (ID `30781220470`) produced a valid manifest with five
  candidates and all three requested deep analyses detected in the combined
  report.
- Real Artifact run 13 (ID `30814967880`) produced a valid manifest with zero
  candidates and `not_required_no_candidates`, matching the real files.
- A full local offline-suite attempt could not collect because this desktop
  Python environment lacks production CI dependencies such as `python-dotenv`
  and `sqlalchemy`; this is an environment limitation, not a test assertion
  failure. The repository PR CI must run the full dependency-backed gate.
- Draft PR [#7](https://github.com/qiqimimi1002/daily_stock_analysis/pull/7)
  was opened from `agent/screening-results-manifest`; implementation commit is
  `93c610044ca05f79a7a406c3f526a2713912d7da`.
- GitHub CI run
  [#30879799585](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30879799585)
  passed: change detection, AI governance, backend-gate, and Docker build all
  succeeded; unrelated desktop/web jobs were correctly skipped.
- PR #7 was marked Ready and squash-merged into `main` on 2026-08-04.
- PR #7 squash commit:
  `bc6622df517e6ee0d979ea2365fe9f6b567ff3a9`.

### First production validation after PR #7

- Manual `main` run: run number 14, ID `30881432666`, with `top_n=5`,
  `run_deep_analysis=true`, and `force_run=false`.
- Runtime: 2026-08-04 13:40:39 to 13:49:56 Asia/Shanghai.
- Full-market screening succeeded with five candidates: `000630`, `600089`,
  `601168`, `000807`, and `002202`; the first three were selected for deep
  analysis.
- Artifact `market-screening-14` (ID `8881599255`) uploaded successfully and
  contains the screening JSON, screened codes, manifest, market report, and
  logs. All manifest-listed SHA-256 values match the downloaded files.
- The independent `screening-results` branch was created at
  `45441bf258d78c98e168a1e26b207fef9c50ca7e`. Its `latest/` and
  `history/2026-08-04/` files are byte-identical to the dynamic Artifact.
- `main` contains no daily runtime output files; the result branch contains
  only `latest/` and date-partitioned `history/` outputs.
- Final manifest status is `partial_success`, with the sole integrity error
  `deep_analysis_incomplete`. All three Gemini analyses failed to produce
  reports because the external service returned 503 high-demand responses and
  then a 429 quota/rate-limit response. Initial screening, Artifact upload, and
  fixed-entry publication all succeeded.
- The workflow conclusion is `failure` because strict manifest validation
  correctly rejected an incomplete deep-analysis result; this is not a V2.1
  screening or publication failure.
- Live reader states (`not_started`, `queued`, `in_progress`, and
  `artifact_read_failure`) are currently specified in documentation but do not
  yet have an executable reader classifier or tests. Do not restore the 10:20
  reader as fully operational until that reader-side logic is implemented.
- Draft PR #6 remains unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120` and was not merged.

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

1. Do not rerun deep analysis until Gemini capacity/quota is available; when it
   is available, rerun run 14's parameters and require a `success` manifest
   with three completed reports.
2. Before restoring the 10:20 reader, implement and test the documented live
   state classifier and read `screening-results/latest/manifest.json` first,
   with dynamic Artifact fallback.
3. Keep 10:20 as the first state check rather than a guaranteed result time;
   retry at 10:40 or 11:00 for `not_started`, `queued`, or `in_progress`.
4. Keep Draft PR #6 independent until the user starts its acceptance task.
5. Keep `agent/v2-2-signal-archive` until the user authorizes deletion.
