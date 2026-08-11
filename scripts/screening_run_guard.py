#!/usr/bin/env python3
"""Prevent duplicate same-day full-market screening runs.

The guard intentionally uses only the Python standard library so it can run
before the workflow installs production dependencies.
"""

from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, time
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SCHEDULE_SLOTS = {
    "40 1 * * 1-5": ("schedule_primary", "09:40"),
    "55 1 * * 1-5": ("schedule_fallback", "09:55"),
    "10 2 * * 1-5": ("schedule_fallback", "10:10"),
}
VALID_SCREENING_STATUSES = {"success", "screening_completed", "partial_success"}
ALLOWED_PARTIAL_ERRORS = {"deep_analysis_incomplete"}
WORKFLOW_DISPATCH_SOURCES = {
    "workflow_dispatch_manual",
    "external_scheduler_cloudflare",
}


def _parse_datetime(value: str, *, field: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def trigger_metadata(
    event_name: str,
    event_schedule: str,
    dispatch_source: str = "workflow_dispatch_manual",
) -> tuple[str, Optional[str]]:
    if event_name == "workflow_dispatch":
        if dispatch_source not in WORKFLOW_DISPATCH_SOURCES:
            raise ValueError(f"unknown workflow_dispatch source: {dispatch_source}")
        return dispatch_source, None
    if event_name != "schedule":
        raise ValueError(f"unsupported event_name: {event_name}")
    try:
        return SCHEDULE_SLOTS[event_schedule]
    except KeyError:
        raise ValueError(f"unknown scheduled slot: {event_schedule}") from None


def _same_shanghai_date(run: Mapping[str, Any], trade_date: str) -> bool:
    try:
        return _parse_datetime(str(run["created_at"]), field="created_at").date().isoformat() == trade_date
    except (KeyError, TypeError, ValueError):
        return False


def _matching_run(
    runs: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    run_number: str,
    branch: str,
    trade_date: str,
) -> Optional[Mapping[str, Any]]:
    for run in runs:
        if str(run.get("id")) != str(run_id):
            continue
        if str(run.get("run_number")) != str(run_number):
            continue
        if run.get("head_branch") != branch or not _same_shanghai_date(run, trade_date):
            continue
        return run
    return None


def _manifest_is_valid_screening(
    manifest: Mapping[str, Any],
    *,
    trade_date: str,
    workflow_name: str,
    branch: str,
    runs: Sequence[Mapping[str, Any]],
) -> bool:
    if manifest.get("trade_date") != trade_date:
        return False
    if manifest.get("workflow_name") != workflow_name:
        return False
    # Schema <= 1.2 omitted branch; the run identity check below still proves it.
    manifest_branch = manifest.get("branch")
    if manifest_branch not in (None, "", branch):
        return False
    if manifest.get("status") not in VALID_SCREENING_STATUSES:
        return False
    if manifest.get("screening_outcome") != "success" or not manifest.get("screening_json"):
        return False
    integrity = manifest.get("integrity")
    if not isinstance(integrity, Mapping):
        return False
    errors = {str(item) for item in integrity.get("errors", [])}
    if errors - ALLOWED_PARTIAL_ERRORS:
        return False
    matching = _matching_run(
        runs,
        run_id=str(manifest.get("run_id", "")),
        run_number=str(manifest.get("run_number", "")),
        branch=branch,
        trade_date=trade_date,
    )
    return matching is not None and matching.get("status") == "completed"


def _older_run(run: Mapping[str, Any], current_run: Mapping[str, Any]) -> bool:
    try:
        return int(run.get("run_number", 0)) < int(current_run.get("run_number", 0))
    except (TypeError, ValueError):
        return str(run.get("created_at", "")) < str(current_run.get("created_at", ""))


def decide_execution(
    *,
    now: datetime,
    event_name: str,
    event_schedule: str,
    dispatch_source: str = "workflow_dispatch_manual",
    workflow_name: str,
    branch: str,
    current_run: Mapping[str, Any],
    workflow_runs: Sequence[Mapping[str, Any]],
    fixed_manifest: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a deterministic same-day execution decision."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone offset")
    now = now.astimezone(SHANGHAI_TZ)
    trade_date = now.date().isoformat()
    trigger_source, scheduled_slot = trigger_metadata(
        event_name,
        event_schedule,
        dispatch_source,
    )
    current_id = str(current_run.get("id", ""))
    current_number = str(current_run.get("run_number", ""))
    if not current_id or not current_number:
        raise ValueError("current run identity is incomplete")
    if current_run.get("head_branch") != branch:
        raise ValueError("current run branch does not match GITHUB_REF_NAME")
    created_at = _parse_datetime(str(current_run.get("created_at", "")), field="run_created_at")

    base = {
        "schema_version": "1.0",
        "trade_date": trade_date,
        "workflow_name": workflow_name,
        "branch": branch,
        "run_id": current_id,
        "run_number": current_number,
        "trigger_source": trigger_source,
        "scheduled_slot": scheduled_slot,
        "run_created_at": created_at.isoformat(timespec="seconds"),
        "decision_at": now.isoformat(timespec="seconds"),
        "screening_started_at": None,
        "run_creation_delay_minutes": _delay_minutes(created_at, trade_date, scheduled_slot),
        "screening_start_delay_minutes": None,
        "idempotency_skipped": False,
        "skip_reason": None,
        "existing_run_id": None,
        "existing_run_number": None,
    }

    if fixed_manifest and _manifest_is_valid_screening(
        fixed_manifest,
        trade_date=trade_date,
        workflow_name=workflow_name,
        branch=branch,
        runs=workflow_runs,
    ):
        return {
            **base,
            "should_run": False,
            "idempotency_skipped": True,
            "skip_reason": "existing_valid_screening_result",
            "existing_run_id": str(fixed_manifest["run_id"]),
            "existing_run_number": str(fixed_manifest["run_number"]),
        }

    same_day_runs = [
        run
        for run in workflow_runs
        if str(run.get("id")) != current_id
        and run.get("head_branch") == branch
        and _same_shanghai_date(run, trade_date)
        and _older_run(run, current_run)
    ]
    active = next(
        (run for run in same_day_runs if run.get("status") in {"queued", "in_progress"}),
        None,
    )
    if active:
        return {
            **base,
            "should_run": False,
            "idempotency_skipped": True,
            "skip_reason": "earlier_run_active",
            "existing_run_id": str(active.get("id")),
            "existing_run_number": str(active.get("run_number")),
        }

    completed = next(
        (
            run
            for run in same_day_runs
            if run.get("status") == "completed" and run.get("conclusion") == "success"
        ),
        None,
    )
    if completed:
        return {
            **base,
            "should_run": False,
            "idempotency_skipped": True,
            "skip_reason": "earlier_successful_run",
            "existing_run_id": str(completed.get("id")),
            "existing_run_number": str(completed.get("run_number")),
        }

    return {**base, "should_run": True}


def _delay_minutes(observed: datetime, trade_date: str, scheduled_slot: Optional[str]) -> Optional[float]:
    if scheduled_slot is None:
        return None
    hour, minute = (int(part) for part in scheduled_slot.split(":"))
    scheduled = datetime.combine(date.fromisoformat(trade_date), time(hour, minute), SHANGHAI_TZ)
    return round(max(0.0, (observed - scheduled).total_seconds() / 60.0), 2)


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def json(self, url: str, *, allow_not_found: bool = False) -> Optional[dict[str, Any]]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "daily-stock-screening-guard",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            raise OSError(f"GitHub API returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise OSError(f"GitHub API request failed: {exc}") from exc

    def fixed_manifest(self, repository: str) -> Optional[dict[str, Any]]:
        encoded_repo = urllib.parse.quote(repository, safe="/")
        url = (
            f"https://api.github.com/repos/{encoded_repo}/contents/latest/manifest.json"
            "?ref=screening-results"
        )
        payload = self.json(url, allow_not_found=True)
        if payload is None:
            return None
        try:
            content = base64.b64decode(str(payload["content"])).decode("utf-8")
            manifest = json.loads(content)
        except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OSError("fixed screening manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise OSError("fixed screening manifest must be an object")
        return manifest


def mark_screening_started(context: dict[str, Any], started_at: datetime) -> dict[str, Any]:
    if not context.get("should_run") or context.get("idempotency_skipped"):
        raise ValueError("a skipped run cannot be marked as started")
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("started_at must include a timezone offset")
    started_at = started_at.astimezone(SHANGHAI_TZ)
    return {
        **context,
        "screening_started_at": started_at.isoformat(timespec="seconds"),
        "screening_start_delay_minutes": _delay_minutes(
            started_at,
            str(context["trade_date"]),
            context.get("scheduled_slot"),
        ),
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _append_log(path: Optional[Path], payload: Mapping[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        key: payload.get(key)
        for key in (
            "decision_at",
            "trade_date",
            "run_id",
            "run_number",
            "trigger_source",
            "scheduled_slot",
            "should_run",
            "idempotency_skipped",
            "skip_reason",
            "existing_run_id",
            "existing_run_number",
            "screening_started_at",
            "run_creation_delay_minutes",
            "screening_start_delay_minutes",
        )
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_github_output(path: Optional[Path], decision: Mapping[str, Any]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"should_run={'true' if decision['should_run'] else 'false'}\n")
        handle.write(f"skip_reason={decision.get('skip_reason') or ''}\n")


def _write_summary(path: Optional[Path], decision: Mapping[str, Any]) -> None:
    if path is None:
        return
    lines = [
        "## 全市场初筛执行守卫",
        "",
        f"- 触发来源：`{decision['trigger_source']}`",
        f"- 计划时点：`{decision.get('scheduled_slot') or 'manual'}`",
        f"- 是否执行：`{str(bool(decision['should_run'])).lower()}`",
        f"- 幂等跳过：`{str(bool(decision['idempotency_skipped'])).lower()}`",
    ]
    if decision.get("skip_reason"):
        lines.extend(
            [
                f"- 跳过原因：`{decision['skip_reason']}`",
                f"- 已有运行：`{decision.get('existing_run_number')}` / `{decision.get('existing_run_id')}`",
            ]
        )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _check(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "").strip()
    if not token:
        print(f"guard_error: {args.token_env} is required")
        return 2
    client = GitHubClient(token)
    encoded_repo = urllib.parse.quote(args.repository, safe="/")
    current = client.json(f"https://api.github.com/repos/{encoded_repo}/actions/runs/{args.run_id}")
    workflow = urllib.parse.quote(Path(args.workflow_file).name, safe="")
    runs_payload = client.json(
        f"https://api.github.com/repos/{encoded_repo}/actions/workflows/{workflow}/runs"
        f"?branch={urllib.parse.quote(args.branch, safe='')}&per_page=100"
    )
    if current is None or runs_payload is None:
        raise OSError("GitHub Actions run data is unavailable")
    if str(current.get("id")) != str(args.run_id):
        raise OSError("current run_id does not match GitHub Actions")
    if str(current.get("run_number")) != str(args.run_number):
        raise OSError("current run_number does not match GitHub Actions")
    runs = runs_payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise OSError("GitHub Actions run list is invalid")
    fixed_manifest = client.fixed_manifest(args.repository)
    now = _parse_datetime(args.now, field="now") if args.now else datetime.now(SHANGHAI_TZ)
    decision = decide_execution(
        now=now,
        event_name=args.event_name,
        event_schedule=args.event_schedule,
        dispatch_source=args.dispatch_source,
        workflow_name=args.workflow_name,
        branch=args.branch,
        current_run=current,
        workflow_runs=runs,
        fixed_manifest=fixed_manifest,
    )
    _write_json(args.output, decision)
    _append_log(args.log, decision)
    _write_github_output(args.github_output, decision)
    _write_summary(args.summary, decision)
    print(json.dumps(decision, ensure_ascii=False))
    return 0


def _mark_started(args: argparse.Namespace) -> int:
    context = json.loads(args.context.read_text(encoding="utf-8"))
    if not isinstance(context, dict):
        raise ValueError("execution context must be an object")
    updated = mark_screening_started(
        context,
        _parse_datetime(args.started_at, field="started_at"),
    )
    _write_json(args.context, updated)
    _append_log(args.log, updated)
    print(json.dumps(updated, ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--repository", required=True)
    check.add_argument("--workflow-file", required=True)
    check.add_argument("--workflow-name", required=True)
    check.add_argument("--branch", required=True)
    check.add_argument("--run-id", required=True)
    check.add_argument("--run-number", required=True)
    check.add_argument("--event-name", required=True)
    check.add_argument("--event-schedule", default="")
    check.add_argument("--dispatch-source", default="workflow_dispatch_manual")
    check.add_argument("--token-env", default="GITHUB_TOKEN")
    check.add_argument("--now")
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--log", type=Path)
    check.add_argument("--github-output", type=Path)
    check.add_argument("--summary", type=Path)
    check.set_defaults(handler=_check)

    started = subparsers.add_parser("mark-started")
    started.add_argument("--context", type=Path, required=True)
    started.add_argument("--started-at", required=True)
    started.add_argument("--log", type=Path)
    started.set_defaults(handler=_mark_started)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return int(args.handler(args))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"guard_error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
