from types import SimpleNamespace
from unittest import TestCase

from src.report_evidence_policy import (
    attribution_weights_for_result,
    conservative_volume_meaning,
    market_snapshot_for_report,
    price_data_for_report,
    sanitize_action_text,
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
