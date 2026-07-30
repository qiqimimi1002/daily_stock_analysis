# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest

import pandas as pd

from src.services.market_scoring import (
    build_v21_scorecard,
    calculate_market_environment,
)
from src.services.market_screener import (
    MarketScreener,
    ScreeningConfig,
    normalize_spot_frame,
    render_markdown,
)


def _spot(**overrides):
    value = {
        "code": "600100",
        "name": "主板样本",
        "close": 24.0,
        "pct_change": 1.2,
        "volume": 1_000_000,
        "amount": 1_500_000_000,
        "turnover": 2.0,
        "volume_ratio": 1.2,
        "pe_ratio": 18.0,
        "pb_ratio": 2.0,
        "industry": "测试行业",
    }
    value.update(overrides)
    return value


def _metrics(**overrides):
    value = {
        "history_close": 24.0,
        "five_day_pct": 3.0,
        "ma5": 23.0,
        "ma10": 22.0,
        "ma20": 21.0,
        "volume_ratio_5d": 1.1,
        "is_intraday": True,
    }
    value.update(overrides)
    return value


def _evidence(**overrides):
    value = {
        "status": "ok",
        "growth": {
            "status": "ok",
            "data": {
                "revenue_yoy": 15.0,
                "net_profit_yoy": 20.0,
                "roe": 18.0,
                "gross_margin": 35.0,
                "debt_ratio": 40.0,
            },
        },
        "earnings": {
            "status": "ok",
            "data": {
                "financial_report": {
                    "net_profit_parent": 300.0,
                    "operating_cash_flow": 360.0,
                }
            },
        },
        "capital_flow": {
            "status": "ok",
            "data": {
                "stock_flow": {
                    "main_net_inflow": 10.0,
                    "inflow_5d": 20.0,
                    "inflow_10d": 30.0,
                }
            },
        },
        "valuation": {
            "status": "ok",
            "data": {"pe_ratio": 18.0, "pb_ratio": 2.0},
        },
    }
    value.update(overrides)
    return value


def _history(*, amount: float = 300_000_000.0) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=30, freq="B")
    return pd.DataFrame(
        {
            "日期": dates,
            "收盘": [18.0 + index * 6.0 / 29 for index in range(30)],
            "成交量": [1_000_000.0] * 29 + [1_100_000.0],
            "成交额": [amount] * 30,
        }
    )


class TestV21Scorecard(unittest.TestCase):
    def test_complete_evidence_increases_coverage_and_confidence(self) -> None:
        complete = build_v21_scorecard(_spot(), _metrics(), _evidence())
        missing = build_v21_scorecard(_spot(pe_ratio=None, pb_ratio=None), _metrics(), {})

        self.assertGreater(complete.coverage_pct, missing.coverage_pct)
        self.assertEqual(complete.confidence, "高")
        self.assertGreater(complete.score, missing.score)
        self.assertEqual(complete.components["fundamental"].max_score, 30.0)
        self.assertEqual(complete.components["industry_catalyst"].available_max, 0.0)
        self.assertEqual(complete.components["technical"].max_score, 20.0)

    def test_missing_evidence_is_reported_not_scored_as_positive(self) -> None:
        card = build_v21_scorecard(
            _spot(pe_ratio=None, pb_ratio=None),
            _metrics(),
            {},
        )
        self.assertEqual(card.components["fundamental"].available_max, 0.0)
        self.assertIn("基本面数据不完整", card.evidence_gaps)
        self.assertLess(card.coverage_pct, 75.0)

    def test_verified_loss_triggers_hard_reject(self) -> None:
        evidence = _evidence()
        evidence["earnings"]["data"]["financial_report"]["net_profit_parent"] = -10.0
        card = build_v21_scorecard(_spot(), _metrics(), evidence)
        self.assertTrue(card.hard_reject)
        self.assertTrue(any("净利润为负" in reason for reason in card.reject_reasons))

    def test_market_environment_score_uses_breadth(self) -> None:
        strong = calculate_market_environment(
            pd.DataFrame({"pct_change": [1.0, 2.0, 0.5, -0.1, 9.8]})
        )
        weak = calculate_market_environment(
            pd.DataFrame({"pct_change": [-1.0, -2.0, -0.5, 0.1, -9.8]})
        )
        self.assertGreater(strong["score"], weak["score"])
        self.assertIn("strategy", strong)


