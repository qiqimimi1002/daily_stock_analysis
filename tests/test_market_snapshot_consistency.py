from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from data_provider.base import DataFetcherManager
from data_provider.market_snapshot import (
    MARKET_SNAPSHOT_ENV,
    MarketSnapshotError,
    load_market_snapshot_quote,
)
from data_provider.realtime_types import RealtimeSource
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
