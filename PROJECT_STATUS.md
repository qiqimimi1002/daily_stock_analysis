# Project Status

> Last updated: 2026-08-18 (Asia/Shanghai)
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
- The original V2.2 phase-2 experiment remains read-only in Draft PR
  [#6](https://github.com/qiqimimi1002/daily_stock_analysis/pull/6), branch
  `agent/v2-2-outcomes`. It was not modified, closed, or merged.
- Do not use or merge the abandoned branch `v2-1-market-scoring`.

## V2.2 per-signal outcome engine v2 (2026-08-18)

- Baseline: latest `main` commit
  `de5c2757cd6ba6d4aaa4266583416f807baf13df`. Development branch:
  `agent/v2-2-outcomes-v2`; a separate Draft PR is pending publication.
- Scope is offline research only. It reads verified immutable phase-1 signal
  archives and writes independent JSON, Parquet, and manifest outcomes for
  1/3/5/10/20 exchange sessions. No V2.1 score, workflow, scheduler,
  Cloudflare, reader, deep analysis, or formal report file was changed.
- The calculation contract accepts only `raw_unadjusted` OHLC, starts on the
  next exchange session, never extends suspension/missing windows, enforces the
  Shanghai 15:00 close boundary, records configurable cost scenarios
  (30 bps default), MFE/MAE, ordered-close maximum drawdown, execution-risk
  flags, corporate-action/conflict states, and time-safe CSI 300 excess return.
- Deterministic validation: isolated research environment 53/53 unit tests
  passed with real Parquet writes; targeted V2.1/research regression 68 passed
  (3 dependency-gated tests skipped in the production Python); `py_compile`
  and full targeted `flake8` passed.
- The repository-wide non-network suite was started locally but its unrelated
  Windows-only Codex backend gate produced the known
  `platform_unsupported` mismatch (14 failures in
  `test_agent_backend_status_service.py`). No adjacent production code was
  changed to mask that environment-specific result; Linux PR CI remains the
  authoritative full-suite check.
- `scripts/check_ai_assets.py` cannot pass in this Windows clone because Git
  materialized `CLAUDE.md` as a regular file instead of the repository's
  `AGENTS.md` symlink. The branch does not modify either file; Linux PR CI
  must verify the canonical symlink checkout.
- Read-only real-signal replay used Run ID `30781220470`, artifact
  `market-screening-12`, file
  `data/market_screening_20260803_1115.json`. The temporary phase-1 archive
  contained five verified signals with content hash
  `8215c9d831bc8935f683dd99530039342c4aa0162e1e44866e8ce43031b01ef0`;
  repeated outcome execution returned `exists` and did not change the signal
  files.
- Real outcome-price acceptance remains blocked: AKShare's raw/unadjusted
  Eastmoney request ended with `RemoteDisconnected`. A clearly labelled
  no-market-data replay verified loading and produced 25 `pending` records,
  but is not claimed as real price-performance evidence. The PR must remain
  Draft until a compliant real raw-price artifact is available.

## P1 same-run market quote consistency (2026-08-14)

- Baseline: `main` commit `009446c04d92127e890a87cb1c8fe6d6e50fdaa5`.
  Implementation branch: `agent/market-snapshot-consistency`; initial
  implementation commit `d046d6f`; PR [#14](https://github.com/qiqimimi1002/daily_stock_analysis/pull/14).
- Root cause: the full-market screener fetched one market-wide spot frame
  (today's Run #34 used AKShare/Eastmoney), while the subsequent Daily Stock
  stage independently fetched each candidate again using
  `REALTIME_SOURCE_PRIORITY` (Run #34 used Tencent first). The two providers
  returned similar current prices but different previous-close bases. The
  screener also trusted the provider percentage field and did not populate its
  already-reserved `market_data_at`; deep-report arithmetic used its separately
  fetched quote/history context. Screening history is qfq and remains used only
  for V2.1 history/MA evidence; it is not the same input as the intraday change
  calculation.
- Fix: the screener now preserves the exact upstream spot source and capture
  time, keeps price and provider/exchange previous close, and computes
  `change_pct` only as `(price - prev_close) / prev_close * 100`. It writes a
  candidate-only `data/market_snapshot.json`; the same workflow sets
  `MARKET_SNAPSHOT_PATH` for deep analysis, where the snapshot is authoritative
  and another realtime provider is not silently queried. Reports prefer the
  snapshot previous close and retain `market_data_at`, the formula, and the
  upstream source. The manifest hashes and validates the snapshot against the
  screening JSON and candidate values before publication.
- Merge-acceptance review found and closed one fail-closed gap: a configured
  invalid snapshot previously returned `None`, allowing the generic pipeline
  to continue with historical close. Deep analysis now runs a snapshot
  preflight before `main.py`; missing files, missing candidates, invalid
  numeric/time metadata, or inconsistent percentages return nonzero, write
  `reports/market_snapshot_error.md`, and are exposed through manifest
  `integrity.errors`, `reason_codes`, and file hashes. Runtime snapshot errors
  are re-raised and cannot call another provider, use historical close, or
  reach the LLM.
- Scope stayed limited to quote consistency, artifact traceability, report
  quote display, tests, and documentation. V2.1 weights/rules, Cloudflare and
  GitHub schedule definitions, the idempotency guard, research/V2.2, and Draft
  PR #6 were not modified.
- Run #34 replay evidence (2026-08-14): screening/deep values were
  `600522` 33.47/+1.73% versus 33.66/+0.33%, `600487`
  58.20/+1.66% versus 58.34/-1.62%, and `002185` 17.91/+0.17% versus
  17.87/-2.35%. The screening bases inferred from the saved price/percentage
  are 32.9008, 57.2497, and 17.8796, while the old deep report used 33.55,
  59.30, and 18.30. Replaying those candidates through the new contract makes
  deep analysis reuse the screening price, previous close, percentage, source,
  and timestamp exactly. This is deterministic replay evidence, not a new
  production Actions run.
- CI on pre-acceptance head `26caffb` passed: main CI Run `31795853951`
  completed `ai-governance`, `backend-gate` (full offline suite), and
  `docker-build`; unrelated desktop/Web jobs skipped. External scheduler CI
  Run `31795853998` also passed without a production dispatch.
- Windows baseline comparison used the same Python 3.12.9 / pytest 9.1.1
  environment and exact command `python -m pytest -m "not network" -q`.
  PR head `26caffb` returned 77 failures and `main@009446c` returned 78; all 77
  PR failure nodeids were present on main (`only PR=0`). After the fail-closed
  acceptance patch, the working tree returned 76 failures; all 76 were still
  a subset of main (`only PR=0`). Main-only observations were the AKShare
  timeout test and, on the final comparison, one local-CLI process-group test.
  Thus none of the reported 77 failures was introduced by PR #14.
- Acceptance verification: 27 focused snapshot/manifest failure-contract
  tests and 113 affected screener, V2.1 scoring, realtime fallback, pipeline,
  history, guard, and workflow tests passed. Compilation, critical flake8,
  workflow YAML parsing, and `git diff --check` passed. The commit containing
  this acceptance update must pass all required PR CI before #14 is marked
  Ready.
- Post-merge production acceptance is intentionally deferred to the next
  trading day's real 10:00 Cron. Do not manually run today's production
  screening. After merge, verify the published snapshot, manifest
  `market_data_at`, and all three deep-report quote rows on that natural Run.

## 2026-08-10 scheduled-trigger reliability work

- Baseline: latest `main` commit
  `2902ed4f95fe78ddbf70b703ee12454eddc640f4`, including merged PR #9.
- Implementation branch: `agent/schedule-fallback-idempotency`. This branch is
  limited to scheduling reliability and does not contain or modify Draft PR #6.
- The full-market workflow keeps the Beijing 09:40 primary schedule and adds
  09:55 and 10:10 fallback schedules. Existing workflow concurrency remains in
  place, but a separate pre-dependency execution guard provides the actual
  same-day idempotency decision.
- The guard uses the `Asia/Shanghai` date, current workflow/branch/run identity,
  same-day Actions states, and the run-matched `screening-results` manifest.
  Older active runs, earlier successful runs, and valid published screening
  results all cause a safe exit before dependency installation, market-data
  collection, or deep analysis.
- A `partial_success` result blocks a full rerun only when the screening itself
  succeeded, the screening JSON exists, the referenced Actions run matches,
  and the only integrity gap is incomplete deep analysis. Invalid or missing
  screening output remains eligible for a fallback attempt.
- Scheduled runs record `schedule_primary` or `schedule_fallback`; manual runs
  record `workflow_dispatch_manual`. The execution record and final manifest
  include the scheduled slot, actual run creation and screening-start times,
  both delay measurements, skip status/reason, and the run identity that caused
  a skip. No token or secret is written to these records.
- Completed no-op runs upload their execution record. The existing reader's
  eight state classifications are unchanged; it only resolves a completed
  idempotent no-op to the original valid run so a later fallback cannot hide a
  usable earlier result.
- Local verification: 70 guard/manifest/reader/history-reliability/V2.1 tests
  passed. Python compilation, workflow YAML parsing, full flake8 on changed
  Python files, and `git diff --check` passed. A fixed-time duplicate-run
  simulation returned `should_run=false`, referenced the existing valid run,
  and made zero market-fetch and deep-analysis calls.
- Draft PR [#10](https://github.com/qiqimimi1002/daily_stock_analysis/pull/10)
  was opened from `agent/schedule-fallback-idempotency`; the implementation
  commit is `d50c041d3a05fb4ca7b1f3c939c97d732e31be93`.
- Full GitHub CI run
  [#31356692920](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/31356692920)
  passed: change detection, AI governance, backend-gate with the complete
  offline suite, Docker build, Docker smoke, and Docker import checks all
  succeeded; unrelated desktop and web jobs were correctly skipped.
- Delivery status: PR #10 passed final acceptance, was marked Ready, and was
  squash-merged into `main` as
  `440b788d8d922818e4727b0f5a4fe38be6591e7a`. The source branch is retained.
  Draft PR #6 remains unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120`.

### PR #10 final acceptance and production validation

- Final acceptance fixed one backward-compatibility edge: a schema <= 1.2
  manifest without `branch` can block a duplicate only when its Actions run
  still matches the same trade date, branch, run ID, and run number. A manifest
  that explicitly names another branch remains ineligible.
- The final PR head was
  `c42b21a9779d631a38104a6ed1c197f18e8e2d25`. Local verification finished with
  72 focused guard/manifest/reader/history/V2.1 tests passing, plus Python
  compilation, critical flake8, YAML parsing, and diff checks.
- Final CI run
  [#31358134944](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/31358134944)
  passed. Change detection, AI governance, backend-gate (11m07s), Docker build,
  Docker smoke, and Docker import checks succeeded; unrelated desktop/web jobs
  were skipped.
- First post-merge `main` production validation: run number 20 / ID
  `31358801924`, 2026-08-10 13:30:21 to 13:37:31 Asia/Shanghai. It completed
  successfully with five candidates, all three requested deep analyses, 60/60
  history coverage, `history_data_quality=ok`, and `integrity.ok=true`.
  Artifact `market-screening-20` has ID `9051702070`; the fixed entry published
  schema 1.3 and the reader returned Run 20 as `success`.
- Same-day duplicate validation: manual run number 21 / ID `31359249085`
  completed in 22 seconds. It executed the pre-dependency guard, recorded
  `existing_valid_screening_result` referencing Run 20, and skipped Python
  setup, dependency installation, market screening, candidate loading, deep
  analysis, manifest rebuilding, and fixed-entry publication. Its guard-only
  Artifact `market-screening-21` has ID `9051730244`.
- After Run 21, the reader still resolved the effective result to Run 20 and
  returned `success` with reason `idempotent_run_skipped`; no duplicate market
  fetch or deep analysis occurred. The 2026-08-07 fixed entry did not block the
  first 2026-08-10 run.
- Remaining acceptance work is operational observation of the 09:40, 09:55,
  and 10:10 schedule slots for 1-3 trading days. Do not change scoring or merge
  PR #6 during this observation; the next development phase is historical-data
  coverage work only after schedule reliability is accepted.

### PR #10 operational observation — 2026-08-11 (day 1/3)

- At 11:07 Asia/Shanghai, the Actions API contained no 2026-08-11 run for any
  of the 09:40 primary, 09:55 fallback, or 10:10 fallback slots. There was no
  run ID, Artifact, current-day candidate list, or deep-analysis result.
- The reader returned `not_started` with `no_run_for_trade_date`. It rejected
  `screening-results/latest/manifest.json` because that fixed entry still
  belongs to 2026-08-10 Run 20; no previous-day result was presented as the
  current trade day's result.
- No duplicate production occurred on 2026-08-11 because no run had been
  created by the observation time. Therefore there is no current-day effective
  run, candidate count, deep-analysis status, history coverage, or integrity
  result to accept.
- The most recent scheduled run was 2026-08-10 Run 22 / ID `31360573968`.
  Its guard Artifact identifies the 09:40 primary slot, created at 14:03:16
  Asia/Shanghai after a 263.27-minute scheduler delay. It safely skipped with
  `existing_valid_screening_result`, referencing Run 20, before dependencies,
  market fetch, or deep analysis.
- Observation conclusion: idempotency and stale-result rejection continue to
  work, but GitHub's shared scheduler did not provide a usable 09:40/09:55/10:10
  trigger by 11:07. This is a P1 external scheduling reliability risk, not a
  V2.1 scoring or guard correctness failure. Do not change scope during the
  remaining observations; if the pattern repeats, the next reliability action
  is an independent external `workflow_dispatch` scheduler.
- Draft PR #6 remains open and unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120`.

#### Late-arriving final result for the same trade date

- The 11:07 observation above remains valid: no scheduled run existed by that
  cutoff. The 09:40 `schedule_primary` event eventually created Run 23 / ID
  `31462135915` at 13:35:12 Asia/Shanghai, 235.20 minutes after its scheduled
  time. Actual screening began at 13:36:08, for a 236.13-minute start delay.
- Run 23 completed successfully at 13:42:49. The manifest records 5,541
  full-market rows, 60/60 successful histories, `history_success_rate=100.0`,
  and `history_data_quality=ok` with high confidence.
- Screening produced four candidates (`601991`, `002532`, `601600`, and
  `000933`). All three requested deep analyses completed, with no missing code,
  Gemini retry, or deep-analysis failure.
- Artifact `market-screening-23` (ID `9090309651`) uploaded successfully.
  Manifest schema 1.3 reports `status=success`, `integrity.ok=true`, and no
  integrity errors. A direct read of `screening-results/latest/manifest.json`
  confirmed that the fixed entry now identifies trade date 2026-08-11, Run 23
  / ID `31462135915`.
- The same delayed scheduler burst also created Run 24 / ID `31462366828` at
  13:39:26; it was cancelled while pending under workflow concurrency and ran
  no job. Run 25 / ID `31462441430`, the 10:10 `schedule_fallback`, was created
  at 13:40:44 after a 210.73-minute delay. Its pre-dependency guard recorded
  `existing_valid_screening_result` and referenced Run 23, so Python setup,
  dependency installation, market fetch, candidate loading, deep analysis,
  manifest rebuilding, and fixed-entry publication were all skipped.
- Final day-1 conclusion: the screening, history-data, deep-analysis,
  publication, integrity, reader fallback, concurrency, and idempotency paths
  behaved correctly once GitHub delivered the events. The principal remaining
  risk is severe GitHub scheduler delay: all three planned times had produced
  no run by 11:07, and the 09:40 primary arrived only at 13:35. Keep the
  2026-08-12 and 2026-08-13 observation plan unchanged; do not alter the three
  schedules or add an external scheduler during this acceptance window.

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
- PR [#9](https://github.com/qiqimimi1002/daily_stock_analysis/pull/9)
  was opened from `agent/history-coverage-reliability`, passed production-readiness
  acceptance, was marked Ready, and was squash-merged into `main` on 2026-08-07.
  The squash commit is `b2b89c6ceb2cdb00d108d23849e2c4b1fd54663d`;
  the source branch was retained for traceability.
  The first normalized PR head was
  `f728f50f47959414d59d302e82dcdbcbe8ed1ec0`; all eight remote file blob SHAs
  matched the locally tested commit, and the final diff contained only the
  expected eight files without line-ending noise.
- GitHub CI run
  [#31162832930](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/31162832930)
  passed on that PR head: change detection, AI governance, backend-gate,
  Docker build, Docker smoke, and Docker import checks succeeded; unrelated
  desktop and web jobs were correctly skipped.
- Pre-merge acceptance reran all 54 focused tests successfully. An additional
  fixed-clock boundary check confirmed that a Friday cache entry is not reused
  on the following Monday: the history window end changed from `20260807` to
  `20260810`, both providers were called again, and both requests reported
  `cache_hit=false`.
- A three-symbol live probe (`600519`, `000001`, `002241`) observed Eastmoney
  remote disconnects on both bounded attempts for every symbol. Sina then
  succeeded on its first attempt for all three, returning 83 valid rows each;
  the same-family efinance fallback was correctly skipped after the Eastmoney
  outage. No source conflict was fabricated.
- The single post-merge equivalent-production run used the production-scale
  60-symbol history preselection and eight history workers. It completed
  normally with 5,538 market rows, 60 preselected symbols, 43 history successes,
  17 failures, and 71.67% coverage. Eastmoney succeeded for 2/60 symbols while
  Sina succeeded for 42/60; the structured failure summary recorded 16
  `all_sources_failed` and one `provider_error` result. Cross-source diagnostics
  recorded one match, 42 single-backend results, 17 not-checked results, and no
  conflicts.
- The post-merge run correctly downgraded data quality to `degraded` / medium
  confidence, retained five candidates, and did not guess missing values or
  relax V2.1 filters. This is a remaining P1 external-source availability risk
  for Monday, not a code-integrity or merge blocker; the 09:40 run must be
  monitored through its coverage and failure-reason fields.
- Delivery status: PR #9 is merged and the one allowed post-merge production
  validation is complete. No further feature work is authorized before next
  week; Draft PR #6 remains unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120`.

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

# External scheduler fallback (2026-08-11)

## Design and scope

- Status: implemented on `agent/cloudflare-external-scheduler`; deployment is
  intentionally pending repository-owner Cloudflare credentials and approval.
- Cloudflare Workers Cron triggers the existing
  `.github/workflows/01-market-screening.yml` by `workflow_dispatch` at
  `02:00 UTC`, which is `10:00 Asia/Shanghai` on weekdays.
- The Worker contains no market-data, scoring, screening, reporting, or deep
  analysis logic. PR #10's pre-dependency guard remains the sole authority for
  same-day run/skip decisions.
- External invocations set
  `trigger_source=external_scheduler_cloudflare`; ordinary manual dispatches
  remain `workflow_dispatch_manual`. The execution context and manifest can
  therefore identify which path created the effective run without changing the
  reader state model.
- Existing GitHub schedules at 09:40, 09:55, and 10:10 Beijing time are
  unchanged. V2.1 scoring/filtering, reports, research, and PR #6 are unchanged.

## Security and deployment

- Use a fine-grained GitHub personal access token restricted to
  `qiqimimi1002/daily_stock_analysis` with repository permission
  `Actions: Read and write`; do not grant Contents write.
- Store the token only as the Cloudflare Worker secret `GITHUB_TOKEN` using
  `wrangler secret put`. It is absent from source, Wrangler variables, test
  output, Worker logs, workflow inputs, manifests, and Artifacts.
- Deploy from `external_scheduler/cloudflare` with `npx wrangler deploy` only
  after configuring the secret. Creating or merging the PR does not deploy it.

## Verification evidence

- Cloudflare Worker tests: 5 passed, including dispatch payload, HTTP failure,
  missing-secret handling, scheduled-log redaction, and preservation of all
  four configured cron slots.
- Screening guard unit tests: 19 passed, including the two external-trigger
  paths: an existing same-day valid result performs zero market/deep-analysis
  work, while no same-day result allows the normal production path.
- Focused production-chain regression: 75 passed across guard, manifest,
  reader, history reliability, scoring, and screener suites.
- Python compilation, YAML parsing, critical flake8 checks, and
  `git diff --check`: passed.

## Risks and production acceptance

- Cloudflare Cron is independent of GitHub's schedule delivery, but dispatch
  still depends on the GitHub API and GitHub Actions runner availability.
- The weekday cron does not know mainland-China exchange holidays; with
  `force_run=false`, the existing production workflow remains responsible for
  trading-day handling.
- Token expiry/rotation and Cloudflare Cron propagation must be monitored.
- After deployment, validate one day with no existing result
  (`should_run=true`) and one day with an existing valid result
  (`idempotency_skipped=true`). Confirm the external run skips dependency
  installation, market fetch, and deep analysis in the latter case.
- Confirm Cloudflare recorded the 10:00 invocation independently even if the
  GitHub 09:40/09:55/10:10 schedule events were delayed.

# Cloudflare scheduler production rollout (2026-08-11)

## Merge and deployment

- PR #11 was moved from Draft to Ready after its final semantic diff audit and
  successful CI, then squash-merged into `main` as
  `c633f1582d1183c4f27272ef9f6988f738ffb910`. The source branch was retained.
- Final PR-head CI passed on normalized LF content: main CI run `31472203994`
  passed Change Detection, AI governance, backend-gate, and Docker; external
  scheduler CI run `31472204028` passed all five Worker tests.
- The Worker was deployed to Cloudflare account
  `a52e64dfee6cbace4ae1b3c7820b9189` as
  `daily-stock-screening-scheduler`, version
  `6ab2989c-cbc8-4e1d-bed2-9ae10cebcde7`.
- The production trigger is `0 2 * * MON-FRI` (10:00 Asia/Shanghai). The Worker
  has `workers_dev = false`, no active HTTP route, and only a scheduled handler.
- Cloudflare Secret inventory exposes only the binding name `GITHUB_TOKEN`; the
  value was entered through Wrangler's masked prompt and never written to the
  repository, command line, logs, manifest, or Artifact.
- The fine-grained GitHub token was configured by the repository owner for only
  `qiqimimi1002/daily_stock_analysis`, with `Actions: Read and write` and no
  `Contents: write`. GitHub accepted the resulting workflow-dispatch request.
- Cloudflare does not use local GitHub CLI credentials. A separate CLI OAuth
  login was used only to publish this rollout record and is never copied into
  the Worker, its secrets, logs, manifest, or Artifacts.

## Same-day safe integration evidence

- Because the real 10:00 Cloudflare Cron time had already passed, the official
  Wrangler local scheduled endpoint was used as an equivalent handler test. It
  returned HTTP 200 and logged `github_workflow_dispatch`, `outcome=accepted`,
  `trigger_source=external_scheduler_cloudflare`, cron `0 2 * * MON-FRI`, and
  scheduled time `2026-08-11T08:56:26.211Z`. This is not represented as a
  production Cloudflare Cron Event; the first real event remains due on Aug 12.
- The handler created GitHub workflow-dispatch Run #26 / ID `31475449822` on
  `main` commit `c633f1582d1183c4f27272ef9f6988f738ffb910`; it completed success
  in approximately 18 seconds.
- Guard Artifact `market-screening-26` / ID `9095027489` recorded:
  `trade_date=2026-08-11`, `trigger_source=external_scheduler_cloudflare`,
  `idempotency_skipped=true`, `skip_reason=existing_valid_screening_result`,
  `existing_run_id=31462135915`, `existing_run_number=23`, and
  `should_run=false`.
- Run #26 skipped Python setup, dependency installation, screening start,
  full-market collection, candidate loading, deep analysis, manifest build, and
  fixed-result publication. Only checkout, guard evaluation, and the guard
  Artifact upload ran.
- The merged reader continued to resolve the original effective Run #23 / ID
  `31462135915`: status `success`, four candidates, deep analysis `completed`,
  history success rate 100%, data-quality status `ok`, and both fixed entry and
  dynamic Artifact available.

## Aug 12 production acceptance gate

- The first real independent Cloudflare Cron Event is expected at 10:00
  Asia/Shanghai on 2026-08-12. Correlate the Cloudflare event/log timestamp with
  the resulting GitHub `workflow_dispatch` Run and its guard Artifact.
- If a native 09:40 run has already produced a valid same-day result, the
  external Run must skip before Python/dependencies/market/deep analysis and the
  reader must continue to resolve the native effective Run.
- If no valid native result exists, the external Run must execute the complete
  screening pipeline and publish same-day Artifact/fixed-entry results.
- Acceptance requires `trigger_source=external_scheduler_cloudflare`, the
  correct same-day trade date, no prior-day substitution, and no duplicate full
  production. A local scheduled test is insufficient evidence for this gate.
- PR #6 remains Draft at `50c995dc10765bb0bb822212663b7cd1b4c35120` and was not
  modified, reviewed, or merged during this rollout.

# Cloudflare 10:05 second fallback (2026-08-13)

## Scope and design

- Baseline: latest `main` commit
  `7cd28bbb811ef0f75e04ed2dd5a6cd915682c1ef`.
- Development branch: `agent/cloudflare-scheduler-1005-fallback`, commit
  `61a9ae0a77bc5822a45d511cc16e0ca0a355ade8`, published as Draft PR #13.
  This change extends the existing `external_scheduler/cloudflare` Worker; it
  does not create a second Worker or duplicate any screening logic.
- The existing weekday 10:00 Asia/Shanghai trigger remains unchanged. A second
  weekday trigger at 10:05 was added using Cloudflare UTC crons
  `0 2 * * MON-FRI` and `5 2 * * MON-FRI`.
- Both times send the existing GitHub workflow-dispatch payload:
  `trigger_source=external_scheduler_cloudflare`, `top_n=5`,
  `run_deep_analysis=true`, and `force_run=false`.
- PR #10's pre-dependency same-day guard remains the only idempotency
  authority. The Worker intentionally performs no result lookup or duplicate
  decision. If 10:00 already produced a valid current-day result, the 10:05
  GitHub Run must exit at the existing guard before Python setup, dependency
  installation, market fetch, candidate loading, or deep analysis.
- The GitHub-native 09:40, 09:55, and 10:10 schedules are unchanged. V2.1
  scoring/filtering, the reader, formal reports, research modules, and Draft
  PR #6 are outside this branch and unchanged.

## Security and observability

- `GITHUB_TOKEN` remains a Cloudflare Secret. No token value, Authorization
  header, GitHub response body, workflow input, manifest field, or Artifact is
  added by this change.
- The required fine-grained token remains restricted to
  `qiqimimi1002/daily_stock_analysis` with `Actions: write` and metadata read;
  no `Contents: write` permission is required.
- Persistent Worker observability is enabled. Success logs contain the
  scheduled time, cron, external trigger source, and GitHub HTTP status.
  Failure logs contain only a sanitized error type and optional HTTP status;
  configuration, network, GitHub HTTP, and unexpected Worker errors are
  distinguished without logging raw exception text or response content.

## Local verification

- Worker syntax check passed; all 8 Worker tests passed. Coverage includes the
  exact dispatch payload, both 10:00/10:05 scheduled handlers, 403 handling,
  network-failure redaction, missing-secret rejection, structured logs, and
  preservation of the three existing GitHub schedules.
- The original idempotency guard suite passed all 19 tests. The affected guard,
  manifest, reader, V2.1 scoring, and market-screener regression selection
  passed all 67 tests.
- Wrangler 4.120.1 `deploy --dry-run` parsed the updated config and bundled the
  Worker successfully without deploying. Workflow YAML parsing and
  `git diff --check` passed.
- The Windows checkout cannot pass the repository's AI-assets symlink check
  because `CLAUDE.md` is materialized instead of a symlink. This is an existing
  local checkout limitation; pull-request CI on Linux remains the authoritative
  full check.
- Draft PR #13 CI completed successfully: External scheduler CI Run
  `31670322778` passed, and full CI Run `31670322784` passed all applicable
  jobs. Desktop and Web jobs were skipped because this branch has no matching
  changes.
- Local mocked scheduled-event verification is complete. A real manual
  dispatch/guard Run and the first actual Cloudflare 10:00/10:05 Cron evidence
  remain production acceptance gates; they must not be represented as complete
  until their GitHub Run and Cloudflare event logs are available.
- Draft PR #6 remains unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120`.

## PR #13 final merge and production deployment (2026-08-13)

- Final manual acceptance found no P0/P1/P2 blocker. The final PR head
  `b3ed6702fccf4672cbea6736c77d13bbd5b3304a` changed only the five approved
  files: this status document and the existing Cloudflare Worker's README,
  source, tests, and Wrangler configuration.
- Latest PR-head CI was fully successful: main CI Run `31671857756` passed all
  applicable checks, and External scheduler CI Run `31671857785` passed all
  eight Worker tests plus all 19 existing idempotency-guard tests. Unrelated
  desktop and web jobs were correctly skipped.
- PR #13 was moved from Draft to Ready and squash-merged into `main` as
  `eff7f51209a3571381778523ad526ab72e16077c`. The source branch was retained
  for traceability.
- The existing Worker `daily-stock-screening-scheduler` was updated in place in
  Cloudflare account `a52e64dfee6cbace4ae1b3c7820b9189`; no second Worker or
  HTTP route was created. The active deployment is version
  `5a8b9298-62b9-4fb9-a35d-30dec63d621d` at 100% traffic.
- Deployment installed exactly two weekday Cron Triggers: `0 2 * * MON-FRI`
  and `5 2 * * MON-FRI`, corresponding to 10:00 and 10:05 Asia/Shanghai.
  Wrangler's post-deployment inventory still exposes only the Secret binding
  name `GITHUB_TOKEN`; its value was neither read nor rewritten during this
  deployment.
- Both Cron events use the existing dispatch payload and PR #10's
  pre-dependency same-day guard remains the sole duplicate-production
  authority. No V2.1 scoring/filter, reader, report, research, or production
  workflow logic changed.
- Real Cloudflare Cron evidence remains pending for the next business day. The
  10:00 and 10:05 events must be correlated with separate GitHub
  `workflow_dispatch` Runs, structured Worker logs, and guard Artifacts. A
  local/manual simulation must not be substituted for these production events.
- Acceptance rule: if 10:00 starts the valid full screening, the 10:05 Run must
  stop before Python setup, dependency installation, market fetch, candidate
  loading, and deep analysis. If 10:00 dispatch fails, 10:05 must be able to
  start the full workflow. By 10:20 the reader must resolve the current-day
  effective Run; the manifest, Artifact, and logs must contain no token value.
- Draft PR #6 remains open, Draft, unmerged, and unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120`.

## Cloudflare dual-Cron production acceptance (2026-08-14): PASSED

- This was a read-only production acceptance before the status-only update. No
  production code, Worker configuration, Cron, Secret, V2.1 scoring, reader,
  idempotency guard, research code, formal report, or PR #6 was changed or
  manually triggered during validation.
- The existing Cloudflare Worker `daily-stock-screening-scheduler` produced
  two real weekday scheduled invocations on active script version
  `6ad6a5bf-d9a8-4835-9589-f1df197ea520`:
  - 10:00 slot: `scheduled_time=2026-08-14T02:00:52.000Z`,
    Cron `0 2 * * MON-FRI`; the structured production log at
    10:00:53.722 Asia/Shanghai recorded
    `trigger_source=external_scheduler_cloudflare`,
    `github_http_status=200`, and `outcome=accepted`.
  - 10:05 slot: `scheduled_time=2026-08-14T02:05:14.000Z`,
    Cron `5 2 * * MON-FRI`; the scheduled event at
    10:05:15.930 Asia/Shanghai completed with `outcome=ok`, and its
    structured log recorded `github_http_status=200` and
    `outcome=accepted`.
  Cloudflare observability showed four successful event/log records and zero
  errors for this validation window. The GitHub Runs below independently prove
  that both accepted dispatches created Actions Runs.
- The 10:00 dispatch created Run #34 / ID `31762409073` at
  10:00:53 Asia/Shanghai. Its guard recorded `should_run=true` and
  `idempotency_skipped=false`; Python setup, dependency installation, market
  fetch, screening, candidate loading, deep analysis, manifest generation,
  Artifact upload, and fixed-entry publication all executed successfully.
- The 10:05 dispatch created Run #35 / ID `31762646303` at
  10:05:15 Asia/Shanghai. After concurrency released it, the guard completed at
  10:08:48 with `should_run=false`,
  `idempotency_skipped=true`,
  `skip_reason=existing_valid_screening_result`, and references to Run #34.
  Python setup, dependency installation, market fetch, screening, candidate
  loading, deep analysis, manifest generation, and publication were all
  skipped. Therefore exactly one Run performed production work.
- Run #34 uploaded `market-screening-34` (Artifact ID `9205218119`).
  Its current-day fixed manifest records 5,542 market rows, 60/60 successful
  history series (`history_success_rate=100`,
  `history_data_quality=ok/high`), four candidates
  (`600522`, `600487`, `002185`, `600176`), three completed deep
  analyses, `status=success`, and `integrity.ok=true`.
  Run #35 uploaded only the 912-byte guard Artifact
  `market-screening-35` (Artifact ID `9205221204`).
- With authenticated Actions-Artifact access, the reader resolves the skipped
  Run #35 back to effective Run #34 and returns `status=success`,
  `candidate_count=4`, `history_success_rate=100`,
  `deep_analysis_status=completed`, and `fixed_entry_valid=true`.
  No previous-day result was accepted as the current-day result.
- Sensitive-value scans found no GitHub PAT, bearer Authorization value, or
  token plaintext in either Artifact. Worker structured logs contained only
  the scheduled time, Cron, trigger source, HTTP status, repository/workflow
  metadata, and sanitized outcome; GitHub job logs exposed only masked
  credentials.
- **Final verdict: PASSED.** Both independent real Cron slots created the
  expected dispatch Runs, the 10:00 Run produced the valid current-day result,
  and the 10:05 Run stopped before all expensive production steps. The external
  startup fallback problem is therefore recorded as formally resolved as of
  this production acceptance.
- Draft PR #6 remains open, Draft, unmerged, and unchanged at
  `50c995dc10765bb0bb822212663b7cd1b4c35120`.
