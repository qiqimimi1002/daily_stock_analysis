"""Validate sanitized Tushare intraday acceptance evidence offline.

The tool is intentionally read-only: it does not import a market provider,
read a Secret, create an HTTP client, or write any file. Machine-readable JSON
is printed to stdout and the human checklist is printed to stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


CN_TZ = ZoneInfo("Asia/Shanghai")
BASELINE_COMMIT = "8009de5b4aaac85e27d4ff7487128c6f9e15de7a"
ALLOWED_ARTIFACTS = {
    "acceptance-summary.json",
    "redaction-scan.json",
}
MAX_ARTIFACT_BYTES = 128 * 1024
MAX_SCAN_BYTES = 1024 * 1024
SUMMARY_FIELDS = {
    "schema_version",
    "mode",
    "requested_codes",
    "reason",
    "provider",
    "api_name",
    "source_label",
    "generated_at",
    "market_data_at",
    "market_state",
    "row_count",
    "columns",
    "volume_unit",
    "amount_unit",
    "quality_status",
    "request_elapsed_seconds",
    "normalized_content_sha256",
    "raw_market_data_persisted",
}
REQUIRED_NORMALIZED_COLUMNS = {
    "code",
    "name",
    "close",
    "prev_close",
    "open",
    "high",
    "low",
    "pct_change",
    "volume",
    "amount",
    "trade_time",
    "market_data_at",
}
SENSITIVE_PATTERNS = {
    "tushare_token_name": re.compile(r"\bTUSHARE_TOKEN\b", re.IGNORECASE),
    "authorization_header": re.compile(
        r"\bauthorization\s*[:=]", re.IGNORECASE
    ),
    "bearer_credential": re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    "token_assignment": re.compile(
        r"\b(?:token|api[_-]?key|secret)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    "credential_shape": re.compile(
        r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
    ),
    "http_headers": re.compile(
        r"\b(?:request|response)?\s*headers?\s*[:=]", re.IGNORECASE
    ),
    "http_response_body": re.compile(
        r"\bresponse\s+body\s*[:=]", re.IGNORECASE
    ),
    "raw_market_row": re.compile(
        r'"(?:ts_code|pre_close|open|high|low|close|vol|amount)"\s*:'
    ),
}


def _check(check_id: str, passed: bool, reason: str) -> dict[str, str]:
    return {
        "id": check_id,
        "status": "PASS" if passed else "FAIL",
        "reason": reason,
    }


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _parse_shanghai(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=8):
        return None
    return parsed.astimezone(CN_TZ)


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
    if not isinstance(payload, dict):
        return None, "top_level_not_object"
    return payload, None


def _iter_files(roots: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved in seen or not root.exists():
            continue
        seen.add(resolved)
        if root.is_file():
            yield root
            continue
        yield from (path for path in root.rglob("*") if path.is_file())


def _scan_files(roots: Iterable[Path]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in _iter_files(roots):
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                findings.append(
                    {"file": path.name, "category": "scan_file_too_large"}
                )
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(
                {"file": path.name, "category": "unreadable_or_binary"}
            )
            continue
        for category, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                findings.append({"file": path.name, "category": category})
    return findings


def validate_acceptance(
    artifact_dir: Path,
    *,
    runtime_dir: Path | None = None,
    expected_code: str = "600000.SH",
    as_of: datetime | None = None,
    max_stale_seconds: int = 180,
) -> dict[str, Any]:
    """Return a deterministic PASS/FAIL report without changing the inputs."""

    as_of = (as_of or datetime.now(CN_TZ)).astimezone(CN_TZ)
    checks: list[dict[str, str]] = []
    artifact_files = (
        sorted(
            path.relative_to(artifact_dir).as_posix()
            for path in artifact_dir.rglob("*")
            if path.is_file()
        )
        if artifact_dir.is_dir()
        else []
    )
    exact_artifacts = set(artifact_files) == ALLOWED_ARTIFACTS
    checks.append(
        _check(
            "artifact_allowlist",
            exact_artifacts,
            "exactly the two sanitized JSON artifacts are present"
            if exact_artifacts
            else "artifact set is missing, extra, or nested",
        )
    )
    size_ok = bool(artifact_files) and all(
        (artifact_dir / name).stat().st_size <= MAX_ARTIFACT_BYTES
        for name in artifact_files
    )
    checks.append(
        _check(
            "artifact_size",
            size_ok,
            "all artifacts are at most 128 KiB"
            if size_ok
            else "an artifact is missing or exceeds 128 KiB",
        )
    )

    summary_path = artifact_dir / "acceptance-summary.json"
    summary, summary_error = _read_json(summary_path)
    checks.append(
        _check(
            "summary_json",
            summary is not None,
            "summary is valid JSON" if summary is not None else str(summary_error),
        )
    )
    summary = summary or {}
    schema_ok = set(summary) == SUMMARY_FIELDS
    checks.append(
        _check(
            "summary_schema",
            schema_ok,
            "summary uses the exact field allowlist"
            if schema_ok
            else "summary fields are missing or not allow-listed",
        )
    )
    types_ok = (
        isinstance(summary.get("schema_version"), str)
        and isinstance(summary.get("mode"), str)
        and isinstance(summary.get("requested_codes"), list)
        and all(
            isinstance(code, str) for code in summary.get("requested_codes", [])
        )
        and isinstance(summary.get("reason"), str)
        and bool(summary.get("reason"))
        and isinstance(summary.get("provider"), str)
        and isinstance(summary.get("api_name"), str)
        and isinstance(summary.get("source_label"), str)
        and isinstance(summary.get("generated_at"), str)
        and isinstance(summary.get("market_data_at"), str)
        and isinstance(summary.get("market_state"), str)
        and isinstance(summary.get("row_count"), int)
        and not isinstance(summary.get("row_count"), bool)
        and isinstance(summary.get("columns"), list)
        and all(isinstance(item, str) for item in summary.get("columns", []))
        and isinstance(summary.get("volume_unit"), str)
        and isinstance(summary.get("amount_unit"), str)
        and isinstance(summary.get("quality_status"), str)
        and _is_number(summary.get("request_elapsed_seconds"))
        and float(summary.get("request_elapsed_seconds", -1)) >= 0
        and isinstance(summary.get("normalized_content_sha256"), str)
        and isinstance(summary.get("raw_market_data_persisted"), bool)
    )
    checks.append(
        _check(
            "summary_types",
            types_ok,
            "summary field types are valid"
            if types_ok
            else "one or more summary field types are invalid",
        )
    )
    identity_ok = (
        summary.get("schema_version") == "1.0"
        and summary.get("mode") == "single_stock"
        and summary.get("requested_codes") == [expected_code]
        and summary.get("provider") == "tushare"
        and summary.get("api_name") == "rt_k"
        and summary.get("source_label") == "数据来源：Tushare数据"
        and summary.get("row_count") == 1
    )
    checks.append(
        _check(
            "single_stock_identity",
            identity_ok,
            f"single-stock evidence is bound to {expected_code}"
            if identity_ok
            else "mode, code, provider, API, source label, or row count differs",
        )
    )
    columns = set(summary.get("columns", []))
    normalized_ok = REQUIRED_NORMALIZED_COLUMNS.issubset(columns)
    checks.append(
        _check(
            "normalized_contract",
            normalized_ok,
            "price(close), prev_close, OHLC, volume and amount passed the "
            "provider quality gate without persisting their values"
            if normalized_ok
            else "required normalized columns are missing",
        )
    )
    unit_ok = (
        summary.get("volume_unit") == "shares"
        and summary.get("amount_unit") == "yuan"
    )
    checks.append(
        _check(
            "unit_contract",
            unit_ok,
            "volume=shares and amount=yuan"
            if unit_ok
            else "volume or amount unit is invalid",
        )
    )
    checks.append(
        _check(
            "market_state",
            summary.get("market_state") == "intraday",
            "market state is intraday"
            if summary.get("market_state") == "intraday"
            else "market state is not intraday",
        )
    )
    checks.append(
        _check(
            "quality_status",
            summary.get("quality_status") == "ok",
            "provider quality status is ok"
            if summary.get("quality_status") == "ok"
            else "provider quality status is not ok",
        )
    )
    market_at = _parse_shanghai(summary.get("market_data_at"))
    generated_at = _parse_shanghai(summary.get("generated_at"))
    time_ok = (
        market_at is not None
        and generated_at is not None
        and market_at <= generated_at <= as_of
    )
    checks.append(
        _check(
            "time_contract",
            time_ok,
            "market and generated times are Asia/Shanghai and ordered"
            if time_ok
            else "time is invalid, wrong-zone, future, or out of order",
        )
    )
    age_seconds = (as_of - market_at).total_seconds() if market_at else None
    freshness_ok = (
        age_seconds is not None
        and 0 <= age_seconds <= max_stale_seconds
    )
    checks.append(
        _check(
            "freshness",
            freshness_ok,
            f"quote age is {age_seconds:.1f}s"
            if freshness_ok
            else "quote is future, invalid, or older than the freshness limit",
        )
    )
    hash_value = summary.get("normalized_content_sha256")
    safety_flags_ok = (
        summary.get("raw_market_data_persisted") is False
        and isinstance(hash_value, str)
        and re.fullmatch(r"[0-9a-f]{64}", hash_value) is not None
    )
    checks.append(
        _check(
            "summary_safety_flags",
            safety_flags_ok,
            "raw data persistence is false and the content hash is valid"
            if safety_flags_ok
            else "raw persistence flag or content hash is invalid",
        )
    )

    redaction_path = artifact_dir / "redaction-scan.json"
    redaction, redaction_error = _read_json(redaction_path)
    redaction_ok = (
        redaction is not None
        and set(redaction) == {"status", "findings"}
        and redaction.get("status") == "passed"
        and redaction.get("findings") == {}
    )
    checks.append(
        _check(
            "redaction_report",
            redaction_ok,
            "redaction report passed with no findings"
            if redaction_ok
            else f"redaction report failed or is invalid: {redaction_error}",
        )
    )
    scan_roots = [artifact_dir]
    if runtime_dir is not None:
        scan_roots.append(runtime_dir)
    findings = _scan_files(scan_roots)
    checks.append(
        _check(
            "sensitive_data_scan",
            not findings,
            "no credential, header/body, or raw paid-row pattern found"
            if not findings
            else "sensitive or raw-data pattern detected",
        )
    )

    overall_status = (
        "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    )
    return {
        "schema_version": "1.0",
        "overall_status": overall_status,
        "baseline_commit": BASELINE_COMMIT,
        "expected_code": expected_code,
        "as_of": as_of.isoformat(timespec="seconds"),
        "max_stale_seconds": max_stale_seconds,
        "checks": checks,
        "scan_findings": findings,
        "manual_review_items": [
            "Confirm the GitHub run used only 600000.SH on main by the owner.",
            "Confirm the provider step completed without an auth or retry anomaly.",
            "Confirm the run uploaded exactly the two allow-listed JSON files.",
            "Confirm no raw quote value was printed in the private job log.",
            "Treat price numeric validity as provider quality-gate evidence; "
            "the values are intentionally absent from sanitized artifacts.",
        ],
    }


def render_human(report: dict[str, Any]) -> str:
    lines = [
        f"Tushare intraday acceptance: {report['overall_status']}",
        f"Baseline: {report['baseline_commit']}",
        f"Expected code: {report['expected_code']}",
        "",
        "Machine checks:",
    ]
    lines.extend(
        f"- [{item['status']}] {item['id']}: {item['reason']}"
        for item in report["checks"]
    )
    lines.append("")
    lines.append("Manual review:")
    lines.extend(f"- [MANUAL] {item}" for item in report["manual_review_items"])
    return "\n".join(lines)


def _parse_cli_time(value: str) -> datetime:
    parsed = _parse_shanghai(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            "--as-of must be an ISO timestamp with Asia/Shanghai +08:00 offset"
        )
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline validation of sanitized Tushare acceptance evidence"
    )
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--expected-code", default="600000.SH")
    parser.add_argument("--as-of", type=_parse_cli_time)
    parser.add_argument("--max-stale-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    report = validate_acceptance(
        args.artifact_dir,
        runtime_dir=args.runtime_dir,
        expected_code=args.expected_code,
        as_of=args.as_of,
        max_stale_seconds=args.max_stale_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(render_human(report), file=sys.stderr)
    return 0 if report["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
