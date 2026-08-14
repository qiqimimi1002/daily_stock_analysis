from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from data_provider.base import DataFetcherManager
from data_provider.market_snapshot import (
    MARKET_SNAPSHOT_ENV,
    MarketSnapshotError,
    load_market_snapshot_quote,
)
from data_provider.realtime_types import RealtimeSource
from scripts.validate_market_snapshot import main as validate_snapshot_main
from src.core.pipeline import StockAnalysisPipeline
from src.enums import ReportType
from src.services.market_screener import MarketScreener, ScreeningConfig, save_result


def _history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-07-01", periods=30, freq="B"),
            "close": [54.0 + index * 4.2 / 29 for index in range(30)],
            "volume": [1_000_000.0] * 30,
            "amount": [1_000_000_000.0] * 30,
        }
    )


def _valid_snapshot() -> dict:
    return {
        "schema_version": "1.0",
        "market_data_at": "2026-08-14T10:01:55+08:00",
        "data_source": "akshare_eastmoney",
        "price_change_formula": "(price - prev_close) / prev_close * 100",
        "quotes": {
            "600487": {
                "name": "亨通光电",
                "price": 58.20,
                "prev_close": 57.25,
                "change_pct": 1.66,
            }
        },
    }


def test_same_run_deep_analysis_reuses_screening_price_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spot = pd.DataFrame(
        [
            {
                "code": "600487",
                "name": "亨通光电",
                "close": 58.20,
                "prev_close": 57.25,
                # Simulate a conflicting provider percentage. The canonical
                # price/previous-close formula must win.
                "pct_change": -1.62,
                "volume": 50_000_000,
                "amount": 2_700_000_000,
                "turnover": 1.93,
                "volume_ratio": 0.90,
                "pe_ratio": 20.0,
                "pb_ratio": 2.0,
                "open": 58.10,
                "high": 58.72,
                "low": 57.25,
            }
        ]
    )
    spot.attrs["market_data_source"] = "akshare_eastmoney"
    spot.attrs["market_data_at"] = "2026-08-14T10:01:55+08:00"
    result = MarketScreener(
        ScreeningConfig(
            top_n=1,
            analysis_limit=1,
            preselect_limit=1,
            history_workers=1,
        )
    ).run(spot_frame=spot, history_fetcher=lambda _: _history())

    candidate = result.candidates[0]
    assert candidate.latest_price == 58.20
    assert candidate.prev_close == 57.25
    assert candidate.daily_pct == 1.66
    assert result.market_data_at == "2026-08-14T10:01:55+08:00"

    snapshot_path = tmp_path / "market_snapshot.json"
    save_result(
        result,
        report_path=tmp_path / "screening.md",
        json_path=tmp_path / "screening.json",
        codes_path=tmp_path / "screened_codes.txt",
        snapshot_path=snapshot_path,
    )
    monkeypatch.setenv(MARKET_SNAPSHOT_ENV, str(snapshot_path))

    manager = DataFetcherManager.__new__(DataFetcherManager)
    quote = manager.get_realtime_quote("600487")

    assert quote is not None
    assert quote.source is RealtimeSource.MARKET_SNAPSHOT
    assert quote.upstream_source == "akshare_eastmoney"
    assert quote.provider_timestamp == result.market_data_at
    assert quote.price == candidate.latest_price
    assert quote.pre_close == candidate.prev_close
    assert quote.change_pct == candidate.daily_pct


def test_market_snapshot_rejects_tampered_percentage(tmp_path: Path) -> None:
    path = tmp_path / "market_snapshot.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "market_data_at": "2026-08-14T10:01:55+08:00",
                "data_source": "akshare_eastmoney",
                "price_change_formula": "(price - prev_close) / prev_close * 100",
                "quotes": {
                    "600487": {
                        "price": 58.20,
                        "prev_close": 57.25,
                        "change_pct": -1.62,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(MarketSnapshotError, match="change_pct is inconsistent"):
        load_market_snapshot_quote(path, "600487")


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing_file", "market snapshot cannot be read"),
        ("missing_candidate", "market snapshot has no quote for 600487"),
        ("invalid_number", "market snapshot field price is invalid"),
        ("invalid_time", "market snapshot market_data_at must include timezone"),
    ],
)
def test_snapshot_preflight_fails_with_diagnostic_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected_error: str,
) -> None:
    snapshot_path = tmp_path / "market_snapshot.json"
    if case != "missing_file":
        payload = _valid_snapshot()
        if case == "missing_candidate":
            payload["quotes"] = {}
        elif case == "invalid_number":
            payload["quotes"]["600487"]["price"] = "invalid"
        elif case == "invalid_time":
            payload["market_data_at"] = "2026-08-14T10:01:55"
        snapshot_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    error_report = tmp_path / "market_snapshot_error.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_market_snapshot.py",
            "--snapshot",
            str(snapshot_path),
            "--codes",
            "600487",
            "--error-report",
            str(error_report),
        ],
    )

    assert validate_snapshot_main() == 2
    assert expected_error in capsys.readouterr().err
    report = error_report.read_text(encoding="utf-8")
    assert expected_error in report
    assert "深度分析未启动" in report
    assert "未切换到其他行情源或历史收盘价" in report


def test_snapshot_preflight_accepts_valid_same_run_quotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_path = tmp_path / "market_snapshot.json"
    snapshot_path.write_text(
        json.dumps(_valid_snapshot(), ensure_ascii=False),
        encoding="utf-8",
    )
    error_report = tmp_path / "market_snapshot_error.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_market_snapshot.py",
            "--snapshot",
            str(snapshot_path),
            "--codes",
            "600487",
            "--error-report",
            str(error_report),
        ],
    )

    assert validate_snapshot_main() == 0
    assert not error_report.exists()


def test_configured_snapshot_error_does_not_call_provider_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DataFetcherManager.__new__(DataFetcherManager)
    fallback = Mock()
    monkeypatch.setattr(manager, "_try_fetcher_quote", fallback)
    monkeypatch.setenv(MARKET_SNAPSHOT_ENV, str(tmp_path / "missing.json"))

    with pytest.raises(MarketSnapshotError, match="market snapshot cannot be read"):
        manager.get_realtime_quote("600487")

    fallback.assert_not_called()


def test_pipeline_does_not_degrade_snapshot_error_to_historical_close() -> None:
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(
        enable_realtime_quote=True,
        report_language="zh",
    )
    pipeline.query_source = "cli"
    pipeline.analysis_phase = "auto"
    pipeline.fetcher_manager = Mock()
    pipeline.fetcher_manager.get_stock_name.return_value = "亨通光电"
    pipeline.fetcher_manager.get_realtime_quote.side_effect = MarketSnapshotError(
        "market snapshot has no quote for 600487"
    )
    pipeline._emit_progress = Mock()
    pipeline._load_daily_market_context = Mock(return_value=None)
    pipeline.analyzer = Mock()

    with pytest.raises(MarketSnapshotError, match="has no quote"):
        pipeline.analyze_stock(
            "600487",
            ReportType.SIMPLE,
            query_id="snapshot-error-test",
            current_time=datetime(2026, 8, 14, 2, 5, tzinfo=timezone.utc),
        )

    pipeline.analyzer.analyze.assert_not_called()
