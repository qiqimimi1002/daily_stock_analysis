# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from src.services.market_screener import (
    HistoryFetchError,
    MarketScreener,
    PublicMarketDataSource,
    ScreeningConfig,
    classify_history_failure,
)


def _history(last_close: float = 24.0) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=30, freq="B")
    closes = [18.0 + index * (last_close - 18.0) / 29 for index in range(30)]
    return pd.DataFrame(
        {
            "日期": dates,
            "收盘": closes,
            "成交量": [1_000_000.0] * 30,
            "成交额": [300_000_000.0] * 30,
        }
    )


class TestHistoryProviderReliability(unittest.TestCase):
    def test_same_backend_disconnect_retries_then_uses_independent_sina(self) -> None:
        delays: list[float] = []
        source = PublicMarketDataSource(sleep=delays.append)
        disconnect = ConnectionError("Remote end closed connection without response")
        with (
            patch.object(source, "_fetch_history_akshare_em", side_effect=disconnect) as em,
            patch.object(source, "_fetch_history_akshare_sina", return_value=_history()) as sina,
            patch.object(source, "_fetch_history_efinance", return_value=_history()) as efinance,
        ):
            result = source.fetch_history_with_meta("600519", max_retries=1)

        self.assertEqual(len(result.frame), 30)
        self.assertEqual(result.metadata["selected_source"], "akshare_sina")
        self.assertEqual(result.metadata["consistency"]["status"], "single_backend")
        self.assertEqual(delays, [0.5])
        self.assertEqual(em.call_count, 2)
        sina.assert_called_once()
        efinance.assert_not_called()
        providers = {item["provider"]: item for item in result.metadata["providers"]}
        self.assertEqual(providers["akshare_eastmoney"]["failure_reason"], "remote_disconnect")
        self.assertEqual(providers["akshare_eastmoney"]["attempts"], 2)
        self.assertEqual(providers["efinance_eastmoney"]["status"], "skipped")
        self.assertEqual(
            providers["efinance_eastmoney"]["failure_reason"],
            "same_backend_unavailable",
        )

    def test_non_transient_field_error_is_not_retried(self) -> None:
        delays: list[float] = []
        source = PublicMarketDataSource(sleep=delays.append)
        invalid = pd.DataFrame({"date": ["2026-08-01"], "close": [10.0]})
        with (
            patch.object(source, "_fetch_history_akshare_em", return_value=invalid) as em,
            patch.object(source, "_fetch_history_akshare_sina", return_value=_history()),
            patch.object(source, "_fetch_history_efinance", return_value=_history()),
        ):
            result = source.fetch_history_with_meta("000001", max_retries=2)

        self.assertEqual(em.call_count, 1)
        self.assertEqual(delays, [])
        providers = {item["provider"]: item for item in result.metadata["providers"]}
        self.assertEqual(providers["akshare_eastmoney"]["failure_reason"], "field_missing")

    def test_repeated_request_reuses_cache(self) -> None:
        source = PublicMarketDataSource(sleep=lambda _: None)
        with (
            patch.object(source, "_fetch_history_akshare_em", return_value=_history()) as em,
            patch.object(source, "_fetch_history_akshare_sina", return_value=_history()) as sina,
        ):
            first = source.fetch_history_with_meta("600519", max_retries=0)
            second = source.fetch_history_with_meta("600519", max_retries=0)

        self.assertFalse(first.metadata["cache_hit"])
        self.assertTrue(second.metadata["cache_hit"])
        self.assertEqual(em.call_count, 1)
        self.assertEqual(sina.call_count, 1)

    def test_cache_does_not_bypass_stricter_minimum_rows(self) -> None:
        source = PublicMarketDataSource(sleep=lambda _: None)
        with (
            patch.object(source, "_fetch_history_akshare_em", return_value=_history()) as em,
            patch.object(source, "_fetch_history_akshare_sina", return_value=_history()) as sina,
            patch.object(source, "_fetch_history_efinance", return_value=_history()) as efinance,
        ):
            source.fetch_history_with_meta("600519", min_rows=20, max_retries=0)
            with self.assertRaises(HistoryFetchError):
                source.fetch_history_with_meta("600519", min_rows=31, max_retries=0)

        self.assertEqual(em.call_count, 2)
        self.assertEqual(sina.call_count, 2)
        self.assertEqual(efinance.call_count, 1)

    def test_cross_source_price_conflict_is_rejected(self) -> None:
        source = PublicMarketDataSource(sleep=lambda _: None)
        with (
            patch.object(source, "_fetch_history_akshare_em", return_value=_history(24.0)),
            patch.object(source, "_fetch_history_akshare_sina", return_value=_history(30.0)),
        ):
            with self.assertRaises(HistoryFetchError) as raised:
                source.fetch_history_with_meta(
                    "600519",
                    max_retries=0,
                    consistency_tolerance_pct=2.0,
                )

        self.assertEqual(raised.exception.diagnostics["failure_reason"], "data_conflict")
        self.assertEqual(raised.exception.diagnostics["consistency"]["status"], "conflict")

    def test_failure_categories_are_stable(self) -> None:
        self.assertEqual(classify_history_failure(TimeoutError("timed out")), "timeout")
        self.assertEqual(
            classify_history_failure(ValueError("insufficient_history: rows=10")),
            "insufficient_history",
        )
        self.assertEqual(
            classify_history_failure(ValueError("数据缺少必要字段: volume")),
            "field_missing",
        )


