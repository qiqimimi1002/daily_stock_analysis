from types import SimpleNamespace
from unittest import TestCase

from src.report_evidence_policy import (
    attribution_weights_for_result,
    conservative_volume_meaning,
    market_snapshot_for_report,
    price_data_for_report,
    sanitize_action_text,
    signal_attribution_text_for_report,
)


class TestReportEvidencePolicyV4(TestCase):
    def _result(self, decision_type="hold", **kwargs):
        defaults = {
            "decision_type": decision_type,
            "operation_advice": "持有",
            "dashboard": {},
            "market_snapshot": {},
            "market_phase_summary": {},
            "analysis_context_pack_overview": {"metadata": {"news_result_count": 0}},
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_non_buy_text_removes_incremental_buy_language(self):
        result = self._result()
        text = sanitize_action_text(
            result,
            "观察1280元支撑，不破则维持买入评级；可低吸加仓。",
            "zh",
        )
        self.assertNotIn("买入", text)
        self.assertNotIn("低吸", text)
        self.assertNotIn("加仓", text)
        self.assertIn("持有观察", text)

    def test_non_buy_text_removes_layout_and_entry_language(self):
        result = self._result()
        text = sanitize_action_text(
            result,
            "禁止盲目追高，建议逢低布局；确认后再分批介入。",
            "zh",
        )
        self.assertNotIn("布局", text)
        self.assertNotIn("介入", text)
        self.assertEqual(text.count("持有观察"), 2)

    def test_buy_text_is_unchanged(self):
        result = self._result(decision_type="buy", operation_advice="买入")
        original = "回调可分批建仓"
        self.assertEqual(sanitize_action_text(result, original, "zh"), original)

    def test_non_buy_immediate_action_waits_for_confirmation(self):
        result = self._result()
        self.assertEqual(
            sanitize_action_text(result, "立即行动", "zh"),
            "等待确认",
        )

    def test_overconfident_and_volume_pressure_claims_are_neutralized(self):
        result = self._result()
        confidence = sanitize_action_text(
            result,
            "技术形态完美，量价配合理想，且符合交易准则。",
            "zh",
        )
        self.assertNotIn("完美", confidence)
        self.assertNotIn("理想", confidence)
        self.assertNotIn("符合交易准则", confidence)
        self.assertIn("方向仍待确认", confidence)
        self.assertNotIn("，，", confidence)

        pressure = sanitize_action_text(
            result,
            "盘中量比过大可能隐含短期抛压",
            "zh",
        )
        self.assertNotIn("隐含短期抛压", pressure)
        self.assertIn("买卖压力待确认", pressure)

    def test_adjacent_buy_terms_collapse_to_one_hold_phrase(self):
        result = self._result()
        self.assertEqual(
            sanitize_action_text(result, "低吸加仓", "zh"),
            "持有观察",
        )

    def test_remaining_unverified_report_claims_are_neutralized(self):
        result = self._result()
        cases = {
            "MA5 > MA10 > MA20 完美多头排列": "MA5 > MA10 > MA20 多头排列",
            "新闻及公告数据近期真空": "新闻及公告未完成有效检索",
            "暂无显著看空信号": "负面信号未完成充分核查",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(
                    sanitize_action_text(result, original, "zh"),
                    expected,
                )

    def test_market_snapshot_arithmetic_is_recomputed(self):
        result = self._result(
            market_snapshot={
                "close": "1293.42",
                "prev_close": "1292.01",
                "change_amount": "-4.00",
                "pct_chg": "-0.31%",
            }
        )
        snapshot = market_snapshot_for_report(result)
        self.assertEqual(snapshot["change_amount"], "+1.41")
        self.assertEqual(snapshot["pct_chg"], "+0.11%")

    def test_intraday_price_table_uses_snapshot_quote(self):
        result = self._result(
            market_snapshot={"close": "1293.42", "prev_close": "1292.01"},
            market_phase_summary={"phase": "intraday"},
        )
        price_data = price_data_for_report(result, {"current_price": "1288.00"})
        self.assertEqual(price_data["current_price"], "1293.42")

    def test_volume_wording_is_neutral(self):
        text = conservative_volume_meaning(
            {"volume_ratio": 0.62, "volume_meaning": "抛压极轻，健康洗盘"},
            "zh",
        )
        self.assertNotIn("抛压", text)
        self.assertNotIn("洗盘", text)
        self.assertIn("不能单独据此判断", text)

    def test_attribution_weights_hidden_when_news_unverified(self):
        result = self._result(
            dashboard={
                "data_perspective": {"trend_status": {"trend_score": 80}},
                "financial_summary": {"pe": 20},
            }
        )
        weights = attribution_weights_for_result(
            result,
            {"technical_indicators": 50, "news_sentiment": 20},
        )
        self.assertEqual(weights, [])

    def test_verified_technical_signal_is_preserved_without_weights(self):
        result = self._result()
        self.assertEqual(
            signal_attribution_text_for_report(result, "MACD金叉", "zh"),
            "MACD金叉",
        )

    def test_missing_chip_directional_claim_is_suppressed(self):
        result = self._result()
        self.assertEqual(
            signal_attribution_text_for_report(
                result,
                "筹码集中度未知导致的潜在抛压风险",
                "zh",
            ),
            "",
        )

    def test_volume_only_directional_claim_is_suppressed(self):
        result = self._result()
        self.assertEqual(
            signal_attribution_text_for_report(
                result,
                "量能上攻动能稍显不足",
                "zh",
            ),
            "",
        )
