"""Read-only Baostock/AKShare adapters for the Phase 2B-0B1 contract."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Sequence

from research.benchmarks.schema import SHANGHAI_TZ
from research.benchmarks.trade_calendar import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    CalendarSourceObservation,
    TradeCalendarContractError,
    VerifiedTradeCalendar,
)


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


def _validated_interval(start: date | str, end: date | str) -> tuple[date, date]:
    query_start = _date(start, field="query_start")
    query_end = _date(end, field="query_end")
    if query_start > query_end:
        raise TradeCalendarContractError("query_start cannot be after query_end")
    return query_start, query_end


def _source_failure(source_id: str, message: str) -> TradeCalendarContractError:
    return TradeCalendarContractError(f"{source_id} failed closed: {message}")


def _validate_baostock_iteration_end(result: Any) -> None:
    if getattr(result, "error_code", None) != "0":
        raise _source_failure(PRIMARY_SOURCE_ID, "query_error")

    page_data = getattr(result, "data", None)
    per_page_count = getattr(result, "per_page_count", None)
    current_row = getattr(result, "cur_row_num", None)
    if page_data is None or per_page_count is None or current_row is None:
        return
    try:
        page_limit = int(per_page_count)
        row_index = int(current_row)
    except (TypeError, ValueError):
        raise _source_failure(PRIMARY_SOURCE_ID, "pagination_schema_error") from None
    if (
        page_limit > 0
        and len(page_data) == page_limit
        and row_index >= len(page_data)
    ):
        raise _source_failure(PRIMARY_SOURCE_ID, "pagination_incomplete")


def fetch_baostock_trade_dates(
    query_start: date | str,
    query_end: date | str,
) -> CalendarSourceObservation:
    """Fetch requested-interval sessions from Baostock without fallback."""

    start, end = _validated_interval(query_start, query_end)
    try:
        import baostock as bs
    except Exception as exc:
        raise _source_failure(PRIMARY_SOURCE_ID, f"import_error:{type(exc).__name__}")

    logged_in = False
    try:
        login_result = bs.login()
        if getattr(login_result, "error_code", None) != "0":
            raise _source_failure(PRIMARY_SOURCE_ID, "login_error")
        logged_in = True
        result = bs.query_trade_dates(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        if getattr(result, "error_code", None) != "0":
            raise _source_failure(PRIMARY_SOURCE_ID, "query_error")
        fields = tuple(getattr(result, "fields", ()) or ())
        if "calendar_date" not in fields or "is_trading_day" not in fields:
            raise _source_failure(PRIMARY_SOURCE_ID, "schema_error")
        calendar_index = fields.index("calendar_date")
        trading_index = fields.index("is_trading_day")
        row_dates = []
        trading_dates = []
        while result.next():
            row = result.get_row_data()
            if len(row) <= max(calendar_index, trading_index):
                raise _source_failure(PRIMARY_SOURCE_ID, "row_schema_error")
            row_date = _date(
                row[calendar_index],
                field="baostock.calendar_date",
            )
            if row_date < start or row_date > end:
                raise _source_failure(PRIMARY_SOURCE_ID, "out_of_range_date")
            row_dates.append(row_date)
            trading_flag = str(row[trading_index]).strip()
            if trading_flag not in {"0", "1"}:
                raise _source_failure(PRIMARY_SOURCE_ID, "invalid_trading_flag")
            if trading_flag == "1":
                trading_dates.append(row_date)
        _validate_baostock_iteration_end(result)
        if len(set(row_dates)) != len(row_dates):
            raise _source_failure(PRIMARY_SOURCE_ID, "duplicate_calendar_date")
        if tuple(sorted(row_dates)) != tuple(row_dates):
            raise _source_failure(PRIMARY_SOURCE_ID, "unsorted_calendar_date")
        observed_at = datetime.now(SHANGHAI_TZ)
        return CalendarSourceObservation(
            source_id=PRIMARY_SOURCE_ID,
            query_start=start,
            query_end=end,
            trading_dates=tuple(trading_dates),
            source_data_as_of=observed_at,
            fetched_at=observed_at,
        )
    except TradeCalendarContractError:
        raise
    except Exception as exc:
        raise _source_failure(PRIMARY_SOURCE_ID, type(exc).__name__) from None
    finally:
        if logged_in:
            try:
                bs.logout()
            except Exception:
                pass


def _akshare_trade_dates(frame: Any) -> Sequence[date]:
    if frame is None or getattr(frame, "empty", True):
        raise _source_failure(CROSS_SOURCE_ID, "empty_response")
    columns = tuple(str(item) for item in getattr(frame, "columns", ()))
    if "trade_date" not in columns:
        raise _source_failure(CROSS_SOURCE_ID, "schema_error")
    return tuple(
        _date(value, field="akshare.trade_date") for value in frame["trade_date"]
    )


def fetch_akshare_trade_dates(
    query_start: date | str,
    query_end: date | str,
) -> CalendarSourceObservation:
    """Fetch Sina's published calendar through AKShare without fallback."""

    start, end = _validated_interval(query_start, query_end)
    try:
        import akshare as ak
    except Exception as exc:
        raise _source_failure(CROSS_SOURCE_ID, f"import_error:{type(exc).__name__}")
    try:
        all_dates = _akshare_trade_dates(ak.tool_trade_date_hist_sina())
        interval_dates = tuple(item for item in all_dates if start <= item <= end)
        observed_at = datetime.now(SHANGHAI_TZ)
        return CalendarSourceObservation(
            source_id=CROSS_SOURCE_ID,
            query_start=start,
            query_end=end,
            trading_dates=interval_dates,
            source_data_as_of=observed_at,
            fetched_at=observed_at,
        )
    except TradeCalendarContractError:
        raise
    except Exception as exc:
        raise _source_failure(CROSS_SOURCE_ID, type(exc).__name__) from None


def fetch_verified_trade_calendar(
    query_start: date | str,
    query_end: date | str,
    *,
    allow_network: bool = False,
) -> VerifiedTradeCalendar:
    """Fetch both fixed sources; network is disabled unless explicitly opted in."""

    start, end = _validated_interval(query_start, query_end)
    if allow_network is not True:
        raise TradeCalendarContractError(
            "network access is disabled by default; pass allow_network=True explicitly"
        )
    try:
        primary = fetch_baostock_trade_dates(start, end)
    except Exception as exc:
        if isinstance(exc, TradeCalendarContractError):
            raise
        raise _source_failure(PRIMARY_SOURCE_ID, type(exc).__name__) from None
    try:
        cross = fetch_akshare_trade_dates(start, end)
    except Exception as exc:
        if isinstance(exc, TradeCalendarContractError):
            raise
        raise _source_failure(CROSS_SOURCE_ID, type(exc).__name__) from None
    return VerifiedTradeCalendar.create(
        query_start=start,
        query_end=end,
        primary=primary,
        cross=cross,
    )
