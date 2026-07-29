# -*- coding: utf-8 -*-

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.services.market_screener import (
    MarketScreener,
    ScreeningConfig,
    apply_spot_filters,
    calculate_history_metrics,
    is_excluded_name,
    is_main_board_code,
    render_markdown,
    save_result,
)


def _history(last_close: float = 24.0, volume_ratio: float = 1.1) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=30, freq="B")
    closes = [18.0 + index * (last_close - 18.0) / 29 for index in range(30)]
    volumes = [1_000_000.0] * 29 + [1_000_000.0 * volume_ratio]
    return pd.DataFrame({"日期": dates, "收盘": closes, "成交量": volumes})


class TestBoardRules(unittest.TestCase):
    def test_keeps_only_shanghai_and_shenzhen_main_board(self) -> None:
        for code in ("600519", "601318", "603259", "605499", "000001", "001979", "002594", "003816"):
            self.assertTrue(is_main_board_code(code), code)
        for code in ("300750", "301001", "688001", "689009", "920001", "830001", "900901", "200002"):
            self.assertFalse(is_main_board_code(code), code)

    def test_excludes_risk_and_new_listing_names(self) -> None:
        for name in ("ST测试", "*ST测试", "退市测试", "N测试", "C测试"):
            self.assertTrue(is_excluded_name(name), name)
        self.assertFalse(is_excluded_name("贵州茅台"))


class TestSpotFilters(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ScreeningConfig(top_n=2, preselect_limit=4, analysis_limit=2)

    def test_hard_filters_are_applied(self) -> None:
        frame = pd.DataFrame(
            [
                ["600519", "贵州茅台", 1300, 1.2, 100, 2_000_000_000, 1.1],
                ["000001", "平安银行", 12, 0.8, 100, 800_000_000, 2.0],
                ["300750", "创业板样本", 200, 1.0, 100, 2_000_000_000, 2.0],
                ["600001", "ST样本", 10, 1.0, 100, 2_000_000_000, 2.0],
                ["600002", "低流动性", 10, 1.0, 100, 20_000_000, 2.0],
                ["600003", "追涨样本", 10, 8.0, 100, 2_000_000_000, 2.0],
            ],
            columns=["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额", "换手率"],
        )
        filtered = apply_spot_filters(frame, self.config)
        self.assertEqual(set(filtered["code"]), {"600519", "000001"})


class TestHistoryAndRanking(unittest.TestCase):
    def test_history_metrics(self) -> None:
        metrics = calculate_history_metrics(_history(), min_rows=20)
        self.assertIsNotNone(metrics)
        assert metrics is not None
        self.assertGreater(metrics["ma5"], metrics["ma20"])
        self.assertAlmostEqual(metrics["volume_ratio_5d"], 1.1, places=2)

    def test_end_to_end_with_injected_data(self) -> None:
        spot = pd.DataFrame(
            [
                ["600100", "主板甲", 24.0, 1.2, 100, 1_500_000_000, 2.0],
                ["000100", "主板乙", 20.0, 0.5, 100, 900_000_000, 1.5],
                ["688100", "科创样本", 50.0, 1.0, 100, 2_000_000_000, 2.0],
            ],
            columns=["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额", "换手率"],
        )
        config = ScreeningConfig(
            top_n=2,
            analysis_limit=1,
            preselect_limit=2,
            history_workers=1,
        )
        result = MarketScreener(config=config).run(
            spot_frame=spot,
            history_fetcher=lambda code: _history(24.0 if code == "600100" else 20.0),
        )
        self.assertEqual(result.universe_count, 3)
        self.assertEqual(result.spot_filtered_count, 2)
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(len(result.analysis_codes), 1)
        self.assertNotIn("688100", result.analysis_codes)

    def test_report_is_observation_not_buy_advice(self) -> None:
        spot = pd.DataFrame(
            [["600100", "主板甲", 24.0, 1.2, 100, 1_500_000_000, 2.0]],
            columns=["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额", "换手率"],
        )
        result = MarketScreener(
            ScreeningConfig(
                top_n=1,
                analysis_limit=1,
                preselect_limit=1,
                history_workers=1,
            )
        ).run(spot_frame=spot, history_fetcher=lambda _: _history())
        report = render_markdown(result)
        self.assertIn("观察候选", report)
        self.assertIn("不代表买入、加仓或建仓建议", report)
        self.assertNotIn("建议买入", report)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_result(
                result,
                report_path=root / "reports" / "screen.md",
                json_path=root / "data" / "screen.json",
                codes_path=root / "data" / "codes.txt",
            )
            self.assertEqual(
                (root / "data" / "codes.txt").read_text(encoding="utf-8"),
                result.analysis_codes[0],
            )


if __name__ == "__main__":
    unittest.main()

