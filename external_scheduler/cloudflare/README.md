# Cloudflare external screening scheduler

This Worker provides independent 10:00 and 10:05 Asia/Shanghai fallbacks for the
existing `.github/workflows/01-market-screening.yml` workflow. It only sends a
GitHub `workflow_dispatch` request. It does not contain market-data, scoring,
screening, reporting, or deep-analysis logic.

## Why Cloudflare Workers Cron

- Its scheduler is independent from GitHub Actions schedule delivery.
- The Worker is a single dependency-free JavaScript file.
- The only secret is an encrypted GitHub token.
- PR #10 remains the authority for same-day idempotency. The Worker always
  dispatches; the workflow guard decides whether to run or exit before
  dependencies and production steps.

The configured crons are `0 2 * * MON-FRI` and `5 2 * * MON-FRI`.
Cloudflare evaluates cron in UTC, so these are 10:00 and 10:05
Asia/Shanghai on weekdays. Both invocations send the same dispatch request.
If the first invocation already produced a valid current-day run, PR #10's
existing execution guard makes the second run exit before dependency
installation, market-data collection, or deep analysis.

## GitHub token

Create a fine-grained personal access token with:

- Resource owner: `qiqimimi1002`
- Repository access: only `daily_stock_analysis`
- Repository permission: **Actions — Read and write**
- No Contents write permission
- A short expiration and a rotation reminder

The GitHub workflow-dispatch endpoint requires Actions write permission. The
token cannot push code, write the result branch, upload Artifacts, or change
repository files. Those operations remain inside the existing workflow and use
its own scoped `GITHUB_TOKEN`.

Never put the token in `wrangler.toml`, source code, `.dev.vars`, command-line
arguments, or logs. Store it as a Cloudflare secret:

```text
cd external_scheduler/cloudflare
npx wrangler secret put GITHUB_TOKEN
```

## Test and deploy

```text
cd external_scheduler/cloudflare
npm test
npx wrangler deploy
```

Cron changes can take several minutes to propagate. Do not remove the existing
09:40, 09:55, or 10:10 GitHub schedules.

The Worker emits structured logs for each invocation. Successful events record
`scheduled_time`, `cron`, `trigger_source`, and `github_http_status`. Failed
events additionally record a sanitized `error_type`; they never include the
token, authorization header, or GitHub response body. Persistent Workers Logs
are enabled so a real Cron event can be audited after it runs.

## Safe production verification

1. Deploy the Worker and confirm the 10:00 and 10:05 Cron Triggers in Cloudflare.
2. On a day with no current screening result, verify a GitHub Actions run is
   created with event `workflow_dispatch` and the execution guard records
   `trigger_source=external_scheduler_cloudflare` and `should_run=true`.
3. On a day where GitHub cron already produced a valid screening result, verify
   the external run records `idempotency_skipped=true`, references the earlier
   run ID/number, and skips Python setup, market fetch, and deep analysis.
4. Confirm Cloudflare Cron Events show both invocations even when GitHub's own
   schedule events are delayed. This proves the triggers came from an
   independent scheduler.
5. Confirm the 10:05 run exits at the existing execution guard when the 10:00
   run has already produced a valid current-day result.
6. Confirm the final manifest or guard-only Artifact contains no token value.

Deployment is intentionally manual because it requires the repository owner's
Cloudflare account and GitHub fine-grained token. Creating this code or its PR
does not deploy the Worker.
