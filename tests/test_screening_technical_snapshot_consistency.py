from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from data_provider.technical_snapshot import (
    TECHNICAL_SNAPSHOT_ENV,
    TechnicalSnapshotError,
    load_technical_snapshot_context,
)
from src.analyzer import AnalysisResult
from src.core.pipeline import StockAnalysisPipeline
from src.services.report_renderer import render
from src.services.market_screener import build_technical_snapshot
from src.stock_analyzer import StockTrendAnalyzer


def _history(days: int = 30, *, last_date: date = date(2026, 8, 18)) -> pd.DataFrame:
    dates = [last_date - timedelta(days=days - 1 - index) for index in range(days)]
    closes = [40.0 + index * 0.25 for index in range(days)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [value - 0.1 for value in closes],
            "high": [value + 0.3 for value in closes],
            "low": [value - 0.3 for value in closes],
            "close": closes,
            "volume": [1_000_000 + index * 10_000 for index in range(days)],
        }
    )


def _snapshot(*, code: str = "600378", run_id: str = "32207015938") -> dict:
    return {
        "schema_version": "1.0",
        "trade_date": "2026-08-19",
        "run_id": run_id,
        "run_number": "49",
        "technical_as_of": "2026-08-19T10:01:42+08:00",
        "indicators": {
            code: {
                "code": code,
                "history_data_through": "2026-08-18",
                "reference_price": 48.15,
                "ma5": 49.02,
                "ma10": 48.30,
                "ma20": 45.30,
                "five_day_pct": 3.52,
                "watch_zone": "45.30—49.02",
                "provider_volume_ratio": None,
                "completed_day_volume_ratio_5d": 0.92,
                "history_source": "akshare_eastmoney",
                "history_price_adjustment": "qfq",
            }
        },
    }


def _write_snapshot(path: Path, **overrides: str) -> Path:
    payload = _snapshot(**overrides)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _result() -> AnalysisResult:
    return AnalysisResult(
        code="600378",
        name="昊华科技",
        sentiment_score=50,
        trend_prediction="震荡",
        operation_advice="观望",
        dashboard={
            "data_perspective": {
                "price_position": {"ma5": 999, "ma10": 998, "ma20": 997},
                "volume_analysis": {
                    "volume_ratio": 0.0,
                    "volume_status": "缩量",
                    "volume_meaning": "缩量回调",
                },
            }
        },
        ma_analysis="盘中均线重新计算为999",
        volume_analysis="量比0.0，缩量回调",
        trend_analysis="均线偏强，缩量洗盘",
    )


def test_same_run_snapshot_drives_internal_and_structured_ma(tmp_path: Path) -> None:
    context = load_technical_snapshot_context(
        _write_snapshot(tmp_path / "technical.json"),
        "600378",
        expected_trade_date="2026-08-19",
        expected_run_id="32207015938",
        expected_run_number="49",
    )
    trend = StockTrendAnalyzer().analyze(
        _history(),
        "600378",
        technical_context=context,
    )
    assert (trend.ma5, trend.ma10, trend.ma20) == (49.02, 48.30, 45.30)
    assert trend.five_day_pct == 3.52
    assert trend.watch_zone == "45.30—49.02"
    assert trend.current_price == 48.15

    result = _result()
    StockAnalysisPipeline._apply_screening_technical_result(result, trend)
    price_position = result.dashboard["data_perspective"]["price_position"]
    assert (price_position["ma5"], price_position["ma10"], price_position["ma20"]) == (
        49.02,
        48.30,
        45.30,
    )
    assert price_position["five_day_pct"] == 3.52
    assert price_position["watch_zone"] == "45.30—49.02"
    assert "49.02" in result.ma_analysis
    markdown = render("markdown", [result], report_date="2026-08-19")
    assert markdown is not None
    assert "| MA5 | 49.02 |" in markdown
    assert "| MA10 | 48.3 |" in markdown
    assert "| MA20 | 45.3 |" in markdown
    assert "999" not in markdown


