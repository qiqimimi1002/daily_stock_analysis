#!/usr/bin/env python3
"""Build and validate a machine-readable market-screening run manifest."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _parse_datetime(value: str, *, field: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def _bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_files(paths: Iterable[Path], *, root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in paths if path.is_file())


def _stock_code(candidate: Mapping[str, Any]) -> Optional[str]:
    raw = candidate.get("stock_code", candidate.get("code"))
    if raw is None:
        return None
    code = str(raw).strip()
    return code if len(code) == 6 and code.isdigit() else None


def _coverage(candidates: Sequence[Mapping[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    values: list[float] = []
    for candidate in candidates:
        value = candidate.get("score_coverage_pct")
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    if not values:
        return None, None
    return round(sum(values) / len(values), 2), round(min(values), 2)


def _load_retry_events(logs_dir: Path | None) -> list[dict[str, Any]]:
    if logs_dir is None:
        return []
    path = logs_dir / "llm_retry_events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            allowed = {
                "action",
                "attempt",
                "max_attempts",
                "error_type",
                "delay_seconds",
                "key_index",
                "key_switched",
                "stock_code",
                "model",
                "recorded_at",
            }
            events.append({key: event.get(key) for key in allowed if key in event})
    return events


def build_manifest(
    *,
    repository_root: Path,
    data_dir: Path,
    reports_dir: Path,
    workflow_name: str,
    run_id: str,
    run_number: str,
    artifact_name: str,
    screening_outcome: str,
    deep_analysis_outcome: str,
    deep_analysis_requested: bool,
    started_at: datetime,
    completed_at: datetime,
    logs_dir: Path | None = None,
) -> dict[str, Any]:
    """Return a final manifest even when the screening output is incomplete."""
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("started_at must include a timezone offset")
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError("completed_at must include a timezone offset")
    started_at = started_at.astimezone(SHANGHAI_TZ)
    completed_at = completed_at.astimezone(SHANGHAI_TZ)
    if completed_at < started_at:
        raise ValueError("completed_at cannot be earlier than started_at")
    json_files = sorted(data_dir.glob("market_screening_*.json"))
    screening_json = json_files[-1] if json_files else None
    codes_path = data_dir / "screened_codes.txt"
    screening_reports = sorted(reports_dir.glob("market_screening_*.md"))
    deep_reports = sorted(reports_dir.glob("report_*.md"))
    errors: list[str] = []
    source: dict[str, Any] = {}

    if len(json_files) > 1:
        errors.append("multiple_screening_json_files")
    if screening_json is None:
        errors.append("screening_json_missing")
    else:
        try:
            loaded = json.loads(screening_json.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("root must be an object")
            source = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"screening_json_invalid:{exc}")
    if not screening_reports or any(path.stat().st_size == 0 for path in screening_reports):
        errors.append("screening_report_missing_or_empty")

    raw_candidates = source.get("candidates", [])
    if not isinstance(raw_candidates, list) or any(not isinstance(item, dict) for item in raw_candidates):
        errors.append("candidates_invalid")
        candidates: list[Mapping[str, Any]] = []
    else:
        candidates = raw_candidates
    candidate_codes = [_stock_code(candidate) for candidate in candidates]
    if any(code is None for code in candidate_codes) or len(set(candidate_codes)) != len(candidate_codes):
        errors.append("candidate_codes_invalid_or_duplicate")
    normalized_candidate_codes = [code for code in candidate_codes if code is not None]

    raw_analysis_codes = source.get("analysis_codes", [])
    if not isinstance(raw_analysis_codes, list):
        errors.append("analysis_codes_invalid")
        analysis_codes: list[str] = []
    else:
        analysis_codes = [str(code).strip() for code in raw_analysis_codes if str(code).strip()]
    if any(code not in normalized_candidate_codes for code in analysis_codes):
        errors.append("analysis_codes_not_in_candidates")

    if codes_path.is_file():
        file_codes = [code.strip() for code in codes_path.read_text(encoding="utf-8").split(",") if code.strip()]
    else:
        file_codes = []
        errors.append("screened_codes_missing")
    if file_codes != analysis_codes:
        errors.append("screened_codes_mismatch")

    deep_text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in deep_reports)
    missing_deep_codes = [code for code in analysis_codes if code not in deep_text]
    if not deep_analysis_requested:
        deep_status = "not_requested"
    elif not analysis_codes:
        deep_status = "not_required_no_candidates"
    elif deep_analysis_outcome == "success" and deep_reports and not missing_deep_codes:
        deep_status = "completed"
    else:
        deep_status = "incomplete"
        errors.append("deep_analysis_incomplete")

    if screening_outcome != "success" or screening_json is None:
        status = "failure"
    elif errors:
        status = "partial_success"
    elif not deep_analysis_requested:
        status = "screening_completed"
    else:
        status = "success"

    generated_at: Optional[datetime] = None
    if source.get("generated_at"):
        try:
            generated_at = _parse_datetime(str(source["generated_at"]), field="generated_at")
        except ValueError as exc:
            errors.append(f"generated_at_invalid:{exc}")
    if generated_at and not (started_at <= generated_at <= completed_at):
        errors.append("generated_at_outside_run_window")
    market_data_at: Optional[datetime] = None
    if source.get("market_data_at"):
        try:
            market_data_at = _parse_datetime(str(source["market_data_at"]), field="market_data_at")
        except ValueError as exc:
            errors.append(f"market_data_at_invalid:{exc}")
    if market_data_at and market_data_at > completed_at:
        errors.append("market_data_at_after_run_completion")
    trade_date = (generated_at or started_at).date().isoformat()
    average_coverage, minimum_coverage = _coverage(candidates)

    retry_events = _load_retry_events(logs_dir)
    deep_analysis_failures = [
        {
            "stock_code": event.get("stock_code"),
            "error_type": event.get("error_type"),
            "attempts": event.get("attempt"),
            "max_attempts": event.get("max_attempts"),
        }
        for event in retry_events
        if event.get("action") == "exhausted"
        and event.get("error_type") in {"gemini_429", "gemini_503"}
    ]
    reason_codes: list[str] = []
    if deep_status == "incomplete":
        reason_codes.append("deep_analysis_incomplete")
    reason_codes.extend(
        str(event["error_type"])
        for event in retry_events
        if event.get("error_type") in {"gemini_429", "gemini_503"}
    )
    retry_event_path = logs_dir / "llm_retry_events.jsonl" if logs_dir else None

    result_files = [
        path
        for path in [screening_json, codes_path, *screening_reports, *deep_reports, retry_event_path]
        if path
    ]
    file_hashes = {
        path.relative_to(repository_root).as_posix(): _sha256(path)
        for path in result_files
        if path.is_file()
    }
    manifest = {
        "schema_version": "1.1",
        "trade_date": trade_date,
        "workflow_name": workflow_name,
        "run_id": str(run_id),
        "run_number": str(run_number),
        "status": status,
        "started_at": started_at.isoformat(timespec="seconds"),
        "completed_at": completed_at.isoformat(timespec="seconds"),
        "screening_generated_at": generated_at.isoformat(timespec="seconds") if generated_at else None,
        "market_data_at": market_data_at.isoformat(timespec="seconds") if market_data_at else None,
        "data_source": source.get("data_source"),
        "model_version": source.get("model_version"),
        "screening_outcome": screening_outcome,
        "deep_analysis_outcome": deep_analysis_outcome,
        "deep_analysis_status": deep_status,
        "deep_analysis_requested": deep_analysis_requested,
        "market_record_count": int(source.get("universe_count", 0) or 0),
        "preselected_count": int(source.get("spot_filtered_count", 0) or 0),
        "history_success_count": int(source.get("history_success_count", 0) or 0),
        "history_failure_count": int(source.get("history_failure_count", 0) or 0),
        "enrichment_success_count": int(source.get("evidence_success_count", 0) or 0),
        "enrichment_failure_count": int(source.get("evidence_failure_count", 0) or 0),
        "candidate_count": len(candidates),
        "candidate_codes": normalized_candidate_codes,
        "analysis_codes": analysis_codes,
        "screening_json": screening_json.relative_to(repository_root).as_posix() if screening_json else None,
        "screened_codes": codes_path.relative_to(repository_root).as_posix() if codes_path.is_file() else None,
        "screening_reports": _relative_files(screening_reports, root=repository_root),
        "deep_analysis_reports": _relative_files(deep_reports, root=repository_root),
        "deep_analysis_missing_codes": missing_deep_codes,
        "llm_retry_events": retry_events,
        "deep_analysis_failures": deep_analysis_failures,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "artifact_name": artifact_name,
        "fixed_result_entry": {
            "branch": "screening-results",
            "latest_manifest": "latest/manifest.json",
            "latest_screening_json": "latest/market_screening.json",
            "latest_screened_codes": "latest/screened_codes.txt",
            "latest_reports": "latest/reports/",
            "history_prefix": f"history/{trade_date}/",
        },
        "evidence_coverage_average": average_coverage,
        "evidence_coverage_minimum": minimum_coverage,
        "result_file_sha256": file_hashes,
        "integrity": {
            "ok": not errors,
            "errors": errors,
            "screening_json_count": len(json_files),
        },
    }
    if errors and status not in {"failure", "partial_success"}:
        manifest["status"] = "partial_success"
    return manifest


def write_summary(path: Path, manifest: Mapping[str, Any]) -> None:
    integrity = manifest["integrity"]
    lines = [
        "## 全市场初筛运行清单",
        "",
        f"- 状态：`{manifest['status']}`",
        f"- 运行：`{manifest['run_number']}` / `{manifest['run_id']}`",
        f"- 候选数量：`{manifest['candidate_count']}`",
        f"- 深度分析：`{manifest['deep_analysis_status']}`",
        f"- Artifact：`{manifest['artifact_name']}`",
        f"- 完整性：`{'pass' if integrity['ok'] else 'fail'}`",
    ]
    if integrity["errors"]:
        lines.extend(["", "完整性问题：", *[f"- `{error}`" for error in integrity["errors"]]])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--logs-dir", type=Path, default=Path("logs"))
    parser.add_argument("--output", type=Path, default=Path("data/screening_run_manifest.json"))
    parser.add_argument("--workflow-name", default="全市场初筛")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-number", required=True)
    parser.add_argument("--artifact-name", required=True)
    parser.add_argument("--screening-outcome", required=True)
    parser.add_argument("--deep-analysis-outcome", default="skipped")
    parser.add_argument("--deep-analysis-requested", default="false")
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--completed-at", required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository_root = Path.cwd().resolve()
    try:
        started_at = _parse_datetime(args.started_at, field="started_at")
        completed_at = _parse_datetime(args.completed_at, field="completed_at")
        if completed_at < started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        manifest = build_manifest(
            repository_root=repository_root,
            data_dir=args.data_dir.resolve(),
            reports_dir=args.reports_dir.resolve(),
            workflow_name=args.workflow_name,
            run_id=args.run_id,
            run_number=args.run_number,
            artifact_name=args.artifact_name,
            screening_outcome=args.screening_outcome,
            deep_analysis_outcome=args.deep_analysis_outcome,
            deep_analysis_requested=_bool(args.deep_analysis_requested),
            started_at=started_at,
            completed_at=completed_at,
            logs_dir=args.logs_dir.resolve(),
        )
    except (OSError, ValueError, TypeError) as exc:
        print(f"manifest_error: {exc}")
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.summary:
        write_summary(args.summary, manifest)
    print(json.dumps({"status": manifest["status"], "output": str(args.output)}, ensure_ascii=False))
    return 3 if args.strict and not manifest["integrity"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
