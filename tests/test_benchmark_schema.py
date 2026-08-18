from __future__ import annotations

import json
import unittest
from zoneinfo import ZoneInfo

from research.benchmarks.schema import (
    BenchmarkModelIdentity,
    BenchmarkSignal,
    BenchmarkValidationError,
    serialize_signal_batch,
)


CN_TZ = ZoneInfo("Asia/Shanghai")


def _model(**overrides) -> BenchmarkModelIdentity:
    values = {
        "model_name": "Synthetic framework fixture",
        "model_version": "1.0.0",
        "model_family": "fixture",
        "variant": "original",
        "calculation_version": "benchmark-contract-1",
        "parameters": {"lookback_days": 20, "threshold": 0.5},
        "generated_at": "2026-08-18T10:01:00+08:00",
    }
    values.update(overrides)
    return BenchmarkModelIdentity.create(**values)


def _signal(model: BenchmarkModelIdentity | None = None, **overrides) -> BenchmarkSignal:
    values = {
        "model": model or _model(),
        "stock_code": "600100",
        "stock_name": "测试股票",
        "signal_date": "2026-08-18",
        "market_data_at": "2026-08-18T10:00:00+08:00",
        "reference_price": 12.34,
        "rank": 1,
        "score": None,
        "raw_metric": {"value": 0.125, "window": 20},
        "selection_reason": "synthetic contract fixture",
        "source_data_as_of": "2026-08-18T10:00:30+08:00",
    }
    values.update(overrides)
    return BenchmarkSignal.create(**values)


class BenchmarkIdentityTests(unittest.TestCase):
    def test_same_model_inputs_produce_same_model_id(self) -> None:
        self.assertEqual(_model().model_id, _model().model_id)

    def test_parameter_key_order_does_not_change_model_id(self) -> None:
        first = _model(parameters={"lookback_days": 20, "threshold": 0.5})
        second = _model(parameters={"threshold": 0.5, "lookback_days": 20})
        self.assertEqual(first.model_id, second.model_id)

    def test_critical_parameter_changes_model_id(self) -> None:
        self.assertNotEqual(
            _model(parameters={"lookback_days": 20}).model_id,
            _model(parameters={"lookback_days": 60}).model_id,
        )

    def test_model_version_changes_model_and_signal_ids(self) -> None:
        first = _model(model_version="1.0.0")
        second = _model(model_version="1.0.1")
        self.assertNotEqual(first.model_id, second.model_id)
        self.assertNotEqual(_signal(first).signal_id, _signal(second).signal_id)

    def test_low_volatility_and_momentum_families_cannot_collide(self) -> None:
        low_volatility = _model(model_family="low_volatility")
        momentum = _model(model_family="momentum_12_1")
        self.assertNotEqual(low_volatility.model_id, momentum.model_id)
        self.assertNotEqual(
            _signal(low_volatility).signal_id,
            _signal(momentum).signal_id,
        )

    def test_original_and_variant_have_distinct_identity(self) -> None:
        self.assertNotEqual(
            _model(variant="original").model_id,
            _model(variant="liquidity-neutral").model_id,
        )

    def test_noncritical_display_fields_do_not_change_identity(self) -> None:
        first = _model(model_name="Display A", generated_at="2026-08-18T10:01:00+08:00")
        second = _model(model_name="Display B", generated_at="2026-08-18T10:02:00+08:00")
        self.assertEqual(first.model_id, second.model_id)
        self.assertEqual(
            _signal(first, stock_name="名称甲", selection_reason="说明甲").signal_id,
            _signal(second, stock_name="名称乙", selection_reason="说明乙").signal_id,
        )

    def test_same_signal_inputs_produce_same_signal_id(self) -> None:
        self.assertEqual(_signal().signal_id, _signal().signal_id)

    def test_signal_identity_changes_for_stock_or_snapshot(self) -> None:
        baseline = _signal().signal_id
        self.assertNotEqual(baseline, _signal(stock_code="000100").signal_id)
        self.assertNotEqual(
            baseline,
            _signal(reference_price=12.35).signal_id,
        )


class BenchmarkSignalContractTests(unittest.TestCase):
    def test_times_are_normalized_to_asia_shanghai(self) -> None:
        model = _model(generated_at="2026-08-18T02:01:00Z")
        signal = _signal(
            model,
            market_data_at="2026-08-18T02:00:00+00:00",
            source_data_as_of="2026-08-18T02:00:30Z",
        )
        self.assertEqual(model.generated_at.tzinfo, CN_TZ)
        self.assertEqual(signal.market_data_at.isoformat(), "2026-08-18T10:00:00+08:00")
        self.assertEqual(signal.source_data_as_of.isoformat(), "2026-08-18T10:00:30+08:00")

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "timezone"):
            _model(generated_at="2026-08-18T10:01:00")

    def test_market_data_after_generation_is_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "market_data_at"):
            _signal(market_data_at="2026-08-18T10:02:00+08:00")

    def test_source_data_after_generation_is_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "source_data_as_of"):
            _signal(source_data_as_of="2026-08-18T10:02:00+08:00")

    def test_source_data_before_market_snapshot_is_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "earlier"):
            _signal(source_data_as_of="2026-08-18T09:59:59+08:00")

    def test_signal_date_must_match_market_date(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "signal_date"):
            _signal(signal_date="2026-08-17")

    def test_invalid_stock_code_and_date_are_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "six digits"):
            _signal(stock_code="SH600100")
        with self.assertRaisesRegex(BenchmarkValidationError, "ISO-8601 date"):
            _signal(signal_date="2026/08/18")

    def test_reference_price_and_rank_are_validated(self) -> None:
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.subTest(reference_price=invalid), self.assertRaises(
                BenchmarkValidationError
            ):
                _signal(reference_price=invalid)
        with self.assertRaisesRegex(BenchmarkValidationError, "rank"):
            _signal(rank=0)

    def test_score_is_nullable_and_raw_metric_is_preserved(self) -> None:
        signal = _signal(score=None, raw_metric={"volatility": 0.1234})
        self.assertIsNone(signal.score)
        self.assertEqual(signal.raw_metric, {"volatility": 0.1234})

    def test_non_finite_json_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "NaN"):
            _model(parameters={"bad": float("nan")})
        with self.assertRaisesRegex(BenchmarkValidationError, "Infinity"):
            _signal(raw_metric={"bad": float("inf")})

    def test_batch_json_is_stable_and_rank_sorted(self) -> None:
        model = _model()
        first = _signal(model, stock_code="600100", rank=2)
        second = _signal(model, stock_code="000100", rank=1)
        forward = serialize_signal_batch(model, [first, second])
        reverse = serialize_signal_batch(model, [second, first])
        self.assertEqual(forward, reverse)
        payload = json.loads(forward)
        self.assertEqual(
            [item["stock_code"] for item in payload["signals"]],
            ["000100", "600100"],
        )
        self.assertNotIn(b"NaN", forward)

    def test_empty_batch_is_valid_and_stable(self) -> None:
        model = _model()
        payload = json.loads(serialize_signal_batch(model, []))
        self.assertEqual(payload["signals"], [])
        self.assertEqual(payload["model"]["model_id"], model.model_id)

    def test_outcome_adapter_exposes_only_frozen_core_fields(self) -> None:
        core = _signal().to_outcome_signal_core()
        self.assertEqual(
            set(core),
            {
                "signal_id",
                "stock_code",
                "signal_date",
                "market_data_at",
                "reference_price",
            },
        )
        for forbidden in ("future_return", "max_drawdown", "win_rate"):
            self.assertNotIn(forbidden, core)


if __name__ == "__main__":
    unittest.main()
