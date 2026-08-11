const GITHUB_API_VERSION = "2026-03-10";

function required(env, name) {
  const value = env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

export async function dispatchWorkflow(env, fetchImpl = globalThis.fetch) {
  const token = required(env, "GITHUB_TOKEN");
  const owner = required(env, "GITHUB_OWNER");
  const repository = required(env, "GITHUB_REPO");
  const workflow = required(env, "GITHUB_WORKFLOW");
  const ref = required(env, "GITHUB_REF");
  const url = `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repository)}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`;

  const response = await fetchImpl(url, {
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
        trigger_source: "external_scheduler_cloudflare",
        top_n: "5",
        run_deep_analysis: "true",
        force_run: "false",
      },
    }),
  });

  if (!response.ok) {
    throw new Error(`GitHub workflow dispatch failed with HTTP ${response.status}`);
  }

  return {
    event: "github_workflow_dispatch",
    outcome: "accepted",
    status: response.status,
    repository: `${owner}/${repository}`,
    workflow,
    ref,
    trigger_source: "external_scheduler_cloudflare",
  };
}

export default {
  async scheduled(controller, env, ctx) {
    const task = dispatchWorkflow(env)
      .then((result) => {
        console.log(JSON.stringify({
          ...result,
          cron: controller.cron,
          scheduled_time: new Date(controller.scheduledTime).toISOString(),
        }));
      })
      .catch((error) => {
        console.error(JSON.stringify({
          event: "github_workflow_dispatch",
          outcome: "failed",
          error: error.message,
          cron: controller.cron,
          scheduled_time: new Date(controller.scheduledTime).toISOString(),
        }));
        throw error;
      });
    ctx.waitUntil(task);
  },
};
