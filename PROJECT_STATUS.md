# Project Status

> Last updated: 2026-08-03 (Asia/Shanghai)
>
> Codex workflow rule: read this file before substantial project work and
> update it after every completed material task. Keep it concise, current, and
> factual; replace stale status instead of accumulating a diary.

## Current objective

Upgrade the existing `daily_stock_analysis` full-market screener to the V2.1
observation model without replacing the existing architecture.

## Repository state

- Repository: `qiqimimi1002/daily_stock_analysis`
- Stable branch: `main`
- Active branch: `feat/v2-1-scoring`
- Pull request: [#4](https://github.com/qiqimimi1002/daily_stock_analysis/pull/4)
- PR target: `main`
- Current remote head: `5f8f5c2364711627fc48ec8b1be1d00886d208d0` before the pending
  final report-claim cleanup.
- PR is open and must not be merged until the live screening run succeeds.
- Temporary branch `v2-1-market-scoring` was created during an abandoned web
  upload attempt. Do not merge or use it.

## Implemented

- V2.1 transparent 100-point scoring model:
  - fundamentals: 30
  - industry catalysts: 20
  - capital/flow: 20
  - technicals: 20
  - valuation: 10
- Shanghai/Shenzhen main-board scope and risk filters.
- Minimum current and 20-day average turnover amount of CNY 200 million.
- Evidence coverage, confidence, evidence gaps, market environment score,
  watch zone, trigger conditions, abandonment conditions, and risk warnings.
- Weak-market candidate-count reduction.
- Missing evidence is disclosed rather than guessed.
- Added a Sina/AKShare full-market spot fallback after the Eastmoney endpoint
  failed from GitHub Actions.
- Added compatibility for Sina's `turnoverratio` field so the fallback snapshot
  can pass the canonical spot-data normalization stage.
- Replaced AKShare's lossy Sina wrapper with a direct adapter for the same
  public Sina endpoint. AKShare parses `turnoverratio` internally but removes
  it from the returned DataFrame; the direct adapter preserves turnover, PE,
  and PB without guessing missing evidence.
- Added explicit pre-open/unavailable snapshot handling:
  - market breadth uses only rows with a real price, volume, and amount;
  - a zero-activity snapshot is labelled unavailable rather than weak market;
  - zero pre-open candidates are explicitly not interpreted as no opportunity;
  - the observation limit is zero until an active quote snapshot exists.
- Added a shared report-language policy across Markdown templates,
  notifications, and history reports:
  - non-buy decisions replace action-oriented buy/add/entry wording with
    neutral observation language;
  - `立即行动` becomes `等待确认` for non-buy decisions;
  - technical superlatives and volume-ratio-only pressure claims are
    neutralized;
  - adjacent buy terms collapse to one clean neutral phrase.

## Verification

- Original V2.1 PR CI run `30515993625`: passed.
- Local screener/scoring tests: 18 passed.
- `python -m py_compile src/services/market_screener.py`: passed.
- A broader local test command could not collect
  `tests/test_fundamental_adapter.py` because the local Python environment lacks
  `python-dotenv`; the earlier GitHub CI covered dependency installation.
- PR CI run `30528193098` for head `e3b12cd`: passed.
- PR CI run `30594863953` for head `098d4d0`: passed.
- PR CI run `30596583997` for head `6ca0715d`: passed.
- Final report-language policy tests: 10 passed locally.
- `python -m py_compile` passed for `src/report_evidence_policy.py`,
  `src/notification.py`, and `src/services/history_service.py`.
- PR CI run `30778997772` reached flake8 and found one scoped defect:
  `history_service.py` used `sanitize_action_text` without importing it.
- The missing import is fixed locally; flake8 critical checks, the 10 focused
  policy tests, and Python compilation all pass after the fix.
- PR CI run `30779754174` for head `5f8f5c2`: passed, including flake8,
  deterministic checks, the full offline suite, Docker, and AI governance.
- Final shared-policy tests after the live-artifact cleanup: 11 passed locally;
  flake8 critical checks and Python compilation also pass.
- Local collection of the broader renderer/notification tests remains blocked
  by incomplete local dependencies; GitHub CI is the required full validation.

## Live-run evidence

- Workflow run #7:
  [failed turnover validation](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30594476438).
- Workflow run #8:
  [successful pre-open integration run](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30595266523).
- Run #8 proved that the direct Sina adapter can retrieve and normalize all
  5,533 market records without the previous turnover error.
- It ran at 09:04 Asia/Shanghai, before A-share trading began, so Sina correctly
  returned zero price/volume/amount activity and no securities passed the
  liquidity gate. This is not valid evidence that the market has no candidates.
- A pre-open/unavailable guard and regression test were added after reviewing
  the run #8 artifact.
- Workflow run #9:
  [successful live-session validation](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30597157051).
- Run #9 used the 09:48 active market snapshot and completed every stage:
  - 5,533 market records;
  - 60 passed the spot filters;
  - 28 had usable history and 32 failed or were insufficient;
  - 8 evidence-enrichment requests completed;
  - 5 observation candidates were produced;
  - `002241,600089,600362` completed Daily Stock deep analysis.
- Score arithmetic was internally consistent and all five candidates were
  explicitly low-confidence (38% evidence coverage), observation-only results.
- Final artifact review found residual deep-report language addressed by the
  pending report-policy patch:
  - non-buy results can still say `立即行动`;
  - confidence reasons can say `技术形态完美/极佳` or `量价配合理想`;
  - a volume-ratio-only bearish signal can infer `短期抛压`.
- A local five-file patch neutralizes those phrases in
  templates, notifications, history reports, and shared evidence policy:
  `src/report_evidence_policy.py`, `src/notification.py`,
  `src/services/history_service.py`, `templates/report_markdown.j2`, and
  `tests/test_report_evidence_policy_v4.py`.
- The focused policy suite covers the exact forbidden phrases, including
  punctuation and adjacent-term regressions.
- Workflow run #11:
  [successful final live screening](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30780303787).
- Run #11 completed full-market screening, deep analysis, and artifact upload.
  It confirmed the core fixes but exposed three residual generated phrases:
  `完美多头排列`, `新闻及公告数据近期真空`, and `暂无显著看空信号`.
- The pending cleanup neutralizes those phrases in the shared policy, sanitizes
  trend alignment and data limitations in every renderer, uses conservative
  volume wording in history reports, and hides history battle plans for
  non-buy decisions.

## Next actions

1. Push the final report-claim cleanup and wait for PR CI.
2. Run one final artifact check for the residual phrases above.
3. Merge PR #4 only after the final artifact passes review.
4. After merge, verify the scheduled main-branch run before deleting temporary
   branches.

## Safety constraints

- Do not treat model output as a buy recommendation.
- Do not convert missing evidence into positive or negative claims.
- Do not merge PR #4 while blocking CI or live screening is failing.
- Do not expose API keys, GitHub credentials, or repository secrets.
