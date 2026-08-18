from __future__ import annotations

from dataclasses import replace
import unittest

import pandas as pd

from research.benchmarks.universe import UniverseStatus, evaluate_v21_universe
from src.services.market_screener import ScreeningConfig, apply_spot_filters


def _row(code: str, name: str = "测试股份", **overrides) -> dict:
    values = {
        "code": code,
        "name": name,
        "close": 10.0,
        "prev_close": 9.9,
        "pct_change": 1.01,
        "volume": 30_000_000,
        "amount": 300_000_000,
        "turnover": 2.0,
    }
    values.update(overrides)
    return values


class BenchmarkUniverseTests(unittest.TestCase):
    def test_main_board_rows_use_v2_1_hard_filter_as_eligibility_truth(self) -> None:
        frame = pd.DataFrame(
            [
                _row("600100"),
                _row("000100"),
                _row("300100"),
                _row("688100"),
            ]
        )
        history = {code: 40 for code in frame["code"]}
        decisions = evaluate_v21_universe(frame, history_rows_by_code=history)
        actual = {
            item.stock_code
            for item in decisions
            if item.status is UniverseStatus.ELIGIBLE
        }

        config = ScreeningConfig()
        full_config = replace(
            config,
            preselect_limit=max(config.top_n, len(frame)),
        )
        expected = set(apply_spot_filters(frame, full_config)["code"])
        self.assertEqual(actual, expected)
        self.assertEqual(actual, {"600100", "000100"})

    def test_st_and_new_listing_names_are_unavailable(self) -> None:
        frame = pd.DataFrame(
            [
                _row("600100", "ST测试"),
                _row("600101", "*ST测试"),
                _row("600102", "N测试"),
                _row("600103", "退市测试"),
            ]
        )
        decisions = evaluate_v21_universe(
            frame,
            history_rows_by_code={code: 60 for code in frame["code"]},
        )
        self.assertEqual(
            {item.status for item in decisions},
            {UniverseStatus.UNAVAILABLE},
        )

    def test_suspended_row_is_explicit(self) -> None:
        frame = pd.DataFrame([_row("600100", volume=0, amount=0)])
        decision = evaluate_v21_universe(
            frame, history_rows_by_code={"600100": 60}
        )[0]
        self.assertEqual(decision.status, UniverseStatus.SUSPENDED)
        self.assertEqual(decision.reasons, ("no_current_market_turnover",))

    def test_insufficient_history_is_explicit(self) -> None:
        frame = pd.DataFrame([_row("600100")])
        decision = evaluate_v21_universe(
            frame, history_rows_by_code={"600100": 19}
        )[0]
        self.assertEqual(decision.status, UniverseStatus.INSUFFICIENT_HISTORY)

    def test_missing_history_is_unavailable_without_guessing(self) -> None:
        frame = pd.DataFrame([_row("600100")])
        decision = evaluate_v21_universe(
            frame, history_rows_by_code={}
        )[0]
        self.assertEqual(decision.status, UniverseStatus.UNAVAILABLE)
        self.assertEqual(decision.reasons, ("history_unavailable",))

    def test_invalid_market_or_history_values_are_explicit(self) -> None:
        frame = pd.DataFrame(
            [
                _row("600100", close=float("nan")),
                _row("600101"),
            ]
        )
        decisions = evaluate_v21_universe(
            frame,
            history_rows_by_code={"600100": 30, "600101": -1},
        )
        by_code = {item.stock_code: item for item in decisions}
        self.assertEqual(by_code["600100"].status, UniverseStatus.INVALID_DATA)
        self.assertEqual(by_code["600101"].status, UniverseStatus.INVALID_DATA)

    def test_output_order_is_stable_and_input_is_not_mutated(self) -> None:
        frame = pd.DataFrame([_row("600200"), _row("000100")])
        original = frame.copy(deep=True)
        first = evaluate_v21_universe(
            frame,
            history_rows_by_code={"600200": 40, "000100": 40},
        )
        second = evaluate_v21_universe(
            frame.iloc[::-1].reset_index(drop=True),
            history_rows_by_code={"600200": 40, "000100": 40},
        )
        self.assertEqual(
            [item.stock_code for item in first],
            ["000100", "600200"],
        )
        self.assertEqual(
            [item.to_dict() for item in first],
            [item.to_dict() for item in second],
        )
        pd.testing.assert_frame_equal(frame, original)

    def test_duplicate_codes_are_rejected_instead_of_silently_colliding(self) -> None:
        frame = pd.DataFrame([_row("600100"), _row("600100")])
        with self.assertRaisesRegex(ValueError, "one row per stock_code"):
            evaluate_v21_universe(
                frame,
                history_rows_by_code={"600100": 40},
            )


if __name__ == "__main__":
    unittest.main()
