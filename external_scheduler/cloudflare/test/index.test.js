import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker, { dispatchWorkflow } from "../src/index.js";

const TOKEN = "test-secret-must-not-leak";
const ENV = {
  GITHUB_TOKEN: TOKEN,
  GITHUB_OWNER: "qiqimimi1002",
  GITHUB_REPO: "daily_stock_analysis",
  GITHUB_WORKFLOW: "01-market-screening.yml",
  GITHUB_REF: "main",
};

test("dispatches the existing workflow with the external source", async () => {
  let captured;
  const result = await dispatchWorkflow(ENV, async (url, options) => {
    captured = { url, options };
    return { ok: true, status: 204 };
  });

  assert.equal(
    captured.url,
    "https://api.github.com/repos/qiqimimi1002/daily_stock_analysis/actions/workflows/01-market-screening.yml/dispatches",
  );
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.deepEqual(JSON.parse(captured.options.body), {
    ref: "main",
    inputs: {
      trigger_source: "external_scheduler_cloudflare",
      top_n: "5",
      run_deep_analysis: "true",
      force_run: "false",
    },
  });
  assert.equal(result.trigger_source, "external_scheduler_cloudflare");
  assert.equal(JSON.stringify(result).includes(TOKEN), false);
});

test("failure messages and results never expose the token", async () => {
  await assert.rejects(
    dispatchWorkflow(ENV, async () => ({ ok: false, status: 403 })),
    (error) => {
      assert.match(error.message, /HTTP 403/);
      assert.equal(error.message.includes(TOKEN), false);
      return true;
    },
  );
});

test("scheduled logs never expose the token", async () => {
  const originalFetch = globalThis.fetch;
  const originalLog = console.log;
  const logLines = [];
  const pending = [];
  globalThis.fetch = async () => ({ ok: true, status: 204 });
  console.log = (value) => logLines.push(String(value));

  try {
    await worker.scheduled(
      { cron: "0 2 * * MON-FRI", scheduledTime: Date.UTC(2026, 7, 12, 2) },
      ENV,
      { waitUntil: (promise) => pending.push(promise) },
    );
    await Promise.all(pending);
  } finally {
    globalThis.fetch = originalFetch;
    console.log = originalLog;
  }

  assert.equal(logLines.length, 1);
  assert.equal(logLines.join("\n").includes(TOKEN), false);
  assert.match(logLines[0], /external_scheduler_cloudflare/);
});

test("requires the token without logging or embedding a fallback", async () => {
  await assert.rejects(
    dispatchWorkflow({ ...ENV, GITHUB_TOKEN: "" }, async () => {
      throw new Error("fetch must not run");
    }),
    /GITHUB_TOKEN is required/,
  );
});

test("keeps the Cloudflare and GitHub schedules unchanged", async () => {
  const workerRoot = new URL("../", import.meta.url);
  const repositoryRoot = new URL("../../../", import.meta.url);
  const wrangler = await readFile(new URL("wrangler.toml", workerRoot), "utf8");
  const workflow = await readFile(
    new URL(".github/workflows/01-market-screening.yml", repositoryRoot),
    "utf8",
  );

  assert.match(wrangler, /crons = \[ "0 2 \* \* MON-FRI" \]/);
  assert.match(workflow, /cron: "40 1 \* \* 1-5"/);
  assert.match(workflow, /cron: "55 1 \* \* 1-5"/);
  assert.match(workflow, /cron: "10 2 \* \* 1-5"/);
});