class TestSpotNormalization(unittest.TestCase):
    def test_sina_turnoverratio_alias_is_supported(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "code": "600100",
                    "name": "sample",
                    "close": 24.0,
                    "pct_change": 1.2,
                    "volume": 1_000_000,
                    "amount": 1_500_000_000,
                    "turnoverratio": 2.0,
                }
            ]
        )

        normalized = normalize_spot_frame(frame)

        self.assertEqual(normalized.loc[0, "turnover"], 2.0)


class TestV21ScreenerIntegration(unittest.TestCase):
    def test_screener_enriches_and_outputs_v21_fields(self) -> None:
        spot = pd.DataFrame(
            [
                [
                    "600100",
                    "主板样本",
                    24.0,
                    1.2,
                    1_000_000,
                    1_500_000_000,
                    2.0,
                    1.2,
                    18.0,
                    2.0,
                    "测试行业",
                ]
            ],
            columns=[
                "代码",
                "名称",
                "最新价",
                "涨跌幅",
                "成交量",
                "成交额",
                "换手率",
                "量比",
                "市盈率-动态",
                "市净率",
                "行业",
            ],
        )
        result = MarketScreener(
            ScreeningConfig(
                top_n=1,
                analysis_limit=1,
                preselect_limit=1,
                enrichment_limit=1,
                history_workers=1,
                evidence_workers=1,
            )
        ).run(
            spot_frame=spot,
            history_fetcher=lambda _: _history(),
            evidence_fetcher=lambda _: _evidence(),
        )

        self.assertEqual(result.model_version, "V2.1")
        self.assertEqual(result.evidence_success_count, 1)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertGreaterEqual(candidate.score_coverage_pct, 75.0)
        self.assertEqual(candidate.avg_amount_20d_yi, 3.0)
        self.assertIn("fundamental", candidate.score_breakdown)
        self.assertIn("industry_catalyst", candidate.score_breakdown)
        self.assertIsNone(candidate.historical_win_rate)
        report = render_markdown(result)
        self.assertIn("V2.1", report)
        self.assertIn("证据覆盖", report)
        self.assertIn("待V2.2积累", report)
        self.assertIn("关注触发条件", report)
        self.assertIn("放弃条件", report)
        self.assertIn("技术观察带", report)
        self.assertNotIn("建议买入", report)

    def test_twenty_day_average_amount_is_a_hard_liquidity_filter(self) -> None:
        spot = pd.DataFrame(
            [
                {
                    "代码": "600100",
                    "名称": "主板样本",
                    "最新价": 24.0,
                    "涨跌幅": 1.2,
                    "成交量": 1_000_000,
                    "成交额": 1_500_000_000,
                    "换手率": 2.0,
                }
            ]
        )
        result = MarketScreener(
            ScreeningConfig(
                top_n=1,
                analysis_limit=1,
                preselect_limit=1,
                enrichment_limit=1,
                history_workers=1,
                evidence_workers=1,
            )
        ).run(
            spot_frame=spot,
            history_fetcher=lambda _: _history(amount=100_000_000.0),
            evidence_fetcher=lambda _: _evidence(),
        )
        self.assertEqual(result.candidates, ())

    def test_weak_market_reduces_observation_list_to_three(self) -> None:
        rows = []
        for index in range(5):
            rows.append(
                {
                    "代码": f"60010{index}",
                    "名称": f"主板样本{index}",
                    "最新价": 24.0,
                    "涨跌幅": -1.0,
                    "成交量": 1_000_000,
                    "成交额": 1_500_000_000,
                    "换手率": 2.0,
                }
            )
        result = MarketScreener(
            ScreeningConfig(
                top_n=5,
                analysis_limit=3,
                preselect_limit=5,
                enrichment_limit=5,
                history_workers=1,
                evidence_workers=1,
            )
        ).run(
            spot_frame=pd.DataFrame(rows),
            history_fetcher=lambda _: _history(),
            evidence_fetcher=lambda _: _evidence(),
        )
        self.assertLess(result.market_environment["score"], 40.0)
        self.assertEqual(result.market_environment["observation_limit"], 3)
        self.assertEqual(len(result.candidates), 3)


if __name__ == "__main__":
    unittest.main()
