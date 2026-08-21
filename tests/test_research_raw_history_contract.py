from __future__ import annotations

from datetime import date
import hashlib
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from research.benchmarks.raw_history import (
    CROSS_ADJUSTMENT,
    CROSS_RAW_SOURCE_ID,
    PRICE_BASIS,
    PRIMARY_ADJUSTMENT,
    PRIMARY_RAW_SOURCE_ID,
    RawDailyBar,
    RawHistoryContractError,
    RawHistoryObservation,
    evaluate_raw_history,
)
from research.benchmarks.schema import canonical_json_bytes
from research.benchmarks.trade_calendar import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    CalendarSourceObservation,
    VerifiedTradeCalendar,
)
from research.data_sources.raw_history import (
    fetch_akshare_sina_raw_history,
    fetch_baostock_raw_history,
    fetch_raw_history_pair,
)


DATES = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]


class RawHistoryContractTests(unittest.TestCase):
    def _calendar(self) -> VerifiedTradeCalendar:
        def observation(source_id):
            return CalendarSourceObservation(
                source_id=source_id,
                query_start="2026-01-02",
                query_end="2026-01-08",
                trading_dates=DATES,
                source_data_as_of="2026-01-08T09:00:00+08:00",
                fetched_at="2026-01-08T09:01:00+08:00",
            )
        return VerifiedTradeCalendar.create(
            query_start="2026-01-02",
            query_end="2026-01-08",
            primary=observation(PRIMARY_SOURCE_ID),
            cross=observation(CROSS_SOURCE_ID),
        )

    def _bar(self, value, *, trade_date, trading=True, amount=None):
        if not trading:
            return RawDailyBar.create(
                trade_date=trade_date, open="", high="", low="", close="",
                volume="", amount="", is_trading=False,
            )
        close = str(value)
        return RawDailyBar.create(
            trade_date=trade_date, open=close, high=str(value + 1),
            low=str(value - 1), close=close, volume=str(value * 100),
            amount=str(amount if amount is not None else value * 1000),
            is_trading=True,
        )

    def _observation(self, source_id, bars, **overrides):
        values = {
            "source_id": source_id,
            "symbol": "600519",
            "requested_start": "2026-01-02",
            "requested_end": "2026-01-07",
            "fetched_at": "2026-01-08T09:30:00+08:00" if source_id == PRIMARY_RAW_SOURCE_ID else "2026-01-08T09:31:00+08:00",
            "price_basis": PRICE_BASIS,
            "adjustment": PRIMARY_ADJUSTMENT if source_id == PRIMARY_RAW_SOURCE_ID else CROSS_ADJUSTMENT,
            "volume_unit": "share",
            "amount_unit": "CNY",
            "bars": bars,
        }
        values.update(overrides)
        return RawHistoryObservation.create(**values)

    def _pair(self, *, amount_delta="0", cross_overrides=None):
        primary_bars = [
            self._bar(10, trade_date="2026-01-02"),
            self._bar(11, trade_date="2026-01-05"),
            self._bar(0, trade_date="2026-01-06", trading=False),
            self._bar(12, trade_date="2026-01-07"),
        ]
        cross_bars = [
            self._bar(10, trade_date="2026-01-02", amount=10000 + float(amount_delta)),
            self._bar(11, trade_date="2026-01-05"),
            self._bar(12, trade_date="2026-01-07"),
        ]
        if cross_overrides:
            cross_bars = cross_overrides(cross_bars)
        return (
            self._observation(PRIMARY_RAW_SOURCE_ID, primary_bars),
            self._observation(CROSS_RAW_SOURCE_ID, cross_bars),
        )

    def _evaluate(self, primary=None, cross=None, **overrides):
        if primary is None or cross is None:
            primary, cross = self._pair()
        values = {
            "calendar": self._calendar(),
            "request_at": "2026-01-08T09:20:00+08:00",
            "market_data_at": "2026-01-08T10:00:00+08:00",
            "primary": primary,
            "cross": cross,
        }
        values.update(overrides)
        return evaluate_raw_history(**values)

    def test_exact_ohlcv_and_suspension_alignment_passes_conditionally(self):
        result = self._evaluate()
        self.assertEqual(result.manifest["acceptance_status"], "conditional_pass")
        self.assertEqual(result.manifest["field_conflict_counts"], {
            "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0,
        })
        self.assertEqual(result.manifest["suspended_dates"], ["2026-01-06"])

    def test_suspended_carried_ohlc_is_ignored_but_trading_values_must_be_zero(self):
        carried = RawDailyBar.create(
            trade_date="2026-01-06", open="10", high="10", low="10", close="10",
            volume="0", amount="0", is_trading=False,
        )
        self.assertFalse(carried.is_trading)
        with self.assertRaisesRegex(RawHistoryContractError, "zero volume and amount"):
            RawDailyBar.create(
                trade_date="2026-01-06", open="10", high="10", low="10", close="10",
                volume="1", amount="0", is_trading=False,
            )

    def test_qfq_or_hfq_adjustment_is_rejected(self):
        bars = [self._bar(10, trade_date="2026-01-02")]
        for adjustment in ("1", "2", "qfq", "hfq"):
            with self.subTest(adjustment=adjustment), self.assertRaisesRegex(
                RawHistoryContractError, "raw unadjusted"
            ):
                self._observation(PRIMARY_RAW_SOURCE_ID, bars, adjustment=adjustment)

    def test_intraday_t_bar_or_t_request_is_rejected(self):
        primary, cross = self._pair()
        primary = self._observation(
            PRIMARY_RAW_SOURCE_ID,
            [*primary.bars, self._bar(13, trade_date="2026-01-08")],
            requested_end="2026-01-08",
        )
        cross = self._observation(
            CROSS_RAW_SOURCE_ID,
            [*cross.bars, self._bar(13, trade_date="2026-01-08")],
            requested_end="2026-01-08",
        )
        with self.assertRaisesRegex(RawHistoryContractError, "beyond frozen cutoff"):
            self._evaluate(primary, cross)

    def test_cutoff_change_during_fetch_fails_closed(self):
        with self.assertRaisesRegex(RawHistoryContractError, "cutoff changed"):
            self._evaluate(market_data_at="2026-01-08T15:00:00+08:00")

    def test_primary_must_equal_frozen_trade_calendar(self):
        primary, cross = self._pair()
        primary = self._observation(PRIMARY_RAW_SOURCE_ID, primary.bars[:-1])
        with self.assertRaisesRegex(RawHistoryContractError, "frozen trade calendar"):
            self._evaluate(primary, cross)

    def test_unexplained_missing_active_cross_date_fails_closed(self):
        primary, cross = self._pair()
        cross = self._observation(CROSS_RAW_SOURCE_ID, cross.bars[:-1])
        with self.assertRaisesRegex(RawHistoryContractError, "active primary dates"):
            self._evaluate(primary, cross)

    def test_duplicate_and_unsorted_rows_are_rejected(self):
        bar1 = self._bar(10, trade_date="2026-01-02")
        bar2 = self._bar(11, trade_date="2026-01-05")
        with self.assertRaisesRegex(RawHistoryContractError, "duplicates"):
            self._observation(PRIMARY_RAW_SOURCE_ID, [bar1, bar1])
        with self.assertRaisesRegex(RawHistoryContractError, "strictly increasing"):
            self._observation(PRIMARY_RAW_SOURCE_ID, [bar2, bar1])

    def test_amount_rounding_within_declared_tolerance_is_reported(self):
        primary, cross = self._pair(amount_delta="0.50")
        result = self._evaluate(primary, cross)
        self.assertEqual(result.manifest["amount_comparison"]["exact_conflict_count"], 1)
        self.assertEqual(result.manifest["amount_comparison"]["max_absolute_difference_cny"], "0.5")

    def test_amount_over_tolerance_fails_closed(self):
        primary, cross = self._pair(amount_delta="0.51")
        with self.assertRaisesRegex(RawHistoryContractError, "amount_over_tolerance"):
            self._evaluate(primary, cross)

    def test_any_ohlcv_conflict_fails_closed(self):
        def changed(rows):
            rows[0] = RawDailyBar.create(
                trade_date="2026-01-02", open="10", high="11", low="9",
                close="10", volume="1001", amount="10000", is_trading=True,
            )
            return rows
        primary, cross = self._pair(cross_overrides=changed)
        with self.assertRaisesRegex(RawHistoryContractError, "ohlcv=1"):
            self._evaluate(primary, cross)

    def test_manifest_is_stable_and_contains_no_raw_rows(self):
        first = self._evaluate()
        second = self._evaluate()
        self.assertEqual(first.serialize(), second.serialize())
        payload = dict(first.manifest)
        expected_hash = payload.pop("manifest_sha256")
        self.assertEqual(expected_hash, hashlib.sha256(canonical_json_bytes(payload)).hexdigest())
        serialized = json.loads(first.serialize())
        self.assertNotIn("bars", serialized)
        self.assertEqual(serialized["public_payload_policy"], "metadata_and_hashes_only_no_raw_rows")

    def test_fetched_after_market_data_at_is_rejected(self):
        primary, cross = self._pair()
        cross = self._observation(
            CROSS_RAW_SOURCE_ID, cross.bars,
            fetched_at="2026-01-08T10:00:01+08:00",
        )
        with self.assertRaisesRegex(RawHistoryContractError, "fetched before"):
            self._evaluate(primary, cross)

    def test_network_is_disabled_by_default_and_primary_failure_stops_cross(self):
        with patch("research.data_sources.raw_history.fetch_baostock_raw_history") as primary, patch(
            "research.data_sources.raw_history.fetch_akshare_sina_raw_history"
        ) as cross:
            with self.assertRaisesRegex(RawHistoryContractError, "disabled"):
                fetch_raw_history_pair("600519", date(2026, 1, 2), date(2026, 1, 7))
            primary.assert_not_called()
            cross.assert_not_called()
        with patch(
            "research.data_sources.raw_history.fetch_baostock_raw_history",
            side_effect=RawHistoryContractError("synthetic"),
        ), patch("research.data_sources.raw_history.fetch_akshare_sina_raw_history") as cross:
            with self.assertRaises(RawHistoryContractError):
                fetch_raw_history_pair(
                    "600519", date(2026, 1, 2), date(2026, 1, 7), allow_network=True
                )
            cross.assert_not_called()

    def test_baostock_adapter_pins_raw_daily_parameters(self):
        class Result:
            error_code = "0"
            fields = ["date", "open", "high", "low", "close", "volume", "amount", "adjustflag", "tradestatus"]
            rows = [["2026-01-02", "10", "11", "9", "10", "1000", "10000", "3", "1"]]
            index = -1

            def next(self):
                self.index += 1
                return self.index < len(self.rows)

            def get_row_data(self):
                return self.rows[self.index]
        query = Mock(return_value=Result())
        fake = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code="0"),
            logout=Mock(), query_history_k_data_plus=query,
        )
        with patch.dict(sys.modules, {"baostock": fake}):
            fetch_baostock_raw_history("600519", date(2026, 1, 2), date(2026, 1, 2))
        self.assertEqual(query.call_args.kwargs["adjustflag"], "3")
        self.assertEqual(query.call_args.kwargs["frequency"], "d")
        self.assertEqual(query.call_args.kwargs["end_date"], "2026-01-02")

    def test_baostock_ambiguous_full_terminal_page_fails_closed(self):
        class Result:
            error_code = "0"
            fields = ["date", "open", "high", "low", "close", "volume", "amount", "adjustflag", "tradestatus"]
            data = [["2026-01-02", "10", "11", "9", "10", "1000", "10000", "3", "1"]]
            per_page_count = "1"
            cur_row_num = 0

            def next(self):
                return self.cur_row_num < len(self.data)

            def get_row_data(self):
                row = self.data[self.cur_row_num]
                self.cur_row_num += 1
                return row

        fake = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code="0"),
            logout=Mock(),
            query_history_k_data_plus=lambda *_args, **_kwargs: Result(),
        )
        with patch.dict(sys.modules, {"baostock": fake}):
            with self.assertRaisesRegex(RawHistoryContractError, "pagination_incomplete"):
                fetch_baostock_raw_history(
                    "600519", date(2026, 1, 2), date(2026, 1, 2)
                )

    def test_akshare_sina_adapter_pins_empty_adjustment_and_units(self):
        frame = pd.DataFrame([{
            "date": "2026-01-02", "open": 10, "high": 11, "low": 9,
            "close": 10, "volume": 1000, "amount": 10000,
        }])
        query = Mock(return_value=frame)
        with patch.dict(sys.modules, {"akshare": SimpleNamespace(stock_zh_a_daily=query)}):
            observation = fetch_akshare_sina_raw_history(
                "600519", date(2026, 1, 2), date(2026, 1, 2)
            )
        self.assertEqual(query.call_args.kwargs["adjust"], "")
        self.assertEqual(query.call_args.kwargs["symbol"], "sh600519")
        self.assertEqual(observation.volume_unit, "share")
        self.assertEqual(observation.amount_unit, "CNY")

    def test_repeated_fetch_is_not_served_from_cross_day_cache(self):
        with patch("research.data_sources.raw_history.fetch_baostock_raw_history", return_value=Mock()) as primary, patch(
            "research.data_sources.raw_history.fetch_akshare_sina_raw_history", return_value=Mock()
        ) as cross:
            for _ in range(2):
                fetch_raw_history_pair(
                    "600519", date(2026, 1, 2), date(2026, 1, 7), allow_network=True
                )
        self.assertEqual(primary.call_count, 2)
        self.assertEqual(cross.call_count, 2)


if __name__ == "__main__":
    unittest.main()
