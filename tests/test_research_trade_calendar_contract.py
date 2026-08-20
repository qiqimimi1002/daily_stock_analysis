from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from research.benchmarks.schema import canonical_json_bytes
from research.benchmarks.trade_calendar import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    CalendarSourceObservation,
    HistoryWindowContract,
    TradeCalendarContractError,
    VerifiedTradeCalendar,
)
from research.data_sources.trade_calendar import (
    fetch_akshare_trade_dates,
    fetch_baostock_trade_dates,
    fetch_verified_trade_calendar,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trade_calendar_phase2b_0b1.json"


class ResearchTradeCalendarContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _observation(self, source_id: str, **overrides) -> CalendarSourceObservation:
        values = {
            "source_id": source_id,
            "query_start": self.fixture["query_start"],
            "query_end": self.fixture["query_end"],
            "trading_dates": list(self.fixture["trading_dates"]),
            "source_data_as_of": "2026-01-08T09:58:00+08:00",
            "fetched_at": "2026-01-08T09:59:00+08:00",
        }
        values.update(overrides)
        return CalendarSourceObservation(**values)

    def _calendar(self, **overrides) -> VerifiedTradeCalendar:
        primary_overrides = overrides.pop("primary_overrides", {})
        cross_overrides = overrides.pop("cross_overrides", {})
        return VerifiedTradeCalendar.create(
            query_start=overrides.pop("query_start", self.fixture["query_start"]),
            query_end=overrides.pop("query_end", self.fixture["query_end"]),
            primary=self._observation(PRIMARY_SOURCE_ID, **primary_overrides),
            cross=self._observation(CROSS_SOURCE_ID, **cross_overrides),
            **overrides,
        )

    def _history_kwargs(self, **overrides):
        values = {
            "calendar": self._calendar(),
            "market_data_at": "2026-01-08T10:00:00+08:00",
            "history_data_as_of": "2026-01-07T15:00:00+08:00",
            "source_data_as_of": "2026-01-08T09:59:00+08:00",
            "fetched_at": "2026-01-08T10:05:00+08:00",
            "generated_at": "2026-01-08T10:01:00+08:00",
            "required_observations": 3,
            "observed_trade_dates": [
                "2026-01-05",
                "2026-01-06",
                "2026-01-07",
            ],
        }
        values.update(overrides)
        return values

    def test_two_sources_exactly_agree_pass(self) -> None:
        calendar = self._calendar()
        self.assertEqual(calendar.consistency_status, "pass")
        self.assertEqual(calendar.primary_count, 8)
        self.assertEqual(calendar.cross_count, 8)
        self.assertEqual(calendar.trading_dates[0].isoformat(), "2026-01-02")

    def test_primary_failure_stops_before_cross_source(self) -> None:
        with patch(
            "research.data_sources.trade_calendar.fetch_baostock_trade_dates",
            side_effect=RuntimeError("synthetic primary failure"),
        ), patch(
            "research.data_sources.trade_calendar.fetch_akshare_trade_dates"
        ) as cross_fetch:
            with self.assertRaisesRegex(
                TradeCalendarContractError,
                "baostock.query_trade_dates failed closed",
            ):
                fetch_verified_trade_calendar(
                    self.fixture["query_start"],
                    self.fixture["query_end"],
                    allow_network=True,
                )
        cross_fetch.assert_not_called()

    def test_cross_source_failure_fails_whole_result(self) -> None:
        with patch(
            "research.data_sources.trade_calendar.fetch_baostock_trade_dates",
            return_value=self._observation(PRIMARY_SOURCE_ID),
        ), patch(
            "research.data_sources.trade_calendar.fetch_akshare_trade_dates",
            side_effect=RuntimeError("synthetic cross failure"),
        ):
            with self.assertRaisesRegex(
                TradeCalendarContractError,
                "akshare.tool_trade_date_hist_sina failed closed",
            ):
                fetch_verified_trade_calendar(
                    self.fixture["query_start"],
                    self.fixture["query_end"],
                    allow_network=True,
                )

    def test_missing_date_disagreement_fails_closed(self) -> None:
        dates = self.fixture["trading_dates"][:-1]
        with self.assertRaisesRegex(TradeCalendarContractError, "sources disagree"):
            self._calendar(cross_overrides={"trading_dates": dates})

    def test_extra_date_disagreement_fails_closed(self) -> None:
        dates = ["2026-01-01", *self.fixture["trading_dates"]]
        with self.assertRaisesRegex(TradeCalendarContractError, "sources disagree"):
            self._calendar(cross_overrides={"trading_dates": dates})

    def test_unsorted_source_dates_are_rejected_before_comparison(self) -> None:
        dates = list(self.fixture["trading_dates"])
        dates[2], dates[3] = dates[3], dates[2]
        with self.assertRaisesRegex(TradeCalendarContractError, "strictly increasing"):
            self._calendar(cross_overrides={"trading_dates": dates})

    def test_duplicate_source_date_is_rejected(self) -> None:
        dates = list(self.fixture["trading_dates"])
        dates.insert(2, dates[1])
        with self.assertRaisesRegex(TradeCalendarContractError, "duplicate"):
            self._calendar(primary_overrides={"trading_dates": dates})

    def test_out_of_range_source_date_is_rejected(self) -> None:
        dates = ["2025-12-31", *self.fixture["trading_dates"]]
        with self.assertRaisesRegex(TradeCalendarContractError, "outside"):
            self._calendar(primary_overrides={"trading_dates": dates})

    def test_noncanonical_date_format_is_rejected(self) -> None:
        dates = list(self.fixture["trading_dates"])
        dates[0] = "2026-1-2"
        with self.assertRaisesRegex(TradeCalendarContractError, "ISO-8601"):
            self._calendar(primary_overrides={"trading_dates": dates})

    def test_empty_source_result_is_rejected(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "cannot be empty"):
            self._calendar(primary_overrides={"trading_dates": []})

    def test_wrong_source_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "source_id"):
            VerifiedTradeCalendar.create(
                query_start=self.fixture["query_start"],
                query_end=self.fixture["query_end"],
                primary=self._observation(CROSS_SOURCE_ID),
                cross=self._observation(CROSS_SOURCE_ID),
            )

    def test_weekends_and_declared_holiday_are_not_synthesized(self) -> None:
        calendar = self._calendar()
        serialized_dates = {item.isoformat() for item in calendar.trading_dates}
        for non_trading_date in self.fixture["explicit_non_trading_dates"]:
            self.assertNotIn(non_trading_date, serialized_dates)

    def test_intraday_trading_day_cutoff_is_previous_session(self) -> None:
        cutoff = self._calendar().previous_completed_trade_date(
            "2026-01-08T10:00:00+08:00"
        )
        self.assertEqual(cutoff.isoformat(), "2026-01-07")

    def test_after_close_trading_day_can_use_completed_session(self) -> None:
        cutoff = self._calendar().previous_completed_trade_date(
            "2026-01-08T15:00:00+08:00"
        )
        self.assertEqual(cutoff.isoformat(), "2026-01-08")

    def test_non_trading_signal_date_uses_latest_market_session(self) -> None:
        cutoff = self._calendar().previous_completed_trade_date(
            "2026-01-11T10:00:00+08:00"
        )
        self.assertEqual(cutoff.isoformat(), "2026-01-09")

    def test_signal_date_outside_verified_interval_fails_closed(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "query interval"):
            self._calendar().previous_completed_trade_date(
                "2026-01-14T10:00:00+08:00"
            )

    def test_history_window_contains_exactly_n_consecutive_sessions(self) -> None:
        window = HistoryWindowContract.create(**self._history_kwargs())
        self.assertEqual(len(window.required_trade_dates), 3)
        self.assertEqual(
            [item.isoformat() for item in window.required_trade_dates],
            ["2026-01-05", "2026-01-06", "2026-01-07"],
        )

    def test_missing_real_trade_date_cannot_shorten_or_fill_window(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "exactly"):
            HistoryWindowContract.create(
                **self._history_kwargs(
                    observed_trade_dates=["2026-01-05", "2026-01-07"]
                )
            )

    def test_natural_day_or_older_substitution_is_rejected(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "exactly"):
            HistoryWindowContract.create(
                **self._history_kwargs(
                    observed_trade_dates=[
                        "2026-01-02",
                        "2026-01-06",
                        "2026-01-07",
                    ]
                )
            )

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "timezone"):
            HistoryWindowContract.create(
                **self._history_kwargs(market_data_at="2026-01-08T10:00:00")
            )

    def test_wrong_timezone_semantics_are_rejected(self) -> None:
        wrong_zone = datetime(2026, 1, 8, 11, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        with self.assertRaisesRegex(TradeCalendarContractError, "Asia/Shanghai"):
            HistoryWindowContract.create(
                **self._history_kwargs(market_data_at=wrong_zone)
            )

    def test_future_source_data_as_of_is_rejected(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "source_data_as_of"):
            HistoryWindowContract.create(
                **self._history_kwargs(
                    source_data_as_of="2026-01-08T10:00:01+08:00"
                )
            )

    def test_future_calendar_source_data_as_of_is_rejected(self) -> None:
        calendar = self._calendar(
            cross_overrides={
                "source_data_as_of": "2026-01-08T10:00:01+08:00",
                "fetched_at": "2026-01-08T10:00:02+08:00",
            }
        )
        with self.assertRaisesRegex(
            TradeCalendarContractError,
            "calendar source_data_as_of",
        ):
            HistoryWindowContract.create(
                **self._history_kwargs(calendar=calendar)
            )

    def test_late_calendar_fetched_at_does_not_authorize_later_content(self) -> None:
        calendar = self._calendar(
            cross_overrides={
                "source_data_as_of": "2026-01-08T09:58:00+08:00",
                "fetched_at": "2026-01-08T10:30:00+08:00",
            }
        )
        window = HistoryWindowContract.create(
            **self._history_kwargs(calendar=calendar)
        )
        self.assertEqual(
            window.previous_completed_trade_date.isoformat(),
            "2026-01-07",
        )

    def test_future_trade_date_in_history_is_rejected(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "future trade date"):
            HistoryWindowContract.create(
                **self._history_kwargs(
                    observed_trade_dates=[
                        "2026-01-06",
                        "2026-01-07",
                        "2026-01-09",
                    ]
                )
            )

    def test_intraday_t_daily_bar_leak_is_rejected(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "future trade date"):
            HistoryWindowContract.create(
                **self._history_kwargs(
                    observed_trade_dates=[
                        "2026-01-06",
                        "2026-01-07",
                        "2026-01-08",
                    ]
                )
            )

    def test_history_data_as_of_cannot_claim_intraday_t_bar(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "cutoff"):
            HistoryWindowContract.create(
                **self._history_kwargs(
                    history_data_as_of="2026-01-08T09:59:00+08:00"
                )
            )

    def test_history_cutoff_bar_must_be_completed(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "completion"):
            HistoryWindowContract.create(
                **self._history_kwargs(
                    history_data_as_of="2026-01-07T14:59:59+08:00"
                )
            )

    def test_late_fetched_at_is_audit_only(self) -> None:
        window = HistoryWindowContract.create(
            **self._history_kwargs(fetched_at="2026-01-08T23:00:00+08:00")
        )
        self.assertGreater(window.fetched_at, window.market_data_at)
        self.assertEqual(
            window.previous_completed_trade_date.isoformat(),
            "2026-01-07",
        )

    def test_calendar_serialization_and_content_hash_are_stable(self) -> None:
        first = self._calendar()
        second = self._calendar()
        self.assertEqual(first.serialize(), second.serialize())
        self.assertEqual(first.content_sha256, second.content_sha256)
        payload = first.to_dict()
        content_hash = payload.pop("content_sha256")
        self.assertEqual(
            content_hash,
            hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )

    def test_history_window_is_deterministic_for_same_input(self) -> None:
        first = HistoryWindowContract.create(**self._history_kwargs())
        second = HistoryWindowContract.create(**self._history_kwargs())
        self.assertEqual(first.serialize(), second.serialize())
        self.assertEqual(first.content_sha256, second.content_sha256)

    def test_source_metadata_requires_timezone_and_ordered_audit_times(self) -> None:
        with self.assertRaisesRegex(TradeCalendarContractError, "timezone"):
            self._calendar(
                primary_overrides={
                    "source_data_as_of": "2026-01-08T09:58:00"
                }
            )
        with self.assertRaisesRegex(TradeCalendarContractError, "fetched_at"):
            self._calendar(
                primary_overrides={
                    "source_data_as_of": "2026-01-08T09:59:10+08:00",
                    "fetched_at": "2026-01-08T09:59:00+08:00",
                }
            )

    def test_network_is_zero_by_default(self) -> None:
        with patch(
            "research.data_sources.trade_calendar.fetch_baostock_trade_dates"
        ) as primary_fetch, patch(
            "research.data_sources.trade_calendar.fetch_akshare_trade_dates"
        ) as cross_fetch:
            with self.assertRaisesRegex(TradeCalendarContractError, "disabled"):
                fetch_verified_trade_calendar(
                    self.fixture["query_start"],
                    self.fixture["query_end"],
                )
        primary_fetch.assert_not_called()
        cross_fetch.assert_not_called()

    def test_baostock_adapter_filters_only_declared_trading_rows(self) -> None:
        class FakeResult:
            error_code = "0"
            fields = ["calendar_date", "is_trading_day"]

            def __init__(self) -> None:
                self.rows = [
                    ["2026-01-01", "0"],
                    ["2026-01-02", "1"],
                    ["2026-01-03", "0"],
                ]
                self.index = -1

            def next(self) -> bool:
                self.index += 1
                return self.index < len(self.rows)

            def get_row_data(self):
                return self.rows[self.index]

        logout = Mock()
        fake_module = SimpleNamespace(
            login=lambda: SimpleNamespace(error_code="0"),
            query_trade_dates=lambda **_kwargs: FakeResult(),
            logout=logout,
        )
        with patch.dict(sys.modules, {"baostock": fake_module}):
            observation = fetch_baostock_trade_dates(
                "2026-01-01",
                "2026-01-03",
            )
        self.assertEqual(observation.source_id, PRIMARY_SOURCE_ID)
        self.assertEqual(
            [item.isoformat() for item in observation.trading_dates],
            ["2026-01-02"],
        )
        logout.assert_called_once_with()

    def test_akshare_adapter_uses_only_requested_interval(self) -> None:
        class FakeFrame:
            empty = False
            columns = ["trade_date"]

            def __getitem__(self, key):
                if key != "trade_date":
                    raise KeyError(key)
                return ["2025-12-31", "2026-01-02", "2026-01-05", "2026-01-14"]

        fake_module = SimpleNamespace(tool_trade_date_hist_sina=lambda: FakeFrame())
        with patch.dict(sys.modules, {"akshare": fake_module}):
            observation = fetch_akshare_trade_dates(
                "2026-01-01",
                "2026-01-13",
            )
        self.assertEqual(observation.source_id, CROSS_SOURCE_ID)
        self.assertEqual(
            [item.isoformat() for item in observation.trading_dates],
            ["2026-01-02", "2026-01-05"],
        )


if __name__ == "__main__":
    unittest.main()