def test_screener_builds_same_run_technical_snapshot() -> None:
    candidate = SimpleNamespace(
        code="600378",
        latest_price=48.15,
        ma5=49.02,
        ma10=48.30,
        ma20=45.30,
        five_day_pct=3.52,
        watch_zone="45.30—49.02",
        volume_ratio_5d=0.92,
        history_data_through="2026-08-18",
        history_source="akshare_eastmoney",
        history_price_adjustment="qfq",
    )
    result = SimpleNamespace(
        market_data_at="2026-08-19T10:01:42+08:00",
        data_source="akshare_eastmoney",
        market_snapshot={
            "quotes": {"600378": {"volume_ratio": None}},
        },
        candidates=[candidate],
        analysis_codes=["600378"],
    )
    payload = build_technical_snapshot(
        result,
        run_id="32207015938",
        run_number="49",
    )
    assert payload["trade_date"] == "2026-08-19"
    assert payload["run_id"] == "32207015938"
    assert payload["run_number"] == "49"
    assert payload["indicators"]["600378"]["provider_volume_ratio"] is None
    assert payload["indicators"]["600378"]["completed_day_volume_ratio_5d"] == 0.92


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("expected_trade_date", "2026-08-20", "trade_date"),
        ("expected_run_id", "different", "run_id"),
        ("expected_run_number", "50", "run_number"),
    ],
)
def test_snapshot_identity_mismatch_fails_closed(
    tmp_path: Path,
    field: str,
    value: str,
    error: str,
) -> None:
    kwargs = {
        "expected_trade_date": "2026-08-19",
        "expected_run_id": "32207015938",
        "expected_run_number": "49",
    }
    kwargs[field] = value
    with pytest.raises(TechnicalSnapshotError, match=error):
        load_technical_snapshot_context(
            _write_snapshot(tmp_path / "technical.json"),
            "600378",
            **kwargs,
        )


def test_missing_code_fails_closed_without_realtime_ma_fallback(tmp_path: Path) -> None:
    path = _write_snapshot(tmp_path / "technical.json")
    with pytest.raises(TechnicalSnapshotError, match="has no indicator"):
        load_technical_snapshot_context(
            path,
            "000063",
            expected_trade_date="2026-08-19",
            expected_run_id="32207015938",
            expected_run_number="49",
        )


def test_missing_volume_ratio_remains_null_and_removes_volume_claims(tmp_path: Path) -> None:
    context = load_technical_snapshot_context(
        _write_snapshot(tmp_path / "technical.json"),
        "600378",
        expected_trade_date="2026-08-19",
        expected_run_id="32207015938",
        expected_run_number="49",
    )
    trend = StockTrendAnalyzer().analyze(_history(), "600378", technical_context=context)
    assert trend.volume_ratio_5d is None
    assert trend.provider_volume_ratio is None
    assert trend.completed_day_volume_ratio_5d == 0.92
    assert trend.volume_status is None
    assert trend.to_dict()["volume_ratio_5d"] is None

    result = _result()
    StockAnalysisPipeline._apply_screening_technical_result(result, trend)
    volume = result.dashboard["data_perspective"]["volume_analysis"]
    assert volume["volume_ratio"] is None
    assert volume["volume_status"] == "无法确认"
    assert result.volume_analysis == "量比缺失，无法确认盘中放量或缩量状态"
    assert "缩量洗盘" not in result.trend_analysis
    markdown = render("markdown", [result], report_date="2026-08-19")
    assert markdown is not None
    assert "量比 N/A" in markdown
    assert "量比 0.0" not in markdown


def test_valid_provider_volume_ratio_is_displayed(tmp_path: Path) -> None:
    payload = _snapshot()
    payload["indicators"]["600378"]["provider_volume_ratio"] = 1.36
    path = tmp_path / "technical.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    context = load_technical_snapshot_context(
        path,
        "600378",
        expected_trade_date="2026-08-19",
        expected_run_id="32207015938",
        expected_run_number="49",
    )
    trend = StockTrendAnalyzer().analyze(_history(), "600378", technical_context=context)
    assert trend.volume_ratio_5d == 1.36
    assert trend.volume_status is not None


def test_independent_analysis_keeps_realtime_augmentation(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = StockAnalysisPipeline.__new__(StockAnalysisPipeline)
    pipeline.config = SimpleNamespace(
        enable_realtime_quote=True,
        enable_realtime_technical_indicators=True,
    )
    pipeline.trend_analyzer = Mock()
    pipeline.trend_analyzer.analyze.return_value = Mock()
    augmented = _history()
    augment = Mock(return_value=augmented)
    pipeline._augment_historical_with_realtime = augment
    quote = SimpleNamespace(price=50.0)
    monkeypatch.delenv(TECHNICAL_SNAPSHOT_ENV, raising=False)

    pipeline._analyze_trend_with_run_context(_history(), quote, "600378")

    augment.assert_called_once()
    pipeline.trend_analyzer.analyze.assert_called_once_with(augmented, "600378")
