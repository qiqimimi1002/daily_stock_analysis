# Project Status

> Last updated: 2026-07-30 (Asia/Shanghai)
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
- Current active-branch head: `ae9c95a`
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

## Verification

- Original V2.1 PR CI run `30515993625`: passed.
- Local `tests/test_market_scoring.py`: 7 passed.
- `python -m py_compile src/services/market_screener.py`: passed.
- A broader local test command could not collect
  `tests/test_fundamental_adapter.py` because the local Python environment lacks
  `python-dotenv`; the earlier GitHub CI covered dependency installation.
- Latest CI for fallback commit `ae9c95a`, run `30517697973`: in progress at
  the time of this update.

## Live-run evidence

- Workflow run:
  [全市场初筛 #4](https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30517458316)
- Result: failed before candidate generation.
- Root cause: both full-market spot sources failed from the GitHub runner:
  Eastmoney/AKShare disconnected and efinance returned invalid JSON.
- Fix pushed: add `ak.stock_zh_a_spot()` (Sina) as an independent fallback.

## Next actions

1. Wait for CI run `30517697973`.
2. If CI passes, run `01-market-screening.yml` again from
   `feat/v2-1-scoring` with:
   - `top_n=5`
   - deep analysis enabled
   - force run enabled
3. Inspect the generated artifact for candidate quality, evidence gaps,
   scoring arithmetic, observation language, and deep-analysis consistency.
4. Merge PR #4 only after the live run succeeds and its artifact passes review.
5. After merge, verify the scheduled main-branch run before deleting temporary
   branches.

## Safety constraints

- Do not treat model output as a buy recommendation.
- Do not convert missing evidence into positive or negative claims.
- Do not merge PR #4 while blocking CI or live screening is failing.
- Do not expose API keys, GitHub credentials, or repository secrets.
