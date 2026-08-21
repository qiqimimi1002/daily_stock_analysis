"""Phase 2B-0B1 point-in-time trade-calendar and no-lookahead contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
from typing import Any, Dict, Optional, Sequence, Tuple

from research.benchmarks.schema import (
    SHANGHAI_TZ,
    BenchmarkValidationError,
    canonical_json_bytes,
)


TRADE_CALENDAR_SCHEMA_VERSION = "phase2b-0b1-trade-calendar-v1"
TRADE_CALENDAR_CALCULATION_VERSION = "trade-calendar-dual-source-v1"
HISTORY_WINDOW_SCHEMA_VERSION = "phase2b-0b1-history-window-v1"
HISTORY_WINDOW_CALCULATION_VERSION = "completed-daily-bars-v1"
PRIMARY_SOURCE_ID = "baostock.query_trade_dates"
CROSS_SOURCE_ID = "akshare.tool_trade_date_hist_sina"
A_SHARE_DAILY_BAR_COMPLETION_TIME = time(15, 0)
CONSISTENCY_PASS = "pass"


class TradeCalendarContractError(BenchmarkValidationError):
    """Raised when calendar or history evidence cannot be accepted safely."""


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        raise TradeCalendarContractError(f"{field} must be a date")
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise TradeCalendarContractError(
            f"{field} must be an ISO-8601 date"
        ) from None
    if parsed.isoformat() != text:
        raise TradeCalendarContractError(
            f"{field} must use canonical YYYY-MM-DD format"
        )
    return parsed


def _shanghai_datetime(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            raise TradeCalendarContractError(
                f"{field} must be an ISO-8601 datetime"
            ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TradeCalendarContractError(f"{field} must include a timezone offset")
    if parsed.utcoffset() != timedelta(hours=8):
        raise TradeCalendarContractError(
            f"{field} must use Asia/Shanghai timezone semantics"
        )
    timezone_key = getattr(parsed.tzinfo, "key", None)
    if timezone_key is not None and timezone_key != "Asia/Shanghai":
        raise TradeCalendarContractError(
            f"{field} must use Asia/Shanghai timezone semantics"
        )
    return parsed.astimezone(SHANGHAI_TZ)


def _normalized_trade_dates(
    values: Sequence[date | str],
    *,
    field: str,
    query_start: date,
    query_end: date,
) -> Tuple[date, ...]:
    parsed = tuple(_date(value, field=field) for value in values)
    if not parsed:
        raise TradeCalendarContractError(f"{field} cannot be empty")
    if len(set(parsed)) != len(parsed):
        raise TradeCalendarContractError(f"{field} cannot contain duplicate dates")
    if tuple(sorted(parsed)) != parsed:
        raise TradeCalendarContractError(
            f"{field} must be strictly increasing before comparison"
        )
    if any(item < query_start or item > query_end for item in parsed):
        raise TradeCalendarContractError(
            f"{field} cannot contain dates outside the requested interval"
        )
    return parsed


def _content_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class CalendarSourceObservation:
    """One provider's requested-interval trading dates and audit times."""

    source_id: str
    query_start: date | str
    query_end: date | str
    trading_dates: Sequence[date | str]
    source_data_as_of: datetime | str
    fetched_at: datetime | str


