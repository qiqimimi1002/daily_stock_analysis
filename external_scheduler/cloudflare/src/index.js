const GITHUB_API_VERSION = "2026-03-10";
const TRIGGER_SOURCE = "external_scheduler_cloudflare";

class WorkflowDispatchError extends Error {
  constructor(errorType, message, httpStatus = null) {
    super(message);
    this.name = "WorkflowDispatchError";
    this.errorType = errorType;
    this.httpStatus = httpStatus;
  }
}

function required(env, name) {
  const value = env[name]?.trim();
  if (!value) {
    throw new WorkflowDispatchError(
      "configuration_error",
      `${name} is required`,
    );
  }
  return value;
}

function safeDispatchError(error) {
  if (error instanceof WorkflowDispatchError) {
    return error;
  }
  return new WorkflowDispatchError(
    "worker_error",
    "Worker dispatch failed",
  );
}

export async function dispatchWorkflow(env, fetchImpl = globalThis.fetch) {
  const token = required(env, "GITHUB_TOKEN");
  const owner = required(env, "GITHUB_OWNER");
  const repository = required(env, "GITHUB_REPO");
  const workflow = required(env, "GITHUB_WORKFLOW");
  const ref = required(env, "GITHUB_REF");
  const url = `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repository)}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;

  let response;
  try {
    response = await fetchImpl(url, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "daily-stock-cloudflare-scheduler",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
      },
      body: JSON.stringify({
        ref,
        inputs: {
          trigger_source: TRIGGER_SOURCE,
          top_n: "5",
          run_deep_analysis: "true",
          force_run: "false",
        },
      }),
    });
  } catch {
    throw new WorkflowDispatchError(
      "github_network_error",
      "GitHub workflow dispatch request failed",
    );
  }

  if (!response.ok) {
    throw new WorkflowDispatchError(
      "github_http_error",
      `GitHub workflow dispatch failed with HTTP ${response.status}`,
      response.status,
    );
  }

  return {
    event: "github_workflow_dispatch",
    outcome: "accepted",
    status: response.status,
    github_http_status: response.status,
    repository: `${owner}/${repository}`,
    workflow,
    ref,
    trigger_source: TRIGGER_SOURCE,
  };
}

export default {
  async scheduled(controller, env, ctx) {
    const task = dispatchWorkflow(env)
      .then((result) => {
        console.log({
          ...result,
          cron: controller.cron,
          scheduled_time: new Date(controller.scheduledTime).toISOString(),
        });
      })
      .catch((error) => {
        const safeError = safeDispatchError(error);
        console.error({
          event: "github_workflow_dispatch",
          outcome: "failed",
          error_type: safeError.errorType,
          github_http_status: safeError.httpStatus,
          trigger_source: TRIGGER_SOURCE,
          cron: controller.cron,
          scheduled_time: new Date(controller.scheduledTime).toISOString(),
        });
        throw safeError;
      });
    ctx.waitUntil(task);
  },
};
