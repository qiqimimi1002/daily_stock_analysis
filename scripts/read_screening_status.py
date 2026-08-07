#!/usr/bin/env python3
"""Read today's market-screening state from Actions, fixed results, and artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime
import io
import json
import os
from typing import Any, Mapping, Optional
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
QUEUED_STATES = {"queued", "waiting", "requested", "pending"}
FAILURE_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}


class _NoCrossHostAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward a GitHub bearer token to the signed Artifact blob URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def _int_or_value(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _run_matches_trade_date(run: Mapping[str, Any], trade_date: str) -> bool:
    created_at = str(run.get("created_at") or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.astimezone(SHANGHAI_TZ).date().isoformat() == trade_date


def select_latest_run(runs: list[Mapping[str, Any]], trade_date: str) -> Optional[Mapping[str, Any]]:
    matching = [run for run in runs if _run_matches_trade_date(run, trade_date)]
    return max(matching, key=lambda run: (str(run.get("created_at") or ""), int(run.get("id") or 0)), default=None)


def _manifest_matches_run(manifest: Mapping[str, Any], run: Mapping[str, Any], trade_date: str) -> tuple[bool, str | None]:
    if str(manifest.get("trade_date") or "") != trade_date:
        return False, "fixed_entry_stale"
    if str(manifest.get("run_id") or "") != str(run.get("id") or ""):
        return False, "manifest_run_id_mismatch"
    if str(manifest.get("run_number") or "") != str(run.get("run_number") or ""):
        return False, "manifest_run_number_mismatch"
    return True, None


def classify_screening_status(
    *,
    trade_date: str,
    run: Mapping[str, Any] | None,
    fixed_manifest: Mapping[str, Any] | None = None,
    artifact_manifest: Mapping[str, Any] | None = None,
    fixed_entry_reachable: bool = False,
    artifact_reachable: bool = False,
) -> dict[str, Any]:
    """Return one normalized status without trusting a stale fixed manifest."""
    if run is None:
        return {
            "trade_date": trade_date,
            "status": "not_started",
            "run_id": None,
            "run_number": None,
            "workflow_status": None,
            "workflow_conclusion": None,
            "workflow_branch": None,
            "screening_status": None,
            "deep_analysis_status": None,
            "fixed_entry_available": False,
            "fixed_entry_valid": False,
            "artifact_available": False,
            "candidate_count": 0,
            "data_quality_status": "unknown",
            "manifest_source": None,
            "reason_codes": ["no_run_for_trade_date"],
        }

    workflow_status = str(run.get("status") or "").lower()
    workflow_conclusion = str(run.get("conclusion") or "").lower() or None
    base = {
        "trade_date": trade_date,
        "run_id": _int_or_value(run.get("id")),
        "run_number": _int_or_value(run.get("run_number")),
        "workflow_status": workflow_status or None,
        "workflow_conclusion": workflow_conclusion,
        "workflow_branch": run.get("head_branch"),
        "screening_status": None,
        "deep_analysis_status": None,
        "fixed_entry_available": False,
        "fixed_entry_valid": False,
        "artifact_available": False,
        "candidate_count": 0,
        "data_quality_status": "unknown",
        "manifest_source": None,
        "reason_codes": [],
    }
    if workflow_status in QUEUED_STATES:
        return {**base, "status": "queued", "reason_codes": ["workflow_queued"]}
    if workflow_status == "in_progress":
        return {**base, "status": "in_progress", "reason_codes": ["workflow_in_progress"]}

    reasons: list[str] = []
    manifest: Mapping[str, Any] | None = None
    source: str | None = None
    fixed_valid = False
    if fixed_manifest is not None:
        fixed_valid, reason = _manifest_matches_run(fixed_manifest, run, trade_date)
        if fixed_valid:
            manifest, source = fixed_manifest, "fixed_entry"
        elif reason:
            reasons.append(reason)
    if manifest is None and artifact_manifest is not None:
        artifact_valid, reason = _manifest_matches_run(artifact_manifest, run, trade_date)
        if artifact_valid:
            manifest, source = artifact_manifest, "artifact"
        elif reason:
            reasons.append(f"artifact_{reason}")

    base["fixed_entry_available"] = fixed_entry_reachable
    base["fixed_entry_valid"] = fixed_valid
    base["artifact_available"] = artifact_reachable
    base["manifest_source"] = source
    if manifest is None:
        if workflow_conclusion in FAILURE_CONCLUSIONS:
            return {**base, "status": "failure", "reason_codes": reasons + ["workflow_failed_no_result"]}
        return {**base, "status": "artifact_read_failure", "reason_codes": reasons + ["no_valid_manifest"]}

    manifest_status = str(manifest.get("status") or "")
    screening_status = str(manifest.get("screening_outcome") or "")
    deep_status = str(manifest.get("deep_analysis_status") or "")
    integrity = manifest.get("integrity") if isinstance(manifest.get("integrity"), Mapping) else {}
    integrity_ok = bool(integrity.get("ok"))
    base.update({
        "screening_status": screening_status or None,
        "deep_analysis_status": deep_status or None,
        "candidate_count": int(manifest.get("candidate_count") or 0),
    })
    preselected_count = int(manifest.get("preselected_count") or 0)
    history_success_count = int(manifest.get("history_success_count") or 0)
    history_failure_count = int(manifest.get("history_failure_count") or 0)
    if preselected_count > 0 and history_success_count == 0 and history_failure_count >= preselected_count:
        base["data_quality_status"] = "degraded"
        reasons.append("history_data_all_failed")
    else:
        base["data_quality_status"] = "ok"
    manifest_reasons = manifest.get("reason_codes")
    if isinstance(manifest_reasons, list):
        reasons.extend(str(reason) for reason in manifest_reasons if reason)
    integrity_errors = integrity.get("errors")
    if isinstance(integrity_errors, list):
        reasons.extend(str(reason) for reason in integrity_errors if reason)

    if screening_status != "success" or manifest_status == "failure":
        status = "failure"
        reasons.append("screening_failed")
    elif deep_status == "not_requested" or manifest_status == "screening_completed":
        status = "screening_completed"
    elif manifest_status == "success" and integrity_ok and deep_status in {"completed", "not_required_no_candidates"}:
        status = "success"
    else:
        status = "partial_success"
        if deep_status == "incomplete":
            reasons.append("deep_analysis_incomplete")
        if not integrity_ok:
            reasons.append("integrity_incomplete")
    return {**base, "status": status, "reason_codes": list(dict.fromkeys(reasons))}


class GitHubReader:
    def __init__(self, repository: str, token: str | None = None) -> None:
        self.repository = repository
        self.token = token

    def _request(self, url: str) -> bytes:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "daily-stock-screening-reader"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        opener = urllib.request.build_opener(_NoCrossHostAuthRedirect())
        with opener.open(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return response.read()

    def json(self, url: str) -> Any:
        return json.loads(self._request(url))

    def read_fixed_manifest(self) -> Mapping[str, Any] | None:
        url = f"https://raw.githubusercontent.com/{self.repository}/screening-results/latest/manifest.json"
        try:
            payload = self.json(url)
        except (OSError, ValueError, urllib.error.HTTPError):
            return None
        return payload if isinstance(payload, Mapping) else None

    def read_artifact_manifest(self, run_id: Any) -> tuple[Mapping[str, Any] | None, bool]:
        list_url = f"https://api.github.com/repos/{self.repository}/actions/runs/{run_id}/artifacts"
        try:
            payload = self.json(list_url)
            artifacts = payload.get("artifacts", []) if isinstance(payload, Mapping) else []
            artifact = next((item for item in artifacts if str(item.get("name", "")).startswith("market-screening-")), None)
            if artifact is None:
                return None, False
            archive = self._request(str(artifact["archive_download_url"]))
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                member = next((name for name in bundle.namelist() if name.endswith("data/screening_run_manifest.json") or name == "screening_run_manifest.json"), None)
                if member is None:
                    return None, True
                parsed = json.loads(bundle.read(member))
                return (parsed if isinstance(parsed, Mapping) else None), True
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, urllib.error.HTTPError):
            return None, False


def read_status(
    repository: str,
    workflow: str,
    trade_date: str,
    token: str | None = None,
    branch: str = "main",
) -> dict[str, Any]:
    reader = GitHubReader(repository, token)
    query = urllib.parse.urlencode({"branch": branch, "per_page": 100})
    runs_url = f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?{query}"
    try:
        payload = reader.json(runs_url)
        runs = payload.get("workflow_runs", []) if isinstance(payload, Mapping) else []
    except (OSError, ValueError, urllib.error.HTTPError):
        result = classify_screening_status(trade_date=trade_date, run=None)
        result.update({
            "status": "artifact_read_failure",
            "workflow_branch": branch,
            "reason_codes": ["actions_run_lookup_failed"],
        })
        return result
    run = select_latest_run(runs, trade_date)
    if run is None:
        return classify_screening_status(trade_date=trade_date, run=None)
    status = str(run.get("status") or "").lower()
    if status in QUEUED_STATES or status == "in_progress":
        return classify_screening_status(trade_date=trade_date, run=run)
    fixed = reader.read_fixed_manifest()
    artifact, artifact_reachable = reader.read_artifact_manifest(run.get("id"))
    return classify_screening_status(
        trade_date=trade_date,
        run=run,
        fixed_manifest=fixed,
        artifact_manifest=artifact,
        fixed_entry_reachable=fixed is not None,
        artifact_reachable=artifact_reachable,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "qiqimimi1002/daily_stock_analysis"))
    parser.add_argument("--workflow", default="01-market-screening.yml")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--trade-date", default=datetime.now(SHANGHAI_TZ).date().isoformat())
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))
    args = parser.parse_args()
    result = read_status(args.repository, args.workflow, args.trade_date, args.token, args.branch)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
