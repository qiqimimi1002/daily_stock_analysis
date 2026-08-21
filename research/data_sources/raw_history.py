"""Explicit, no-fallback raw-history adapters used only by research acceptance."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from research.benchmarks.raw_history import (
    CROSS_ADJUSTMENT,
    CROSS_RAW_SOURCE_ID,
    PRICE_BASIS,
    PRIMARY_ADJUSTMENT,
    PRIMARY_RAW_SOURCE_ID,
    RawDailyBar,
    RawHistoryContractError,
    RawHistoryObservation,
)
from research.benchmarks.schema import SHANGHAI_TZ


def _failure(source_id: str, reason: str) -> RawHistoryContractError:
    return RawHistoryContractError(f"{source_id} failed closed: {reason}")


def _interval(start: date, end: date) -> tuple[date, date]:
    if not isinstance(start, date) or isinstance(start, datetime):
        raise RawHistoryContractError("start must be date")
    if not isinstance(end, date) or isinstance(end, datetime):
        raise RawHistoryContractError("end must be date")
    if start > end:
        raise RawHistoryContractError("start cannot follow end")
    return start, end


def _market_symbol(symbol: str, *, dotted: bool) -> str:
    if len(symbol) != 6 or not symbol.isdigit():
        raise RawHistoryContractError("symbol must contain six digits")
    exchange = "sh" if symbol.startswith("6") else "sz"
    return f"{exchange}.{symbol}" if dotted else f"{exchange}{symbol}"


def _validate_baostock_iteration_end(result: Any) -> None:
    """Reject an ambiguous full terminal page instead of assuming completeness."""

    page_data = getattr(result, "data", None)
    per_page_count = getattr(result, "per_page_count", None)
    current_row = getattr(result, "cur_row_num", None)
    if page_data is None or per_page_count is None or current_row is None:
        return
    try:
        page_limit = int(per_page_count)
        row_index = int(current_row)
    except (TypeError, ValueError):
        raise _failure(PRIMARY_RAW_SOURCE_ID, "pagination_schema_error") from None
    if page_limit > 0 and len(page_data) == page_limit and row_index >= len(page_data):
        raise _failure(PRIMARY_RAW_SOURCE_ID, "pagination_incomplete")


def fetch_baostock_raw_history(symbol: str, start: date, end: date) -> RawHistoryObservation:
    """Fetch explicit adjustflag=3 daily bars; never fall back."""

    start, end = _interval(start, end)
    try:
        import baostock as bs
    except Exception as exc:
        raise _failure(PRIMARY_RAW_SOURCE_ID, f"import_error:{type(exc).__name__}")
    logged_in = False
    try:
        login = bs.login()
        if getattr(login, "error_code", None) != "0":
            raise _failure(PRIMARY_RAW_SOURCE_ID, "login_error")
        logged_in = True
        fields = "date,open,high,low,close,volume,amount,adjustflag,tradestatus"
        result = bs.query_history_k_data_plus(
            _market_symbol(symbol, dotted=True),
            fields,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag=PRIMARY_ADJUSTMENT,
        )
        if getattr(result, "error_code", None) != "0":
            raise _failure(PRIMARY_RAW_SOURCE_ID, "query_error")
        actual_fields = tuple(getattr(result, "fields", ()) or ())
        expected = tuple(fields.split(","))
        if actual_fields != expected:
            raise _failure(PRIMARY_RAW_SOURCE_ID, "schema_error")
        bars = []
        while result.next():
            row = dict(zip(actual_fields, result.get_row_data()))
            if row["adjustflag"] != PRIMARY_ADJUSTMENT:
                raise _failure(PRIMARY_RAW_SOURCE_ID, "adjustment_drift")
            if row["tradestatus"] not in {"0", "1"}:
                raise _failure(PRIMARY_RAW_SOURCE_ID, "trading_status_error")
            active = row["tradestatus"] == "1"
            bars.append(RawDailyBar.create(
                trade_date=row["date"], open=row["open"], high=row["high"],
                low=row["low"], close=row["close"], volume=row["volume"],
                amount=row["amount"], is_trading=active,
            ))
        _validate_baostock_iteration_end(result)
        return RawHistoryObservation.create(
            source_id=PRIMARY_RAW_SOURCE_ID, symbol=symbol,
            requested_start=start, requested_end=end,
            fetched_at=datetime.now(SHANGHAI_TZ), price_basis=PRICE_BASIS,
            adjustment=PRIMARY_ADJUSTMENT, volume_unit="share", amount_unit="CNY",
            bars=bars,
        )
    except RawHistoryContractError:
        raise
    except Exception as exc:
        raise _failure(PRIMARY_RAW_SOURCE_ID, type(exc).__name__) from None
    finally:
        if logged_in:
            try:
                bs.logout()
            except Exception:
                pass


def _frame_rows(frame: Any):
    if frame is None or getattr(frame, "empty", True):
        raise _failure(CROSS_RAW_SOURCE_ID, "empty_response")
    required = ("date", "open", "high", "low", "close", "volume", "amount")
    if not set(required).issubset({str(item) for item in frame.columns}):
        raise _failure(CROSS_RAW_SOURCE_ID, "schema_error")
    return frame.loc[:, list(required)].itertuples(index=False, name=None)


def fetch_akshare_sina_raw_history(symbol: str, start: date, end: date) -> RawHistoryObservation:
    """Fetch explicit adjust='' Sina daily bars through AKShare; never fall back."""

    start, end = _interval(start, end)
    try:
        import akshare as ak
    except Exception as exc:
        raise _failure(CROSS_RAW_SOURCE_ID, f"import_error:{type(exc).__name__}")
    try:
        frame = ak.stock_zh_a_daily(
            symbol=_market_symbol(symbol, dotted=False),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=CROSS_ADJUSTMENT,
        )
        bars = [RawDailyBar.create(
            trade_date=row[0], open=row[1], high=row[2], low=row[3], close=row[4],
            volume=row[5], amount=row[6], is_trading=True,
        ) for row in _frame_rows(frame)]
        return RawHistoryObservation.create(
            source_id=CROSS_RAW_SOURCE_ID, symbol=symbol,
            requested_start=start, requested_end=end,
            fetched_at=datetime.now(SHANGHAI_TZ), price_basis=PRICE_BASIS,
            adjustment=CROSS_ADJUSTMENT, volume_unit="share", amount_unit="CNY",
            bars=bars,
        )
    except RawHistoryContractError:
        raise
    except Exception as exc:
        raise _failure(CROSS_RAW_SOURCE_ID, type(exc).__name__) from None


def fetch_raw_history_pair(symbol: str, start: date, end: date, *, allow_network: bool = False):
    """Fetch both fixed sources sequentially; any source failure ends the run."""

    if allow_network is not True:
        raise RawHistoryContractError(
            "network access is disabled by default; pass allow_network=True explicitly"
        )
    primary = fetch_baostock_raw_history(symbol, start, end)
    cross = fetch_akshare_sina_raw_history(symbol, start, end)
    return primary, cross
