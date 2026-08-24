# Dashboard v0.1

Independent static, read-only results cockpit. It has no build step, backend,
database, desktop-client integration, provider call, or production trigger.

## Local preview

From the repository root:

```powershell
python -m http.server 4174 --bind 127.0.0.1 --directory dashboard
```

Open `http://127.0.0.1:4174/` for real Public results, or the explicitly
fictional UI fixture at
`http://127.0.0.1:4174/?fixture=dashboard-v0.1`.

## Data boundary

The adapter performs static `GET` requests only for:

- `screening-results/latest|history/<date>/manifest.json`
- `screening-results/latest|history/<date>/market_screening.json`
- `research/results/short_term_v1_vs_phase2a_2026-08-21.json` on `main`

Candidate and research records pass through explicit output allowlists. Market
snapshots, Private Universe rows, OHLCV, amount, corporate-action details and
raw evidence are not returned to the page. A failed Public read remains a
visible failure and never falls back to fixture data.

Run the dependency-free adapter and boundary tests with Node 20+:

```powershell
node --test dashboard/tests/*.test.mjs
```
