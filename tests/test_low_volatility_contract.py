from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import json
import math
from pathlib import Path
from statistics import stdev
import unittest

from research.benchmarks.low_volatility import (
    ANNUALIZATION_FACTOR,
    BENCHMARK_TOP_N,
    CORPORATE_ACTION_POLICY,
    MODEL_FAMILY,
    MODEL_NAME,
    MODEL_VARIANT,
    PRICE_BASIS_POLICY,
    REQUIRED_CLOSE_OBSERVATIONS,
    REQUIRED_RETURN_OBSERVATIONS,
    STD_DDOF,
    LowVolatilityResult,
    LowVolatilityStatus,
    create_model_identity,
    evaluate_history,
    rank_eligible,
    simple_daily_returns,
)
from research.benchmarks.schema import (
    BenchmarkModelIdentity,
    BenchmarkSignal,
    BenchmarkValidationError,
    canonical_json_bytes,
)
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
    universe_config_payload,
)
from src.services.market_screener import ScreeningConfig


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "low_volatility_phase2a.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _weekdays(start: str, count: int) -> list[date]:
    current = date.fromisoformat(start)
    values = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values


def _closes(dates: list[date], cycle: list[float]) -> list[dict]:
    close = 100.0
    values = [{"trade_date": dates[0].isoformat(), "close": close}]
    for index, trade_date in enumerate(dates[1:]):
        close *= 1.0 + cycle[index % len(cycle)]
        values.append({"trade_date": trade_date.isoformat(), "close": close})
    return values


class LowVolatilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _fixture()
        cls.window_dates = _weekdays(
            cls.fixture["synthetic_calendar_start"],
            REQUIRED_CLOSE_OBSERVATIONS,
        )
        cls.model = create_model_identity(
            generated_at=cls.fixture["generated_at"]
        )

    def _stock(self, case: str) -> dict:
        return next(item for item in self.fixture["stocks"] if item["case"] == case)

    def _evaluate(self, case: str, **overrides) -> LowVolatilityResult:
        stock = self._stock(case)
        dates = list(self.window_dates)
        close_count = stock.get("close_count", len(dates))
        expected_dates = dates[:close_count]
        bars = _closes(expected_dates, stock["return_cycle"])
        if "missing_index" in stock:
            bars.pop(stock["missing_index"])
        actions = []
        if "corporate_action_index" in stock:
            actions.append(
                {
                    "action_type": "cash_dividend_ex_date",
                    "known_at": "2026-03-20T15:00:00+08:00",
                    "trade_date": dates[stock["corporate_action_index"]].isoformat(),
                }
            )
        values = {
            "closes": bars,
            "corporate_action_data_as_of": self.fixture["source_data_as_of"],
            "corporate_action_reviewed": True,
            "corporate_action_source": "synthetic_phase2a_fixture",
            "corporate_actions": actions,
            "expected_trade_dates": expected_dates,
            "fetched_at": self.fixture["fetched_at"],
            "history_data_as_of": self.fixture["history_data_as_of"],
            "history_source": "synthetic_phase2a_fixture",
            "market_data_at": self.fixture["market_data_at"],
            "model": self.model,
            "price_basis": "raw_unadjusted",
            "previous_completed_trade_date": expected_dates[-1],
            "signal_date": self.fixture["signal_date"],
            "source_data_as_of": self.fixture["source_data_as_of"],
            "stock_code": stock["code"],
            "trade_calendar_source": "synthetic_weekday_fixture",
        }
        values.update(overrides)
        return evaluate_history(**values)

    def test_formal_model_identity_and_parameters_are_frozen(self) -> None:
        self.assertEqual(self.model.model_name, MODEL_NAME)
        self.assertEqual(self.model.model_family, MODEL_FAMILY)
        self.assertEqual(self.model.variant, MODEL_VARIANT)
        self.assertEqual(
            self.model.parameters,
            {
                "annualization_factor": ANNUALIZATION_FACTOR,
                "corporate_action_policy": CORPORATE_ACTION_POLICY,
                "lookback_returns": REQUIRED_RETURN_OBSERVATIONS,
                "price_basis_policy": PRICE_BASIS_POLICY,
                "ranking_direction": "ascending",
                "required_close_observations": REQUIRED_CLOSE_OBSERVATIONS,
                "return_type": "simple",
                "std_ddof": STD_DDOF,
                "top_n": BENCHMARK_TOP_N,
                "universe_config_hash": universe_config_hash(),
                "universe_contract_version": UNIVERSE_CONTRACT_VERSION,
            },
        )

    def test_61_closes_produce_60_simple_returns(self) -> None:
        result = self._evaluate("low")
        self.assertEqual(result.status, LowVolatilityStatus.ELIGIBLE)
        self.assertEqual(result.raw_metric["close_observations"], 61)
        self.assertEqual(result.raw_metric["return_observations"], 60)

    def test_simple_return_formula_and_sample_std_are_exact(self) -> None:
        closes = [100.0, 101.0, 99.99]
        returns = simple_daily_returns(closes)
        self.assertAlmostEqual(returns[0], 101.0 / 100.0 - 1.0, places=15)
        self.assertAlmostEqual(returns[1], 99.99 / 101.0 - 1.0, places=15)

        result = self._evaluate("medium")
        expected_returns = [0.01, -0.01] * 30
        self.assertAlmostEqual(
            result.raw_metric["volatility_daily_60d"],
            stdev(expected_returns),
            places=14,
        )
        self.assertAlmostEqual(
            result.raw_metric["volatility_annualized"],
            stdev(expected_returns) * math.sqrt(252),
            places=14,
        )

    def test_only_60_closes_is_insufficient_and_window_is_not_shortened(self) -> None:
        result = self._evaluate("insufficient")
        self.assertEqual(result.status, LowVolatilityStatus.INSUFFICIENT_HISTORY)
        self.assertIsNone(result.raw_metric)

    def test_missing_required_date_is_not_filled_from_earlier_extra_bar(self) -> None:
        stock = self._stock("missing_date")
        bars = _closes(self.window_dates, stock["return_cycle"])
        bars.pop(stock["missing_index"])
        earlier = self.window_dates[0] - timedelta(days=3)
        bars.insert(0, {"trade_date": earlier.isoformat(), "close": 99.0})
        result = self._evaluate("missing_date", closes=bars)
        self.assertEqual(result.status, LowVolatilityStatus.INSUFFICIENT_HISTORY)
        self.assertEqual(result.reasons, ("required_trade_date_missing",))

    def test_signal_day_close_and_non_prior_window_end_are_rejected(self) -> None:
        bars = _closes(self.window_dates, [0.001, -0.001])
        bars.append({"trade_date": self.fixture["signal_date"], "close": 100.0})
        with self.assertRaisesRegex(BenchmarkValidationError, "strictly earlier"):
            self._evaluate("low", closes=bars)
        dates = self.window_dates[:-1] + [date.fromisoformat(self.fixture["signal_date"])]
        with self.assertRaisesRegex(BenchmarkValidationError, "history_window_end"):
            self._evaluate(
                "low",
                expected_trade_dates=dates,
                previous_completed_trade_date=dates[-1],
            )

    def test_window_must_end_on_declared_previous_completed_trade_date(self) -> None:
        with self.assertRaisesRegex(
            BenchmarkValidationError, "previous_completed_trade_date"
        ):
            self._evaluate(
                "low",
                previous_completed_trade_date=self.window_dates[-2],
            )

    def test_point_in_time_and_fetch_audit_contracts_are_enforced(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "history_data_as_of"):
            self._evaluate(
                "low",
                source_data_as_of="2026-03-31T10:00:01+08:00",
            )
        fetched_later = self._evaluate(
            "low",
            fetched_at="2026-03-31T10:05:00+08:00",
        )
        self.assertEqual(fetched_later.status, LowVolatilityStatus.ELIGIBLE)

    def test_non_raw_price_basis_is_rejected(self) -> None:
        for basis in ("qfq", "hfq", "unknown"):
            with self.subTest(basis=basis), self.assertRaisesRegex(
                BenchmarkValidationError, "raw_unadjusted"
            ):
                self._evaluate("low", price_basis=basis)

    def test_corporate_action_requires_review_and_never_becomes_volatility(self) -> None:
        not_reviewed = self._evaluate("low", corporate_action_reviewed=False)
        action = self._evaluate("corporate_action")
        self.assertEqual(
            not_reviewed.status,
            LowVolatilityStatus.CORPORATE_ACTION_REVIEW,
        )
        self.assertEqual(action.status, LowVolatilityStatus.CORPORATE_ACTION_REVIEW)
        self.assertIsNone(action.raw_metric)

    def test_future_corporate_action_knowledge_is_rejected(self) -> None:
        action_date = self.window_dates[30].isoformat()
        with self.assertRaisesRegex(BenchmarkValidationError, "knowledge"):
            self._evaluate(
                "low",
                corporate_actions=[
                    {
                        "action_type": "split",
                        "known_at": "2026-04-01T09:00:00+08:00",
                        "trade_date": action_date,
                    }
                ],
            )

    def test_ranking_uses_daily_volatility_then_stock_code_only(self) -> None:
        results = [
            self._evaluate("high"),
            self._evaluate("medium"),
            self._evaluate("tie"),
            self._evaluate("low"),
        ]
        ranked = rank_eligible(results)
        self.assertEqual(
            [item.stock_code for item in ranked],
            ["000001", "000002", "600001", "600002"],
        )
        first = replace(
            ranked[0],
            raw_metric={
                **ranked[0].raw_metric,
                "volatility_annualized": 999.0,
            },
        )
        self.assertEqual(rank_eligible([ranked[1], first])[0].stock_code, "000001")

    def test_raw_metric_is_stable_json_and_result_is_deterministic(self) -> None:
        first = self._evaluate("low")
        second = self._evaluate("low")
        self.assertEqual(first, second)
        self.assertEqual(canonical_json_bytes(first.to_dict()), canonical_json_bytes(second.to_dict()))
        self.assertEqual(first.raw_metric["lookback_return_observations"], 60)
        self.assertEqual(first.raw_metric["window_end"], "2026-03-30")

    def test_score_none_remains_valid_for_synthetic_contract_signal(self) -> None:
        result = self._evaluate("low")
        signal = BenchmarkSignal.create(
            model=self.model,
            stock_code=result.stock_code,
            stock_name="合成测试",
            signal_date=self.fixture["signal_date"],
            market_data_at=self.fixture["market_data_at"],
            reference_price=10.0,
            rank=1,
            score=None,
            raw_metric=result.raw_metric,
            selection_reason="synthetic contract fixture only",
            source_data_as_of=self.fixture["source_data_as_of"],
            fetched_at=self.fixture["fetched_at"],
        )
        self.assertIsNone(signal.score)

    def test_universe_version_and_hash_are_stable_and_semantic(self) -> None:
        self.assertEqual(UNIVERSE_CONTRACT_VERSION, "v2_1_mainboard_v1")
        self.assertEqual(universe_config_hash(), universe_config_hash())
        payload = universe_config_payload()
        self.assertEqual(payload["contract_version"], UNIVERSE_CONTRACT_VERSION)
        changed = replace(ScreeningConfig(), min_amount_yuan=300_000_000.0)
        self.assertNotEqual(universe_config_hash(), universe_config_hash(changed))

    def test_universe_change_changes_model_id_but_display_time_does_not(self) -> None:
        later = create_model_identity(generated_at="2026-03-31T10:02:00+08:00")
        self.assertEqual(self.model.model_id, later.model_id)
        changed = create_model_identity(
            generated_at=self.fixture["generated_at"],
            universe_config=replace(
                ScreeningConfig(),
                min_turnover_pct=0.75,
            ),
        )
        self.assertNotEqual(self.model.model_id, changed.model_id)

    def test_any_critical_parameter_change_changes_model_identity(self) -> None:
        base = dict(self.model.parameters)
        for key, value in (
            ("return_type", "log"),
            ("lookback_returns", 20),
            ("std_ddof", 0),
            ("annualization_factor", 250),
            ("ranking_direction", "descending"),
            ("top_n", 10),
            ("price_basis_policy", "qfq"),
            ("corporate_action_policy", "ignore"),
            ("universe_contract_version", "v2_1_mainboard_v2"),
        ):
            parameters = {**base, key: value}
            changed = BenchmarkModelIdentity.create(
                model_name=self.model.model_name,
                model_version=self.model.model_version,
                model_family=self.model.model_family,
                variant=self.model.variant,
                calculation_version=self.model.calculation_version,
                parameters=parameters,
                generated_at=self.fixture["generated_at"],
            )
            with self.subTest(parameter=key):
                self.assertNotEqual(self.model.model_id, changed.model_id)


if __name__ == "__main__":
    unittest.main()
