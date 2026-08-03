# Project Status

> Last updated: 2026-08-03 (Asia/Shanghai)
>
> Codex workflow rule: read this file before substantial project work and
> update it after every completed material task. Complete safe in-scope work
> directly instead of asking the user to edit files step by step.

## Current state

- Repository: `qiqimimi1002/daily_stock_analysis`
- Stable branch: `main`
- V2.1 pull request: [#4](https://github.com/qiqimimi1002/daily_stock_analysis/pull/4)
- Status: merged on 2026-08-03
- Squash merge commit: `c56623f19256fe7b633aee579d7fceda31bd8659`
- The former feature branch `feat/v2-1-scoring` is no longer the active work
  target. New work must start from the current `main` branch.
- Do not use or merge the abandoned temporary branch `v2-1-market-scoring`.

## Delivered

- V2.1 transparent 100-point observation model:
  - fundamentals: 30
  - industry catalysts: 20
  - capital/flow: 20
  - technicals: 20
  - valuation: 10
- Shanghai/Shenzhen main-board scope; excludes ChiNext, STAR Market, Beijing
  Stock Exchange, ST, and `*ST` securities.
- Liquidity, loss, risk-announcement, regulatory, abnormal-move, and evidence
  coverage controls.
- Market-environment score, weak-market candidate reduction, watch zones,
  trigger conditions, abandonment conditions, and risk warnings.
- Direct Sina full-market fallback that preserves turnover, PE, and PB fields.
- Explicit pre-open/unavailable snapshot handling; a zero-activity snapshot is
  not interpreted as a weak market or absence of opportunities.
- Shared evidence-aware report policy across Markdown, notification, template,
  and history output:
  - non-buy decisions remove buy/add/build/accumulate wording;
  - incomplete evidence hides attribution percentages;
  - independently meaningful signals such as `MACD金叉` remain visible;
  - missing-chip and volume-only directional claims are suppressed;
  - all-zero attribution payloads do not render an empty section;
  - market prices and percentage arithmetic use one consistent quote;
  - report timestamps use Asia/Shanghai.

## Verification evidence

- Successful live full-market workflow run #12:
  https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30781220470
- Final PR CI run #30:
  https://github.com/qiqimimi1002/daily_stock_analysis/actions/runs/30782881564
- CI result: 5,006 passed, 4 deselected, 48 warnings, and 490 subtests passed.
- CI also passed Python syntax, flake8 critical checks, deterministic checks,
  Docker build/smoke tests, change detection, and AI governance.
- Focused local policy suite: 15 passed.
- Local Python compilation, flake8 critical checks, and `git diff --check`:
  passed.

## Remaining risks

- Model output is an observation aid, not a buy recommendation.
- News, announcement, capital-flow, chip, or fundamental data that were not
  successfully retrieved must remain marked as unverified.
- Live run #12 covered the full integration path before the final deterministic
  report guards. The guards are fully regression-tested, but the next scheduled
  `main` workflow remains the first post-merge production confirmation.

## Next actions

1. Observe the next scheduled `main` full-market workflow and review its
   artifact only if it fails or produces evidence-policy regressions.
2. Do not reopen V2.1 unless that production run reveals a concrete defect.
3. Start V2.2 history/backtest tracking as a separate branch and pull request.
