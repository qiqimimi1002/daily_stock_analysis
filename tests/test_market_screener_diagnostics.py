from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.services.market_screener import (
    MarketScreener,
    PublicMarketDataSource,
    ScreeningConfig,
)
from src.services.market_screener_diagnostics import MarketScreenerDiagnostics


def _history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": pd.date_range("2026-06-01", periods=30, freq="B"),
            "收盘": [18.0 + index * 6.0 / 29 for index in range(30)],
            "成交量": [1_000_000.0] * 30,
            "成交额": [300_000_000.0] * 30,
        }
    )


def _spot() -> pd.DataFrame:
    return pd.DataFrame(
        [["600100", "主板甲", 24.0, 1.2, 100, 1_500_000_000, 2.0]],
        columns=["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额", "换手率"],
    )


def _read_events(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _config() -> ScreeningConfig:
    return ScreeningConfig(
        top_n=1,
        analysis_limit=1,
        preselect_limit=1,
        history_workers=1,
        enrichment_limit=1,
        evidence_workers=1,
    )


def test_history_request_records_start_end_and_elapsed(tmp_path: Path) -> None:
    path = tmp_path / "market_screener_timing.jsonl"
    diagnostics = MarketScreenerDiagnostics(path)
    source = PublicMarketDataSource(
        sleep=lambda _: None,
        diagnostics=diagnostics,
    )

    with (
        patch.object(source, "_fetch_history_akshare_em", return_value=_history()),
        patch.object(source, "_fetch_history_akshare_sina", return_value=_history()),
    ):
        source.fetch_history_with_meta("600100", max_retries=0)

    events = _read_events(path)
    started = [
        event
        for event in events
        if event["event"] == "history_request_started"
        and event["provider"] == "akshare_eastmoney"
    ]
    completed = [
        event
        for event in events
        if event["event"] == "history_request_completed"
        and event["provider"] == "akshare_eastmoney"
    ]
    assert len(started) == 1
    assert len(completed) == 1
    assert completed[0]["code"] == "600100"
    assert completed[0]["attempt"] == 1
    assert completed[0]["success"] is True
    assert completed[0]["status"] == "success"
    assert completed[0]["error_category"] is None
    assert completed[0]["request_started_at"] == started[0]["request_started_at"]
    assert completed[0]["request_completed_at"]
    assert completed[0]["elapsed_seconds"] >= 0


def test_history_request_failure_records_provider_code_and_category(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market_screener_timing.jsonl"
    diagnostics = MarketScreenerDiagnostics(path)
    source = PublicMarketDataSource(
        sleep=lambda _: None,
        diagnostics=diagnostics,
    )

    with (
        patch.object(
            source,
            "_fetch_history_akshare_em",
            side_effect=TimeoutError("provider timed out"),
        ),
        patch.object(source, "_fetch_history_akshare_sina", return_value=_history()),
    ):
        source.fetch_history_with_meta("600100", max_retries=0)

    failed = [
        event
        for event in _read_events(path)
        if event["event"] == "history_request_completed"
        and event["provider"] == "akshare_eastmoney"
    ]
    assert len(failed) == 1
    assert failed[0]["code"] == "600100"
    assert failed[0]["status"] == "failure"
    assert failed[0]["success"] is False
    assert failed[0]["error_category"] == "timeout"


def test_heartbeat_lists_pending_futures_and_active_provider(tmp_path: Path) -> None:
    path = tmp_path / "market_screener_timing.jsonl"
    diagnostics = MarketScreenerDiagnostics(path)
    assert diagnostics.heartbeat_interval_seconds == 20.0
    diagnostics.start()
    diagnostics.begin_stage(
        "history_fetch",
        pending_codes=["600100", "000100"],
    )
    diagnostics.request_started(
        code="600100",
        provider="akshare_eastmoney",
        attempt=1,
    )
    diagnostics.mark_completed("000100")
    diagnostics.emit_heartbeat()
    diagnostics.stop(status="success")

    heartbeat = next(
        event
        for event in _read_events(path)
        if event["event"] == "heartbeat"
    )
    assert heartbeat["current_stage"] == "history_fetch"
    assert heartbeat["completed_count"] == 1
    assert heartbeat["total_count"] == 2
    assert heartbeat["pending_codes"] == ["600100"]
    assert heartbeat["active_providers"] == [
        {
            "attempt": 1,
            "code": "600100",
            "provider": "akshare_eastmoney",
        }
    ]
    assert heartbeat["run_elapsed_seconds"] >= 0


def test_stage_events_are_valid_json_and_cover_all_phases(tmp_path: Path) -> None:
    path = tmp_path / "market_screener_timing.jsonl"
    diagnostics = MarketScreenerDiagnostics(path)

    result = MarketScreener(
        _config(),
        diagnostics=diagnostics,
    ).run(
        spot_frame=_spot(),
        history_fetcher=lambda _: _history(),
    )

    events = _read_events(path)
    assert result.history_success_count == 1
    completed_stages = {
        event["stage"]: event
        for event in events
        if event["event"] == "stage_completed"
    }
    assert set(completed_stages) == {
        "full_market_fetch",
        "basic_filter",
        "history_fetch",
        "evidence_enrichment",
    }
    assert completed_stages["full_market_fetch"]["source"] == "injected"
    assert completed_stages["full_market_fetch"]["record_count"] == 1
    assert completed_stages["basic_filter"]["input_count"] == 1
    assert completed_stages["basic_filter"]["output_count"] == 1
    assert completed_stages["history_fetch"]["success_count"] == 1
    assert completed_stages["evidence_enrichment"]["status"] == "skipped"
    assert all(event["schema_version"] == "1.0" for event in events)


def test_diagnostic_write_failure_does_not_block_screening(tmp_path: Path) -> None:
    diagnostics = MarketScreenerDiagnostics(tmp_path / "unwritable.jsonl")

    with patch.object(
        diagnostics,
        "_append_line",
        side_effect=OSError("disk unavailable"),
    ):
        result = MarketScreener(
            _config(),
            diagnostics=diagnostics,
        ).run(
            spot_frame=_spot(),
            history_fetcher=lambda _: _history(),
        )

    assert result.history_success_count == 1
    assert [candidate.code for candidate in result.candidates] == ["600100"]
