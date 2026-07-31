# Project Status

> Last updated: 2026-07-31 (Asia/Shanghai)
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
- Current active-branch head: see PR #4 (this file is updated in the same
  changeset as the latest compatibility fix).
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

## Verification

- Original V2.1 PR CI run `30515993625`: passed.
- Local screener/scoring tests: 18 passed.
- `python -m py_compile src/services/market_screener.py`: passed.
- A broader local test command could not collect
  `tests/test_fundamental_adapter.py` because the local Python environment lacks
  `python-dotenv`; the earlier GitHub CI covered dependency installation.
- PR CI run `30528193098` for head `e3b12cd`: passed.
- PR CI run `30594863953` for head `098d4d0`: passed.

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

## Next actions

1. Push the pre-open/unavailable guard and wait for PR CI.
2. At or after 09:40 Asia/Shanghai, run `01-market-screening.yml` again from
   `feat/v2-1-scoring` with:
   - `top_n=5`
   - deep analysis enabled
   - force run enabled
3. Inspect the live-session artifact for candidate quality, evidence gaps,
   scoring arithmetic, observation language, and deep-analysis consistency.
4. Merge PR #4 only after the live run succeeds and its artifact passes review.
5. After merge, verify the scheduled main-branch run before deleting temporary
   branches.

## Safety constraints

- Do not treat model output as a buy recommendation.
- Do not convert missing evidence into positive or negative claims.
- Do not merge PR #4 while blocking CI or live screening is failing.
- Do not expose API keys, GitHub credentials, or repository secrets.