class TestScreeningHistoryQuality(unittest.TestCase):
    @staticmethod
    def _spot() -> pd.DataFrame:
        return pd.DataFrame(
            [["600100", "主板甲", 24.0, 1.2, 100, 1_500_000_000, 2.0]],
            columns=["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额", "换手率"],
        )

    def test_run_aggregates_provider_success_rate_and_consistency(self) -> None:
        source = PublicMarketDataSource(sleep=lambda _: None)
        with (
            patch.object(source, "_fetch_history_akshare_em", return_value=_history()),
            patch.object(source, "_fetch_history_akshare_sina", return_value=_history()),
        ):
            result = MarketScreener(
                ScreeningConfig(
                    top_n=1,
                    analysis_limit=1,
                    preselect_limit=1,
                    history_workers=1,
                ),
                data_source=source,
            ).run(spot_frame=self._spot())

        self.assertEqual(result.history_success_rate, 100.0)
        self.assertEqual(result.history_data_quality["status"], "ok")
        self.assertEqual(result.history_source_stats["akshare_eastmoney"]["success_rate"], 100.0)
        self.assertEqual(result.history_source_stats["akshare_sina"]["success_rate"], 100.0)
        self.assertEqual(result.history_consistency["checked_count"], 1)

    def test_low_coverage_is_structured_without_filling_missing_history(self) -> None:
        spot = pd.DataFrame(
            [
                ["600100", "主板甲", 24.0, 1.2, 100, 1_500_000_000, 2.0],
                ["000100", "主板乙", 20.0, 0.5, 100, 900_000_000, 1.5],
            ],
            columns=["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额", "换手率"],
        )

        def fetch(code: str) -> pd.DataFrame:
            if code == "000100":
                raise ConnectionError("Remote end closed connection without response")
            return _history()

        result = MarketScreener(
            ScreeningConfig(
                top_n=2,
                analysis_limit=1,
                preselect_limit=2,
                history_workers=1,
            )
        ).run(spot_frame=spot, history_fetcher=fetch)

        self.assertEqual(result.history_success_count, 1)
        self.assertEqual(result.history_failure_count, 1)
        self.assertEqual(result.history_success_rate, 50.0)
        self.assertEqual(result.history_data_quality["status"], "insufficient")
        self.assertEqual(result.history_data_quality["confidence_label"], "low")
        self.assertEqual(result.history_failure_reasons["counts"], {"remote_disconnect": 1})
        self.assertEqual([candidate.code for candidate in result.candidates], ["600100"])
        self.assertTrue(any("不会放宽原筛选标准" in item for item in result.limitations))


if __name__ == "__main__":
    unittest.main()
