from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import math
from statistics import stdev
import unittest

from research.benchmarks.raw_history import (
    CROSS_ADJUSTMENT,
    CROSS_RAW_SOURCE_ID,
    PRIMARY_ADJUSTMENT,
    PRIMARY_RAW_SOURCE_ID,
    RawDailyBar,
    RawHistoryAcceptance,
    RawHistoryObservation,
    evaluate_raw_history,
)
from research.benchmarks.schema import BenchmarkValidationError, canonical_json_bytes
from research.benchmarks.short_term import (
    AMOUNT_ROLE,
    BENCHMARK_TOP_N,
    OUTCOME_HORIZONS,
    REQUIRED_CLOSE_OBSERVATIONS,
    AblationFactorStatus,
    ShortTermResult,
    ShortTermStatus,
    breakout_strength_20,
    create_model_identity,
    create_signals,
    evaluate_ablation_factors,
    evaluate_history,
    outcome_handoff,
    period_return,
    rank_eligible,
    vol_contraction_10_60,
    volume_ratio_5,
)
from research.benchmarks.trade_calendar import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    CalendarSourceObservation,
    HistoryWindowContract,
    VerifiedTradeCalendar,
)


def _weekdays(start: str, count: int) -> list[date]:
    current = date.fromisoformat(start)
    values = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values


def _iso(day: date, clock: str) -> str:
    return f"{day.isoformat()}T{clock}+08:00"


