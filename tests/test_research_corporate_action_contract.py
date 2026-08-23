from __future__ import annotations

from decimal import Decimal
import unittest

from research.benchmarks.corporate_actions import (
    ACCEPTANCE_STATUS,
    CorporateActionContractError,
    CorporateActionEvent,
    CorporateActionObservation,
    action_safe_bar_view,
    distribution_economic_return,
    evaluate_corporate_actions,
    reference_price,
)
from research.benchmarks.raw_history import RawDailyBar
from research.benchmarks.trade_calendar import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    CalendarSourceObservation,
    VerifiedTradeCalendar,
)


DATES = [
    "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07",
    "2026-01-08", "2026-01-09",
]


class CorporateActionContractTests(unittest.TestCase):
    def _calendar(self) -> VerifiedTradeCalendar:
        def observation(source_id):
            return CalendarSourceObservation(
                source_id=source_id,
                query_start=DATES[0],
                query_end=DATES[-1],
                trading_dates=DATES,
                source_data_as_of="2026-01-09T09:00:00+08:00",
                fetched_at="2026-01-09T09:01:00+08:00",
            )

        return VerifiedTradeCalendar.create(
            query_start=DATES[0],
            query_end=DATES[-1],
            primary=observation(PRIMARY_SOURCE_ID),
            cross=observation(CROSS_SOURCE_ID),
        )

    def _event(self, action_type="cash_dividend", **overrides):
        values = {
            "symbol": "600519",
            "action_type": action_type,
            "known_at": "2026-01-02T18:00:00+08:00",
        }
        if action_type == "cash_dividend":
            values.update(
                record_date="2026-01-05", ex_date="2026-01-06",
                payment_date="2026-01-06", cash_per_share="0.5",
            )
        elif action_type == "stock_dividend_or_transfer":
            values.update(
                record_date="2026-01-05", ex_date="2026-01-06",
                listing_date="2026-01-06", cash_per_share="0.1",
                payment_date="2026-01-06", stock_ratio="0.3",
            )
        elif action_type == "rights_issue":
            values.update(
                record_date="2026-01-05", ex_date="2026-01-08",
                listing_date="2026-01-09", rights_ratio="0.15",
                rights_price="14.43",
            )
        else:
            values.update(
                suspension_start="2026-01-05", resumption_date="2026-01-08",
            )
        values.update(overrides)
        return CorporateActionEvent.create(**values)

    def _observation(self, source_id, events, **overrides):
        values = {
            "source_id": source_id,
            "source_data_as_of": "2026-01-09T09:10:00+08:00",
            "fetched_at": "2026-01-09T09:11:00+08:00",
            "events": events,
        }
        values.update(overrides)
        return CorporateActionObservation.create(**values)

    def _no_event_observation(self, source_id, **overrides):
        values = {
            "source_id": source_id,
            "source_data_as_of": "2026-01-09T09:10:00+08:00",
            "fetched_at": "2026-01-09T09:11:00+08:00",
            "symbol": "600519",
            "query_start": "2026-01-05",
            "query_end": "2026-01-06",
            "query_status": "success",
            "query_result": "no_event",
            "events": [],
        }
        values.update(overrides)
        return CorporateActionObservation.create(**values)

    def _bar(self, trade_date, *, trading=True, carried=False):
        if not trading:
            price = "25.7" if carried else ""
            return RawDailyBar.create(
                trade_date=trade_date, open=price, high=price, low=price,
                close=price, volume="", amount="", is_trading=False,
            )
        return RawDailyBar.create(
            trade_date=trade_date, open="24", high="25", low="23",
            close="24.5", volume="100", amount="2450", is_trading=True,
        )

    def _evaluate(self, events=None, cross_events=None, raw_bars=None, **overrides):
        events = [self._event()] if events is None else events
        cross_events = events if cross_events is None else cross_events
        values = {
            "calendar": self._calendar(),
            "market_data_at": "2026-01-09T10:00:00+08:00",
            "primary": self._observation(
                "akshare.stock_dividend_cninfo.snapshot", events
            ),
            "cross": self._observation(
                "akshare.stock_history_dividend_detail.sina.snapshot",
                cross_events,
            ),
            "raw_bars": raw_bars or [
                self._bar("2026-01-05"), self._bar("2026-01-06")
            ],
        }
        values.update(overrides)
        return evaluate_corporate_actions(**values)

    def test_cash_dividend_pair_passes_with_hash_only_manifest(self):
        result = self._evaluate()
        self.assertEqual(result.manifest["acceptance_status"], ACCEPTANCE_STATUS)
        self.assertEqual(result.manifest["review_status"], "review_required")
        self.assertEqual(result.manifest["action_types"], ["cash_dividend"])
        self.assertNotIn("events", result.manifest)
        self.assertNotIn(b"24.5", result.serialize())

    def test_stock_transfer_combined_with_cash_is_supported(self):
        event = self._event("stock_dividend_or_transfer")
        result = self._evaluate(events=[event])
        self.assertEqual(result.manifest["action_dates"], ["2026-01-06"])
        self.assertEqual(reference_price("22.45", event), Decimal("17.19230769230769230769230769"))

    def test_cash_distribution_economic_return_uses_separate_overlay(self):
        event = self._event(cash_per_share="0.5")
        self.assertEqual(
            distribution_economic_return("10", "9.8", event),
            Decimal("0.03"),
        )

    def test_stock_distribution_economic_return_handles_share_ratio(self):
        event = self._event("stock_dividend_or_transfer")
        expected = (Decimal("16.49") * Decimal("1.3") + Decimal("0.1")) / Decimal("22.45") - 1
        self.assertEqual(distribution_economic_return("22.45", "16.49", event), expected)

    def test_rights_reference_price_is_diagnostic_but_return_requires_review(self):
        event = self._event("rights_issue")
        self.assertEqual(
            reference_price("25.70", event),
            (Decimal("25.70") + Decimal("14.43") * Decimal("0.15")) / Decimal("1.15"),
        )
        with self.assertRaisesRegex(CorporateActionContractError, "holder decision"):
            distribution_economic_return("25.70", "24.15", event)

    def test_suspended_carried_ohlc_is_never_exposed(self):
        view = action_safe_bar_view([
            self._bar("2026-01-05", trading=False, carried=True)
        ])
        self.assertEqual(
            {view[0][name] for name in ("open", "high", "low", "close")},
            {None},
        )
        self.assertIsNone(view[0]["volume"])

    def test_suspension_and_resumption_match_frozen_calendar(self):
        event = self._event("suspension_resumption")
        bars = [
            self._bar("2026-01-05", trading=False, carried=True),
            self._bar("2026-01-06", trading=False, carried=True),
            self._bar("2026-01-07", trading=False, carried=True),
            self._bar("2026-01-08"),
        ]
        result = self._evaluate(events=[event], raw_bars=bars)
        self.assertEqual(result.manifest["suspended_date_count"], 3)
        self.assertEqual(result.manifest["suspension_price_policy"], "inactive_provider_ohlc_discarded_no_forward_fill")

    def test_missing_cross_event_fails_closed(self):
        with self.assertRaisesRegex(CorporateActionContractError, "explicitly"):
            self._evaluate(cross_events=[])

    def test_two_successful_no_event_queries_are_reviewed_clear(self):
        result = self._evaluate(
            primary=self._no_event_observation(
                "akshare.stock_dividend_cninfo.snapshot"
            ),
            cross=self._no_event_observation(
                "akshare.stock_history_dividend_detail.sina.snapshot"
            ),
        )
        self.assertEqual(result.manifest["review_status"], "reviewed_clear")
        self.assertEqual(result.manifest["query_result"], "no_event")
        self.assertEqual(result.manifest["action_count"], 0)
        self.assertNotIn("events", result.manifest)

    def test_event_and_no_event_conflict_fails_closed(self):
        cross = self._no_event_observation(
            "akshare.stock_history_dividend_detail.sina.snapshot"
        )
        with self.assertRaisesRegex(CorporateActionContractError, "source conflict"):
            self._evaluate(cross=cross)

    def test_failed_no_event_query_is_not_evidence(self):
        with self.assertRaisesRegex(CorporateActionContractError, "explicit"):
            self._no_event_observation(
                "akshare.stock_dividend_cninfo.snapshot", query_status="failed"
            )

    def test_no_event_query_success_must_be_explicit(self):
        with self.assertRaisesRegex(CorporateActionContractError, "explicit"):
            self._no_event_observation(
                "akshare.stock_dividend_cninfo.snapshot", query_status=None
            )

    def test_no_event_interval_mismatch_fails_closed(self):
        primary = self._no_event_observation(
            "akshare.stock_dividend_cninfo.snapshot"
        )
        cross = self._no_event_observation(
            "akshare.stock_history_dividend_detail.sina.snapshot",
            query_end="2026-01-07",
        )
        with self.assertRaisesRegex(CorporateActionContractError, "intervals must match"):
            self._evaluate(primary=primary, cross=cross)

    def test_no_event_future_source_snapshot_fails_closed(self):
        primary = self._no_event_observation(
            "akshare.stock_dividend_cninfo.snapshot",
            source_data_as_of="2026-01-09T10:01:00+08:00",
            fetched_at="2026-01-09T10:02:00+08:00",
        )
        cross = self._no_event_observation(
            "akshare.stock_history_dividend_detail.sina.snapshot"
        )
        with self.assertRaisesRegex(CorporateActionContractError, "before market_data_at"):
            self._evaluate(primary=primary, cross=cross)

    def test_date_conflict_fails_closed(self):
        conflicting = self._event(ex_date="2026-01-07")
        with self.assertRaisesRegex(CorporateActionContractError, "source conflict"):
            self._evaluate(cross_events=[conflicting])

    def test_term_conflict_fails_closed(self):
        conflicting = self._event(cash_per_share="0.4")
        with self.assertRaisesRegex(CorporateActionContractError, "source conflict"):
            self._evaluate(cross_events=[conflicting])

    def test_missing_payment_date_fails_closed(self):
        with self.assertRaisesRegex(CorporateActionContractError, "dates are incomplete"):
            self._event(payment_date=None)

    def test_future_known_event_fails_closed(self):
        event = self._event(known_at="2026-01-09T10:01:00+08:00")
        with self.assertRaisesRegex(CorporateActionContractError, "future-known"):
            self._evaluate(events=[event])

    def test_future_source_snapshot_fails_closed(self):
        primary = self._observation(
            "akshare.stock_dividend_cninfo.snapshot", [self._event()],
            source_data_as_of="2026-01-09T10:01:00+08:00",
            fetched_at="2026-01-09T10:02:00+08:00",
        )
        with self.assertRaisesRegex(CorporateActionContractError, "before market_data_at"):
            self._evaluate(primary=primary)

    def test_same_source_cannot_self_cross(self):
        event = self._event()
        primary = self._observation("same.snapshot", [event])
        cross = self._observation("same.snapshot", [event])
        with self.assertRaisesRegex(CorporateActionContractError, "independent"):
            self._evaluate(primary=primary, cross=cross)

    def test_unapproved_source_alias_fails_closed(self):
        event = self._event()
        primary = self._observation("unapproved.snapshot", [event])
        with self.assertRaisesRegex(CorporateActionContractError, "primary source"):
            self._evaluate(primary=primary)

    def test_event_date_outside_frozen_calendar_fails(self):
        event = self._event(record_date="2026-01-01")
        with self.assertRaisesRegex(CorporateActionContractError, "frozen trade calendar"):
            self._evaluate(events=[event])

    def test_suspension_date_mismatch_fails(self):
        event = self._event("suspension_resumption")
        bars = [
            self._bar("2026-01-05", trading=False),
            self._bar("2026-01-07", trading=False),
            self._bar("2026-01-08"),
        ]
        with self.assertRaisesRegex(CorporateActionContractError, "suspension dates"):
            self._evaluate(events=[event], raw_bars=bars)

    def test_resumption_must_be_active(self):
        event = self._event("suspension_resumption")
        bars = [
            self._bar("2026-01-05", trading=False),
            self._bar("2026-01-06", trading=False),
            self._bar("2026-01-07", trading=False),
            self._bar("2026-01-08", trading=False),
        ]
        with self.assertRaisesRegex(CorporateActionContractError, "suspension dates"):
            self._evaluate(events=[event], raw_bars=bars)

    def test_duplicate_events_are_rejected(self):
        event = self._event()
        with self.assertRaisesRegex(CorporateActionContractError, "duplicate identities"):
            self._observation("source", [event, event])

    def test_same_type_and_date_with_conflicting_terms_is_ambiguous(self):
        first = self._event(cash_per_share="0.4")
        second = self._event(cash_per_share="0.5")
        ordered = sorted(
            [first, second],
            key=lambda item: (item.effective_date, item.action_type, item.semantic_sha256),
        )
        with self.assertRaisesRegex(CorporateActionContractError, "duplicate identities"):
            self._observation("source", ordered)

    def test_unsorted_events_are_rejected(self):
        later = self._event("rights_issue")
        earlier = self._event()
        with self.assertRaisesRegex(CorporateActionContractError, "strictly sorted"):
            self._observation("source", [later, earlier])

    def test_manifest_is_stable_and_idempotent(self):
        first = self._evaluate().serialize()
        second = self._evaluate().serialize()
        self.assertEqual(first, second)
        self.assertIn(b"manifest_sha256", first)


if __name__ == "__main__":
    unittest.main()
