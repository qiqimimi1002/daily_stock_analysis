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
  assert.equal(result.github_http_status, 204);
  assert.equal(JSON.stringify(result).includes(TOKEN), false);
});

test("HTTP failures expose only the status and sanitized error type", async () => {
  let responseBodyRead = false;
  await assert.rejects(
    dispatchWorkflow(ENV, async () => ({
      ok: false,
      status: 403,
      text: async () => {
        responseBodyRead = true;
        return `response containing ${TOKEN}`;
      },
    })),
    (error) => {
      assert.match(error.message, /HTTP 403/);
      assert.equal(error.errorType, "github_http_error");
      assert.equal(error.httpStatus, 403);
      assert.equal(error.message.includes(TOKEN), false);
      return true;
    },
  );
  assert.equal(responseBodyRead, false);
});

test("network failures discard sensitive underlying error text", async () => {
  await assert.rejects(
    dispatchWorkflow(ENV, async () => {
      throw new Error(`network failure with ${TOKEN}`);
    }),
    (error) => {
      assert.equal(error.errorType, "github_network_error");
      assert.equal(error.httpStatus, null);
      assert.equal(error.message.includes(TOKEN), false);
      return true;
    },
  );
});

test("both scheduled fallbacks log accepted dispatches without secrets", async () => {
  const originalFetch = globalThis.fetch;
  const originalLog = console.log;
  const logEntries = [];
  const pending = [];
  globalThis.fetch = async () => ({ ok: true, status: 204 });
  console.log = (value) => logEntries.push(value);

  try {
    for (const [cron, minute] of [
      ["0 2 * * MON-FRI", 0],
      ["5 2 * * MON-FRI", 5],
    ]) {
      await worker.scheduled(
        { cron, scheduledTime: Date.UTC(2026, 7, 12, 2, minute) },
        ENV,
        { waitUntil: (promise) => pending.push(promise) },
      );
    }
    await Promise.all(pending);
  } finally {
    globalThis.fetch = originalFetch;
    console.log = originalLog;
  }

  assert.equal(logEntries.length, 2);
  assert.deepEqual(
    logEntries.map((entry) => entry.cron),
    ["0 2 * * MON-FRI", "5 2 * * MON-FRI"],
  );
  for (const entry of logEntries) {
    assert.equal(entry.outcome, "accepted");
    assert.equal(entry.github_http_status, 204);
    assert.equal(entry.trigger_source, "external_scheduler_cloudflare");
    assert.equal(JSON.stringify(entry).includes(TOKEN), false);
  }
});

test("scheduled failure logs are structured and sanitized", async () => {
  const originalFetch = globalThis.fetch;
  const originalError = console.error;
  const errorEntries = [];
  const pending = [];
  globalThis.fetch = async () => {
    throw new Error(`network failure with ${TOKEN}`);
  };
  console.error = (value) => errorEntries.push(value);

  try {
    await worker.scheduled(
      { cron: "5 2 * * MON-FRI", scheduledTime: Date.UTC(2026, 7, 12, 2, 5) },
      ENV,
      { waitUntil: (promise) => pending.push(promise) },
    );
    await assert.rejects(pending[0], /GitHub workflow dispatch request failed/);
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalError;
  }

  assert.equal(errorEntries.length, 1);
  assert.deepEqual(errorEntries[0], {
    event: "github_workflow_dispatch",
    outcome: "failed",
    error_type: "github_network_error",
    github_http_status: null,
    trigger_source: "external_scheduler_cloudflare",
    cron: "5 2 * * MON-FRI",
    scheduled_time: "2026-08-12T02:05:00.000Z",
  });
  assert.equal(JSON.stringify(errorEntries[0]).includes(TOKEN), false);
});

test("scheduled HTTP failure logs include the GitHub status", async () => {
  const originalFetch = globalThis.fetch;
  const originalError = console.error;
  const errorEntries = [];
  const pending = [];
  globalThis.fetch = async () => ({ ok: false, status: 403 });
  console.error = (value) => errorEntries.push(value);

  try {
    await worker.scheduled(
      { cron: "0 2 * * MON-FRI", scheduledTime: Date.UTC(2026, 7, 12, 2) },
      ENV,
      { waitUntil: (promise) => pending.push(promise) },
    );
    await assert.rejects(pending[0], /HTTP 403/);
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalError;
  }

  assert.equal(errorEntries.length, 1);
  assert.equal(errorEntries[0].error_type, "github_http_error");
  assert.equal(errorEntries[0].github_http_status, 403);
  assert.equal(errorEntries[0].trigger_source, "external_scheduler_cloudflare");
  assert.equal(JSON.stringify(errorEntries[0]).includes(TOKEN), false);
});

test("requires the token without logging or embedding a fallback", async () => {
  await assert.rejects(
    dispatchWorkflow({ ...ENV, GITHUB_TOKEN: "" }, async () => {
      throw new Error("fetch must not run");
    }),
    /GITHUB_TOKEN is required/,
  );
});

test("adds only the two Cloudflare fallbacks and preserves GitHub schedules", async () => {
  const workerRoot = new URL("../", import.meta.url);
  const repositoryRoot = new URL("../../../", import.meta.url);
  const wrangler = await readFile(new URL("wrangler.toml", workerRoot), "utf8");
  const workflow = await readFile(
    new URL(".github/workflows/01-market-screening.yml", repositoryRoot),
    "utf8",
  );

  assert.match(
    wrangler,
    /crons = \[ "0 2 \* \* MON-FRI", "5 2 \* \* MON-FRI" \]/,
  );
  assert.match(wrangler, /workers_dev = false/);
  assert.match(wrangler, /\[observability\][\s\S]*enabled = true/);
  assert.match(workflow, /cron: "40 1 \* \* 1-5"/);
  assert.match(workflow, /cron: "55 1 \* \* 1-5"/);
  assert.match(workflow, /cron: "10 2 \* \* 1-5"/);
});