class ShortTermContractTests(unittest.TestCase):
    """All dates, prices, volumes and symbols below are fictional fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.calendar_dates = _weekdays("2025-11-24", 62)
        cls.signal_date = cls.calendar_dates[-1]
        cls.factor_dates = cls.calendar_dates[-62:-1]
        cls.window_dates = cls.factor_dates[-REQUIRED_CLOSE_OBSERVATIONS:]
        cls.market_data_at = _iso(cls.signal_date, "10:00:00")
        cls.source_data_as_of = _iso(cls.signal_date, "09:30:00")
        cls.generated_at = _iso(cls.signal_date, "10:01:00")
        calendar_observation = {
            "query_start": cls.calendar_dates[0],
            "query_end": cls.signal_date,
            "trading_dates": cls.calendar_dates,
            "source_data_as_of": _iso(cls.signal_date, "08:00:00"),
            "fetched_at": _iso(cls.signal_date, "08:00:01"),
        }
        cls.calendar = VerifiedTradeCalendar.create(
            query_start=cls.calendar_dates[0],
            query_end=cls.signal_date,
            primary=CalendarSourceObservation(
                source_id=PRIMARY_SOURCE_ID,
                **calendar_observation,
            ),
            cross=CalendarSourceObservation(
                source_id=CROSS_SOURCE_ID,
                **calendar_observation,
            ),
        )
        cls.window = HistoryWindowContract.create(
            calendar=cls.calendar,
            market_data_at=cls.market_data_at,
            history_data_as_of=_iso(cls.window_dates[-1], "15:00:00"),
            source_data_as_of=cls.source_data_as_of,
            fetched_at=_iso(cls.signal_date, "09:20:00"),
            generated_at=cls.generated_at,
            required_observations=REQUIRED_CLOSE_OBSERVATIONS,
            observed_trade_dates=cls.window_dates,
        )
        cls.factor_window = HistoryWindowContract.create(
            calendar=cls.calendar,
            market_data_at=cls.market_data_at,
            history_data_as_of=_iso(cls.factor_dates[-1], "15:00:00"),
            source_data_as_of=cls.source_data_as_of,
            fetched_at=_iso(cls.signal_date, "09:20:00"),
            generated_at=cls.generated_at,
            required_observations=61,
            observed_trade_dates=cls.factor_dates,
        )
        cls.model = create_model_identity(generated_at=cls.generated_at)

    def _closes(self, *, ret_20: float = 0.10, ret_5: float = 0.03) -> list[float]:
        values = [95.0 + (index % 7) * 0.13 for index in range(40)]
        anchor_20 = 100.0
        latest = anchor_20 * (1.0 + ret_20)
        anchor_5 = latest / (1.0 + ret_5)
        middle = [
            anchor_20 + (anchor_5 - anchor_20) * index / 15.0
            for index in range(16)
        ]
        tail = [
            anchor_5 + (latest - anchor_5) * index / 5.0
            for index in range(1, 6)
        ]
        return values + middle + tail

    def _raw_evidence(
        self,
        code: str,
        closes: list[float],
        volumes: list[int] | None = None,
    ) -> tuple[RawHistoryObservation, RawHistoryAcceptance]:
        volumes = volumes or [1_000_000 + index * 1_000 for index in range(61)]
        bars = []
        for trade_date, close, volume in zip(self.factor_dates, closes, volumes):
            price = Decimal(str(close))
            bars.append(
                RawDailyBar.create(
                    trade_date=trade_date,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=volume,
                    amount=price * Decimal(volume),
                    is_trading=True,
                )
            )
        common = {
            "symbol": code,
            "requested_start": self.factor_dates[0],
            "requested_end": self.factor_dates[-1],
            "fetched_at": _iso(self.signal_date, "09:15:00"),
            "price_basis": "raw_unadjusted",
            "volume_unit": "share",
            "amount_unit": "CNY",
            "bars": bars,
        }
        primary = RawHistoryObservation.create(
            source_id=PRIMARY_RAW_SOURCE_ID,
            adjustment=PRIMARY_ADJUSTMENT,
            **common,
        )
        cross = RawHistoryObservation.create(
            source_id=CROSS_RAW_SOURCE_ID,
            adjustment=CROSS_ADJUSTMENT,
            **common,
        )
        acceptance = evaluate_raw_history(
            calendar=self.calendar,
            request_at=_iso(self.signal_date, "09:00:00"),
            market_data_at=self.market_data_at,
            primary=primary,
            cross=cross,
        )
        return primary, acceptance

    def _evaluate(
        self,
        code: str = "600001",
        *,
        ret_20: float = 0.10,
        ret_5: float = 0.03,
        **overrides,
    ) -> ShortTermResult:
        primary, acceptance = self._raw_evidence(
            code,
            self._closes(ret_20=ret_20, ret_5=ret_5),
        )
        values = {
            "corporate_action_data_as_of": self.source_data_as_of,
            "corporate_action_reviewed": True,
            "corporate_action_source": "synthetic_dual_source_review",
            "corporate_actions": [],
            "history_acceptance": acceptance,
            "history_observation": primary,
            "history_window": self.window,
            "model": self.model,
            "stock_code": code,
        }
        values.update(overrides)
        return evaluate_history(**values)

    @staticmethod
    def _changed_manifest(
        acceptance: RawHistoryAcceptance,
        **changes,
    ) -> RawHistoryAcceptance:
        payload = dict(acceptance.manifest)
        payload.pop("manifest_sha256")
        payload.update(changes)
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        return RawHistoryAcceptance({**payload, "manifest_sha256": digest})

    def test_model_identity_freezes_only_the_simple_main_rule(self) -> None:
        self.assertEqual(self.model.parameters["trend_lookback_sessions"], 20)
        self.assertEqual(self.model.parameters["ranking_lookback_sessions"], 5)
        self.assertEqual(self.model.parameters["trend_gate"], "ret_20_strictly_greater_than_zero")
        self.assertEqual(self.model.parameters["ranking_direction"], "descending")
        self.assertEqual(self.model.parameters["required_close_observations"], 21)
        self.assertEqual(self.model.parameters["top_n"], BENCHMARK_TOP_N)
        self.assertEqual(self.model.parameters["amount_role"], AMOUNT_ROLE)
        for forbidden in ("rsi", "macd", "industry", "market_cap", "stop_loss"):
            self.assertNotIn(forbidden, self.model.parameters)

    def test_t_minus_one_cutoff_and_exact_return_formulas(self) -> None:
        result = self._evaluate(ret_20=0.12, ret_5=0.04)
        self.assertEqual(result.status, ShortTermStatus.ELIGIBLE)
        self.assertLess(date.fromisoformat(result.raw_metric["window_end"]), self.signal_date)
        self.assertAlmostEqual(result.raw_metric["ret_20"], 0.12, places=14)
        self.assertAlmostEqual(result.raw_metric["ret_5"], 0.04, places=14)
        self.assertEqual(result.raw_metric["price_basis"], "raw_unadjusted")

    def test_even_a_completed_t_bar_is_rejected_for_this_model(self) -> None:
        after_close = _iso(self.signal_date, "15:10:00")
        generated_at = _iso(self.signal_date, "15:11:00")
        window = HistoryWindowContract.create(
            calendar=self.calendar,
            market_data_at=after_close,
            history_data_as_of=_iso(self.signal_date, "15:05:00"),
            source_data_as_of=_iso(self.signal_date, "15:06:00"),
            fetched_at=None,
            generated_at=generated_at,
            required_observations=REQUIRED_CLOSE_OBSERVATIONS,
            observed_trade_dates=self.calendar_dates[-REQUIRED_CLOSE_OBSERVATIONS:],
        )
        with self.assertRaisesRegex(BenchmarkValidationError, "only through T-1"):
            self._evaluate(
                history_window=window,
                model=create_model_identity(generated_at=generated_at),
            )

    def test_trend_gate_is_strictly_positive(self) -> None:
        zero = self._evaluate(code="600002", ret_20=0.0, ret_5=0.02)
        negative = self._evaluate(code="600003", ret_20=-0.01, ret_5=0.02)
        self.assertEqual(zero.status, ShortTermStatus.TREND_FILTERED)
        self.assertEqual(negative.status, ShortTermStatus.TREND_FILTERED)
        self.assertEqual(zero.reasons, ("ret_20_not_positive",))

    def test_five_day_ranking_and_tie_break_are_stable(self) -> None:
        results = [
            self._evaluate(code="600003", ret_5=0.01),
            self._evaluate(code="000002", ret_5=0.05),
            self._evaluate(code="000001", ret_5=0.05),
            self._evaluate(code="600004", ret_20=-0.01, ret_5=0.50),
        ]
        ranked = rank_eligible(results)
        self.assertEqual(
            [item.stock_code for item in ranked],
            ["000001", "000002", "600003"],
        )

    def test_insufficient_window_is_not_shortened(self) -> None:
        short_dates = self.window_dates[-20:]
        short_window = HistoryWindowContract.create(
            calendar=self.calendar,
            market_data_at=self.market_data_at,
            history_data_as_of=_iso(self.window_dates[-1], "15:00:00"),
            source_data_as_of=self.source_data_as_of,
            fetched_at=None,
            generated_at=self.generated_at,
            required_observations=20,
            observed_trade_dates=short_dates,
        )
        result = self._evaluate(history_window=short_window)
        self.assertEqual(result.status, ShortTermStatus.INSUFFICIENT_HISTORY)
        self.assertIsNone(result.raw_metric)

    def test_missing_duplicate_unsorted_and_future_rows_fail_closed(self) -> None:
        primary, acceptance = self._raw_evidence("600001", self._closes())
        cases = {
            "missing": replace(primary, bars=primary.bars[:-1]),
            "duplicate": replace(primary, bars=primary.bars + (primary.bars[-1],)),
            "unsorted": replace(primary, bars=tuple(reversed(primary.bars))),
            "future": replace(
                primary,
                bars=primary.bars[:-1]
                + (replace(primary.bars[-1], trade_date=self.signal_date),),
            ),
        }
        patterns = {
            "missing": "missing",
            "duplicate": "duplicate",
            "unsorted": "strictly increasing",
            "future": "future",
        }
        for name, observation in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(
                BenchmarkValidationError,
                patterns[name],
            ):
                self._evaluate(
                    history_observation=observation,
                    history_acceptance=acceptance,
                )

    def test_nonfinite_price_and_adjusted_basis_fail_closed(self) -> None:
        primary, acceptance = self._raw_evidence("600001", self._closes())
        invalid_bar = replace(primary.bars[-1], close=float("nan"))
        with self.assertRaisesRegex(BenchmarkValidationError, "finite positive"):
            self._evaluate(
                history_observation=replace(
                    primary,
                    bars=primary.bars[:-1] + (invalid_bar,),
                ),
                history_acceptance=acceptance,
            )
        for basis in ("qfq", "hfq"):
            with self.subTest(basis=basis), self.assertRaisesRegex(
                BenchmarkValidationError,
                "raw_unadjusted",
            ):
                self._evaluate(
                    history_observation=replace(primary, price_basis=basis),
                    history_acceptance=acceptance,
                )

    def test_current_snapshot_backfill_is_not_a_model_vintage(self) -> None:
        primary, acceptance = self._raw_evidence("600001", self._closes())
        backfill = self._changed_manifest(
            acceptance,
            acquisition_mode="backfill_current_snapshot",
        )
        with self.assertRaisesRegex(BenchmarkValidationError, "prospective"):
            self._evaluate(
                history_observation=primary,
                history_acceptance=backfill,
            )

    def test_corporate_action_review_blocks_metric_and_ranking(self) -> None:
        not_reviewed = self._evaluate(corporate_action_reviewed=False)
        action = self._evaluate(
            corporate_actions=[
                {
                    "action_type": "rights_issue",
                    "effective_date": self.window_dates[-10],
                    "known_at": _iso(self.window_dates[-11], "15:00:00"),
                    "review_status": "review_required",
                }
            ]
        )
        self.assertEqual(not_reviewed.status, ShortTermStatus.CORPORATE_ACTION_REVIEW)
        self.assertEqual(action.status, ShortTermStatus.CORPORATE_ACTION_REVIEW)
        self.assertIsNone(action.raw_metric)
        self.assertEqual(rank_eligible([not_reviewed, action]), [])

    def test_future_known_corporate_action_is_lookahead(self) -> None:
        with self.assertRaisesRegex(BenchmarkValidationError, "knowledge"):
            self._evaluate(
                corporate_actions=[
                    {
                        "action_type": "cash_dividend",
                        "effective_date": self.window_dates[-10],
                        "known_at": _iso(self.signal_date + timedelta(days=1), "09:00:00"),
                    }
                ]
            )

    def test_three_ablation_factor_formulas_are_independent(self) -> None:
        daily_returns = [0.01, -0.005] * 25 + [0.003, -0.001] * 5
        closes = [100.0]
        for value in daily_returns:
            closes.append(closes[-1] * (1.0 + value))
        volumes = [100.0] * 55 + [100.0, 110.0, 90.0, 100.0, 100.0, 150.0]
        self.assertAlmostEqual(
            vol_contraction_10_60(closes),
            stdev(daily_returns[-10:]) / stdev(daily_returns),
            places=13,
        )
        self.assertAlmostEqual(
            breakout_strength_20(closes),
            closes[-1] / max(closes[-21:-1]) - 1.0,
            places=14,
        )
        self.assertAlmostEqual(volume_ratio_5(volumes), 1.5, places=14)
        self.assertAlmostEqual(period_return(closes, 5), closes[-1] / closes[-6] - 1.0)

        primary, acceptance = self._raw_evidence(
            "600001",
            closes,
            [int(item * 10_000) for item in volumes],
        )
        factors = evaluate_ablation_factors(
            model=self.model,
            stock_code="600001",
            history_window=self.factor_window,
            history_observation=primary,
            history_acceptance=acceptance,
            corporate_action_reviewed=True,
            corporate_action_source="synthetic_dual_source_review",
            corporate_action_data_as_of=self.source_data_as_of,
            corporate_actions=[],
        )
        self.assertEqual(len(factors), 3)
        self.assertTrue(all(item.status is AblationFactorStatus.ELIGIBLE for item in factors))
        self.assertEqual(
            [item.factor_name for item in factors],
            ["vol_contraction_10_60", "breakout_strength_20", "volume_ratio_5"],
        )
        short_factor_window = evaluate_ablation_factors(
            model=self.model,
            stock_code="600001",
            history_window=self.window,
            history_observation=primary,
            history_acceptance=acceptance,
            corporate_action_reviewed=True,
            corporate_action_source="synthetic_dual_source_review",
            corporate_action_data_as_of=self.source_data_as_of,
            corporate_actions=[],
        )
        self.assertTrue(
            all(item.status is AblationFactorStatus.UNDEFINED for item in short_factor_window)
        )

    def test_signal_output_is_deterministic_and_uses_frozen_handoff(self) -> None:
        ranked = rank_eligible(
            [
                self._evaluate(code=f"60000{index}", ret_5=0.10 - index * 0.01)
                for index in range(1, 7)
            ]
        )
        references = {
            item.stock_code: {
                "stock_name": f"虚构股票{index}",
                "reference_price": 10.0 + index,
            }
            for index, item in enumerate(ranked, start=1)
        }
        args = {
            "fetched_at": _iso(self.signal_date, "10:02:00"),
            "market_data_at": self.market_data_at,
            "model": self.model,
            "ranked_results": ranked,
            "references": references,
            "signal_date": self.signal_date,
            "source_data_as_of": self.source_data_as_of,
        }
        first = create_signals(**args)
        second = create_signals(**args)
        self.assertEqual(first, second)
        self.assertEqual(len(first), BENCHMARK_TOP_N)
        self.assertTrue(all(item.score is None for item in first))
        self.assertTrue(all("amount" not in item.raw_metric for item in first))
        self.assertEqual(OUTCOME_HORIZONS, ("1d", "3d", "5d", "10d", "20d"))
        handoff = outcome_handoff(first)
        self.assertEqual(
            set(handoff[0]),
            {"signal_id", "stock_code", "signal_date", "market_data_at", "reference_price"},
        )
        self.assertNotIn("ret_5", handoff[0])

    def test_unordered_signal_input_is_rejected(self) -> None:
        ranked = rank_eligible(
            [self._evaluate(code="600001", ret_5=0.01), self._evaluate(code="600002", ret_5=0.02)]
        )
        with self.assertRaisesRegex(BenchmarkValidationError, "stable ranking"):
            create_signals(
                model=self.model,
                ranked_results=list(reversed(ranked)),
                signal_date=self.signal_date,
                market_data_at=self.market_data_at,
                source_data_as_of=self.source_data_as_of,
                references={
                    "600001": {"reference_price": 10.0},
                    "600002": {"reference_price": 11.0},
                },
            )

    def test_undefined_factor_never_emits_nan_or_changes_main_result(self) -> None:
        closes = [100.0] * 61
        with self.assertRaisesRegex(BenchmarkValidationError, "denominator is zero"):
            vol_contraction_10_60(closes)
        primary, acceptance = self._raw_evidence("600001", closes)
        factors = evaluate_ablation_factors(
            model=self.model,
            stock_code="600001",
            history_window=self.factor_window,
            history_observation=primary,
            history_acceptance=acceptance,
            corporate_action_reviewed=True,
            corporate_action_source="synthetic_dual_source_review",
            corporate_action_data_as_of=self.source_data_as_of,
            corporate_actions=[],
        )
        self.assertEqual(factors[0].status, AblationFactorStatus.UNDEFINED)
        self.assertIsNone(factors[0].value)
        main = evaluate_history(
            model=self.model,
            stock_code="600001",
            history_window=self.window,
            history_observation=primary,
            history_acceptance=acceptance,
            corporate_action_reviewed=True,
            corporate_action_source="synthetic_dual_source_review",
            corporate_action_data_as_of=self.source_data_as_of,
            corporate_actions=[],
        )
        self.assertEqual(main.status, ShortTermStatus.TREND_FILTERED)
        self.assertTrue(math.isfinite(main.raw_metric["ret_20"]))


if __name__ == "__main__":
    unittest.main()