@dataclass(frozen=True, init=False)
class VerifiedTradeCalendar:
    """A calendar that exists only after strict two-source agreement."""

    query_start: date
    query_end: date
    primary_source_id: str
    cross_source_id: str
    primary_source_data_as_of: datetime
    primary_fetched_at: datetime
    cross_source_data_as_of: datetime
    cross_fetched_at: datetime
    primary_count: int
    cross_count: int
    consistency_status: str
    trading_dates: Tuple[date, ...]
    schema_version: str
    calculation_version: str
    content_sha256: str

    @classmethod
    def create(
        cls,
        *,
        query_start: date | str,
        query_end: date | str,
        primary: CalendarSourceObservation,
        cross: CalendarSourceObservation,
    ) -> "VerifiedTradeCalendar":
        start = _date(query_start, field="query_start")
        end = _date(query_end, field="query_end")
        if start > end:
            raise TradeCalendarContractError("query_start cannot be after query_end")

        observations = (
            ("primary", primary, PRIMARY_SOURCE_ID),
            ("cross", cross, CROSS_SOURCE_ID),
        )
        normalized = {}
        audit_times = {}
        for role, observation, expected_source in observations:
            if observation.source_id != expected_source:
                raise TradeCalendarContractError(
                    f"{role} source_id must be {expected_source}"
                )
            observed_start = _date(
                observation.query_start,
                field=f"{role}.query_start",
            )
            observed_end = _date(
                observation.query_end,
                field=f"{role}.query_end",
            )
            if observed_start != start or observed_end != end:
                raise TradeCalendarContractError(
                    f"{role} source interval must match the requested interval"
                )
            source_time = _shanghai_datetime(
                observation.source_data_as_of,
                field=f"{role}.source_data_as_of",
            )
            fetched_time = _shanghai_datetime(
                observation.fetched_at,
                field=f"{role}.fetched_at",
            )
            if source_time > fetched_time:
                raise TradeCalendarContractError(
                    f"{role}.source_data_as_of cannot be later than fetched_at"
                )
            normalized[role] = _normalized_trade_dates(
                observation.trading_dates,
                field=f"{role}.trading_dates",
                query_start=start,
                query_end=end,
            )
            audit_times[role] = (source_time, fetched_time)

        if normalized["primary"] != normalized["cross"]:
            primary_dates = set(normalized["primary"])
            cross_dates = set(normalized["cross"])
            missing_from_cross = sorted(primary_dates - cross_dates)
            extra_in_cross = sorted(cross_dates - primary_dates)
            raise TradeCalendarContractError(
                "trade-calendar sources disagree: "
                f"primary_count={len(normalized['primary'])}, "
                f"cross_count={len(normalized['cross'])}, "
                f"missing_from_cross={[item.isoformat() for item in missing_from_cross]}, "
                f"extra_in_cross={[item.isoformat() for item in extra_in_cross]}"
            )

        payload = {
            "calculation_version": TRADE_CALENDAR_CALCULATION_VERSION,
            "consistency_status": CONSISTENCY_PASS,
            "cross_source": {
                "fetched_at": audit_times["cross"][1].isoformat(timespec="seconds"),
                "normalized_trade_date_count": len(normalized["cross"]),
                "source_data_as_of": audit_times["cross"][0].isoformat(
                    timespec="seconds"
                ),
                "source_id": CROSS_SOURCE_ID,
            },
            "primary_source": {
                "fetched_at": audit_times["primary"][1].isoformat(
                    timespec="seconds"
                ),
                "normalized_trade_date_count": len(normalized["primary"]),
                "source_data_as_of": audit_times["primary"][0].isoformat(
                    timespec="seconds"
                ),
                "source_id": PRIMARY_SOURCE_ID,
            },
            "query_end": end.isoformat(),
            "query_start": start.isoformat(),
            "schema_version": TRADE_CALENDAR_SCHEMA_VERSION,
            "trading_dates": [item.isoformat() for item in normalized["primary"]],
        }
        return _frozen_instance(
            cls,
            {
                "query_start": start,
                "query_end": end,
                "primary_source_id": PRIMARY_SOURCE_ID,
                "cross_source_id": CROSS_SOURCE_ID,
                "primary_source_data_as_of": audit_times["primary"][0],
                "primary_fetched_at": audit_times["primary"][1],
                "cross_source_data_as_of": audit_times["cross"][0],
                "cross_fetched_at": audit_times["cross"][1],
                "primary_count": len(normalized["primary"]),
                "cross_count": len(normalized["cross"]),
                "consistency_status": CONSISTENCY_PASS,
                "trading_dates": normalized["primary"],
                "schema_version": TRADE_CALENDAR_SCHEMA_VERSION,
                "calculation_version": TRADE_CALENDAR_CALCULATION_VERSION,
                "content_sha256": _content_hash(payload),
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calculation_version": self.calculation_version,
            "consistency_status": self.consistency_status,
            "content_sha256": self.content_sha256,
            "cross_source": {
                "fetched_at": self.cross_fetched_at.isoformat(timespec="seconds"),
                "normalized_trade_date_count": self.cross_count,
                "source_data_as_of": self.cross_source_data_as_of.isoformat(
                    timespec="seconds"
                ),
                "source_id": self.cross_source_id,
            },
            "primary_source": {
                "fetched_at": self.primary_fetched_at.isoformat(
                    timespec="seconds"
                ),
                "normalized_trade_date_count": self.primary_count,
                "source_data_as_of": self.primary_source_data_as_of.isoformat(
                    timespec="seconds"
                ),
                "source_id": self.primary_source_id,
            },
            "query_end": self.query_end.isoformat(),
            "query_start": self.query_start.isoformat(),
            "schema_version": self.schema_version,
            "trading_dates": [item.isoformat() for item in self.trading_dates],
        }

    def serialize(self) -> bytes:
        return canonical_json_bytes(self.to_dict()) + b"\n"

    def previous_completed_trade_date(
        self,
        market_data_at: datetime | str,
    ) -> date:
        local_market_time = _shanghai_datetime(
            market_data_at,
            field="market_data_at",
        )
        signal_date = local_market_time.date()
        if signal_date < self.query_start or signal_date > self.query_end:
            raise TradeCalendarContractError(
                "market_data_at date must be covered by the verified query interval"
            )
        eligible = [item for item in self.trading_dates if item < signal_date]
        if (
            signal_date in self.trading_dates
            and local_market_time.time() >= A_SHARE_DAILY_BAR_COMPLETION_TIME
        ):
            eligible.append(signal_date)
        if not eligible:
            raise TradeCalendarContractError(
                "verified calendar does not contain a completed prior trade date"
            )
        return eligible[-1]


@dataclass(frozen=True, init=False)
class HistoryWindowContract:
    """Exact consecutive completed-market-session window for daily bars."""

    market_data_at: datetime
    history_data_as_of: datetime
    source_data_as_of: datetime
    fetched_at: Optional[datetime]
    generated_at: datetime
    previous_completed_trade_date: date
    required_trade_dates: Tuple[date, ...]
    calendar_content_sha256: str
    schema_version: str
    calculation_version: str
    content_sha256: str

    @classmethod
    def create(
        cls,
        *,
        calendar: VerifiedTradeCalendar,
        market_data_at: datetime | str,
        history_data_as_of: datetime | str,
        source_data_as_of: datetime | str,
        fetched_at: datetime | str | None,
        generated_at: datetime | str,
        required_observations: int,
        observed_trade_dates: Sequence[date | str],
    ) -> "HistoryWindowContract":
        if (
            not isinstance(required_observations, int)
            or isinstance(required_observations, bool)
            or required_observations < 1
        ):
            raise TradeCalendarContractError(
                "required_observations must be a positive integer"
            )
        market_time = _shanghai_datetime(market_data_at, field="market_data_at")
        history_time = _shanghai_datetime(
            history_data_as_of,
            field="history_data_as_of",
        )
        source_time = _shanghai_datetime(
            source_data_as_of,
            field="source_data_as_of",
        )
        generated_time = _shanghai_datetime(generated_at, field="generated_at")
        fetched_time = (
            _shanghai_datetime(fetched_at, field="fetched_at")
            if fetched_at is not None
            else None
        )
        if not history_time <= source_time <= market_time <= generated_time:
            raise TradeCalendarContractError(
                "history_data_as_of <= source_data_as_of <= market_data_at "
                "<= generated_at is required"
            )
        if (
            calendar.primary_source_data_as_of > market_time
            or calendar.cross_source_data_as_of > market_time
        ):
            raise TradeCalendarContractError(
                "calendar source_data_as_of cannot be later than market_data_at"
            )

        cutoff = calendar.previous_completed_trade_date(market_time)
        if history_time.date() != cutoff:
            raise TradeCalendarContractError(
                "history_data_as_of must identify the completed cutoff daily bar"
            )
        if history_time.time() < A_SHARE_DAILY_BAR_COMPLETION_TIME:
            raise TradeCalendarContractError(
                "history_data_as_of cannot identify a daily bar before completion"
            )
        available = tuple(item for item in calendar.trading_dates if item <= cutoff)
        if len(available) < required_observations:
            raise TradeCalendarContractError(
                "verified calendar does not contain the required history window"
            )
        expected = available[-required_observations:]
        observed = _normalized_trade_dates(
            observed_trade_dates,
            field="observed_trade_dates",
            query_start=calendar.query_start,
            query_end=calendar.query_end,
        )
        if any(item > cutoff for item in observed):
            raise TradeCalendarContractError(
                "observed history contains an uncompleted or future trade date"
            )
        if observed != expected:
            raise TradeCalendarContractError(
                "observed history must contain exactly the required consecutive "
                "market trade dates; filling, shortening and substitution are forbidden"
            )

        payload = {
            "calculation_version": HISTORY_WINDOW_CALCULATION_VERSION,
            "calendar_content_sha256": calendar.content_sha256,
            "fetched_at": (
                fetched_time.isoformat(timespec="seconds")
                if fetched_time is not None
                else None
            ),
            "generated_at": generated_time.isoformat(timespec="seconds"),
            "history_data_as_of": history_time.isoformat(timespec="seconds"),
            "market_data_at": market_time.isoformat(timespec="seconds"),
            "previous_completed_trade_date": cutoff.isoformat(),
            "required_trade_dates": [item.isoformat() for item in expected],
            "schema_version": HISTORY_WINDOW_SCHEMA_VERSION,
            "source_data_as_of": source_time.isoformat(timespec="seconds"),
        }
        return _frozen_instance(
            cls,
            {
                "market_data_at": market_time,
                "history_data_as_of": history_time,
                "source_data_as_of": source_time,
                "fetched_at": fetched_time,
                "generated_at": generated_time,
                "previous_completed_trade_date": cutoff,
                "required_trade_dates": expected,
                "calendar_content_sha256": calendar.content_sha256,
                "schema_version": HISTORY_WINDOW_SCHEMA_VERSION,
                "calculation_version": HISTORY_WINDOW_CALCULATION_VERSION,
                "content_sha256": _content_hash(payload),
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "calculation_version": self.calculation_version,
            "calendar_content_sha256": self.calendar_content_sha256,
            "content_sha256": self.content_sha256,
            "fetched_at": (
                self.fetched_at.isoformat(timespec="seconds")
                if self.fetched_at is not None
                else None
            ),
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
            "history_data_as_of": self.history_data_as_of.isoformat(
                timespec="seconds"
            ),
            "market_data_at": self.market_data_at.isoformat(timespec="seconds"),
            "previous_completed_trade_date": (
                self.previous_completed_trade_date.isoformat()
            ),
            "required_trade_dates": [
                item.isoformat() for item in self.required_trade_dates
            ],
            "schema_version": self.schema_version,
            "source_data_as_of": self.source_data_as_of.isoformat(
                timespec="seconds"
            ),
        }

    def serialize(self) -> bytes:
        return canonical_json_bytes(self.to_dict()) + b"\n"


def _frozen_instance(cls, values: Dict[str, Any]):
    instance = object.__new__(cls)
    for field, value in values.items():
        object.__setattr__(instance, field, value)
    return instance
