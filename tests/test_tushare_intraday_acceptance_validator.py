"""Offline-only tests for the private intraday acceptance evidence validator.

Every payload in this module is invented. No provider or network is used.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.validate_tushare_intraday_acceptance import main, validate_acceptance


CN_TZ = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 8, 21, 9, 36, 30, tzinfo=CN_TZ)


def _summary() -> dict:
    return {
        "schema_version": "1.0",
        "mode": "single_stock",
        "requested_codes": ["600000.SH"],
        "reason": "synthetic-offline-test",
        "provider": "tushare",
        "api_name": "rt_k",
        "source_label": "数据来源：Tushare数据",
        "generated_at": "2026-08-21T09:36:10+08:00",
        "market_data_at": "2026-08-21T09:36:00+08:00",
        "market_state": "intraday",
        "row_count": 1,
        "columns": [
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
        ],
        "volume_unit": "shares",
        "amount_unit": "yuan",
        "quality_status": "ok",
        "request_elapsed_seconds": 0.25,
        "normalized_content_sha256": "a" * 64,
        "raw_market_data_persisted": False,
    }


def _write_bundle(
    root: Path,
    *,
    summary: dict | None = None,
    scan: dict | None = None,
) -> Path:
    artifact_dir = root / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "acceptance-summary.json").write_text(
        json.dumps(summary or _summary(), ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact_dir / "redaction-scan.json").write_text(
        json.dumps(
            scan or {"status": "passed", "findings": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return artifact_dir


def _failed_checks(report: dict) -> set[str]:
    return {
        item["id"]
        for item in report["checks"]
        if item["status"] == "FAIL"
    }


def test_valid_synthetic_bundle_passes_without_network(tmp_path, monkeypatch):
    network_calls = []

    def fail_network(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError("offline validator attempted a network call")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    artifact_dir = _write_bundle(tmp_path)

    report = validate_acceptance(artifact_dir, as_of=AS_OF)

    assert report["overall_status"] == "PASS"
    assert network_calls == []
    assert all(item["status"] == "PASS" for item in report["checks"])


@pytest.mark.parametrize(
    ("field", "value", "check_id"),
    [
        ("provider", None, "summary_schema"),
        ("row_count", "1", "summary_types"),
        ("market_state", "premarket", "market_state"),
        ("quality_status", "unknown", "quality_status"),
        ("quality_status", "invalid_volume", "quality_status"),
        ("quality_status", "invalid_amount", "quality_status"),
        ("volume_unit", "lots", "unit_contract"),
        ("amount_unit", "thousand_yuan", "unit_contract"),
        ("request_elapsed_seconds", -1, "summary_types"),
    ],
)
def test_summary_contract_failures_are_blocking(
    tmp_path,
    field,
    value,
    check_id,
):
    summary = _summary()
    if value is None:
        summary.pop(field)
    else:
        summary[field] = value

    report = validate_acceptance(
        _write_bundle(tmp_path, summary=summary),
        as_of=AS_OF,
    )

    assert report["overall_status"] == "FAIL"
    assert check_id in _failed_checks(report)


@pytest.mark.parametrize(
    ("market_data_at", "check_id"),
    [
        ("2026-08-21T09:36:31+08:00", "freshness"),
        ("2026-08-21T09:33:29+08:00", "freshness"),
        ("2026-08-21T01:36:00+00:00", "time_contract"),
        ("not-a-time", "time_contract"),
    ],
)
def test_time_contract_rejects_future_stale_or_wrong_timezone(
    tmp_path,
    market_data_at,
    check_id,
):
    summary = _summary()
    summary["market_data_at"] = market_data_at

    report = validate_acceptance(
        _write_bundle(tmp_path, summary=summary),
        as_of=AS_OF,
    )

    assert report["overall_status"] == "FAIL"
    assert check_id in _failed_checks(report)


def test_missing_required_normalized_columns_fails(tmp_path):
    summary = _summary()
    summary["columns"].remove("prev_close")
    summary["columns"].remove("volume")
    summary["columns"].remove("amount")

    report = validate_acceptance(
        _write_bundle(tmp_path, summary=summary),
        as_of=AS_OF,
    )

    assert report["overall_status"] == "FAIL"
    assert "normalized_contract" in _failed_checks(report)


def test_missing_artifact_fails(tmp_path):
    artifact_dir = _write_bundle(tmp_path)
    (artifact_dir / "redaction-scan.json").unlink()

    report = validate_acceptance(artifact_dir, as_of=AS_OF)

    assert report["overall_status"] == "FAIL"
    assert "artifact_allowlist" in _failed_checks(report)


@pytest.mark.parametrize(
    "extra_path",
    ["raw-response.json", "debug.log", "cache/quote.json"],
)
def test_extra_or_nested_artifact_fails(tmp_path, extra_path):
    artifact_dir = _write_bundle(tmp_path)
    path = artifact_dir / extra_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic extra file", encoding="utf-8")

    report = validate_acceptance(artifact_dir, as_of=AS_OF)

    assert report["overall_status"] == "FAIL"
    assert "artifact_allowlist" in _failed_checks(report)


def test_oversized_artifact_fails(tmp_path):
    artifact_dir = _write_bundle(tmp_path)
    summary_path = artifact_dir / "acceptance-summary.json"
    summary_path.write_text("x" * 131_073, encoding="utf-8")

    report = validate_acceptance(artifact_dir, as_of=AS_OF)

    assert report["overall_status"] == "FAIL"
    assert "artifact_size" in _failed_checks(report)


@pytest.mark.parametrize(
    "leak",
    [
        "TUSHARE_TOKEN=synthetic-never-use",
        "Authorization: Bearer synthetic-never-use",
        '{"data":[{"ts_code":"999999.SH","pre_close":1,"close":1}]}',
        'response body: {"open":1,"high":1,"low":1,"vol":1}',
    ],
)
def test_runtime_leaks_are_blocking(tmp_path, leak):
    artifact_dir = _write_bundle(tmp_path)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "synthetic.log").write_text(leak, encoding="utf-8")

    report = validate_acceptance(
        artifact_dir,
        runtime_dir=runtime_dir,
        as_of=AS_OF,
    )

    assert report["overall_status"] == "FAIL"
    assert "sensitive_data_scan" in _failed_checks(report)


def test_failed_redaction_report_is_blocking(tmp_path):
    artifact_dir = _write_bundle(
        tmp_path,
        scan={"status": "failed", "findings": {"synthetic.log": ["token"]}},
    )

    report = validate_acceptance(artifact_dir, as_of=AS_OF)

    assert report["overall_status"] == "FAIL"
    assert "redaction_report" in _failed_checks(report)


def test_cli_emits_machine_json_and_human_checklist(tmp_path, capsys):
    artifact_dir = _write_bundle(tmp_path)

    exit_code = main(
        [
            "--artifact-dir",
            str(artifact_dir),
            "--as-of",
            "2026-08-21T09:36:30+08:00",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["overall_status"] == "PASS"
    assert "Machine checks:" in captured.err
    assert "Manual review:" in captured.err


def test_validator_source_has_no_provider_or_network_entrypoint():
    source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "validate_tushare_intraday_acceptance.py"
    ).read_text(encoding="utf-8")

    assert "data_provider" not in source
    assert "TushareRtKProvider" not in source
    assert "requests." not in source
    assert "urllib" not in source
