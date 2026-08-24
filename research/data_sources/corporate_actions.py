"""Private-only dual-source corporate-action acquisition.

The provider rows remain in the Private request.  This adapter only normalizes
the already accepted CNINFO and Sina interfaces into the frozen Phase 2B
contract; it has no fallback source.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping

import pandas as pd

from research.benchmarks.corporate_actions import (
    CorporateActionEvent,
    CorporateActionObservation,
    QUERY_RESULT_EVENTS,
    QUERY_RESULT_NO_EVENT,
    QUERY_STATUS_SUCCESS,
)
from research.benchmarks.schema import SHANGHAI_TZ


PRIMARY_SOURCE_ID = "issuer_exchange.implementation_disclosure"
CROSS_SOURCE_ID = "akshare.stock_history_dividend_detail.sina.snapshot"


class CorporateActionSourceError(RuntimeError):
    """A source failed or could not prove a complete normalized result."""


def _date_value(value: Any, *, field: str, required: bool = True) -> date | None:
    if value is None or pd.isna(value) or str(value).strip() in {"", "--", "-"}:
        if required:
            raise CorporateActionSourceError(f"{field} is missing")
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise CorporateActionSourceError(f"{field} is invalid")
    return parsed.date()


def _number(value: Any, *, field: str) -> Decimal:
    if value is None or pd.isna(value) or str(value).strip() in {"", "--", "-"}:
        return Decimal("0")
    try:
        parsed = Decimal(str(value).strip())
    except Exception as exc:
        raise CorporateActionSourceError(f"{field} is invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CorporateActionSourceError(f"{field} is invalid")
    return parsed


def _per_share(value: Any, *, field: str) -> Decimal:
    return _number(value, field=field) / Decimal("10")


def _in_window(value: date | None, start: date, end: date) -> bool:
    return value is not None and start <= value <= end


def _distribution_event(
    *,
    symbol: str,
    known_at: datetime,
    record_date: date,
    ex_date: date,
    payment_date: date | None,
    listing_date: date | None,
    cash_per_share: Decimal,
    stock_ratio: Decimal,
) -> CorporateActionEvent:
    if stock_ratio > 0:
        if listing_date is None or (cash_per_share > 0 and payment_date is None):
            raise CorporateActionSourceError("implemented stock distribution dates are incomplete")
        return CorporateActionEvent.create(
            symbol=symbol,
            action_type="stock_dividend_or_transfer",
            known_at=known_at,
            record_date=record_date,
            ex_date=ex_date,
            payment_date=payment_date,
            listing_date=listing_date,
            cash_per_share=cash_per_share,
            stock_ratio=stock_ratio,
        )
    if cash_per_share > 0:
        if payment_date is None:
            raise CorporateActionSourceError("implemented cash payment date is incomplete")
        return CorporateActionEvent.create(
            symbol=symbol,
            action_type="cash_dividend",
            known_at=known_at,
            record_date=record_date,
            ex_date=ex_date,
            payment_date=payment_date,
            cash_per_share=cash_per_share,
        )
    raise CorporateActionSourceError("implemented distribution has no supported terms")


def cninfo_events(
    symbol: str,
    start: date,
    end: date,
    *,
    fetched_at: datetime,
    dividends: pd.DataFrame,
    allotments: pd.DataFrame,
) -> tuple[CorporateActionEvent, ...]:
    """Normalize successful CNINFO dividend and allotment snapshots."""

    events = []
    for row in dividends.to_dict("records"):
        ex_date = _date_value(row.get("除权日"), field="CNINFO 除权日", required=False)
        if not _in_window(ex_date, start, end):
            continue
        events.append(
            _distribution_event(
                symbol=symbol,
                known_at=fetched_at,
                record_date=_date_value(row.get("股权登记日"), field="CNINFO 股权登记日"),
                ex_date=ex_date,
                payment_date=_date_value(row.get("派息日"), field="CNINFO 派息日", required=False),
                listing_date=_date_value(row.get("股份到账日"), field="CNINFO 股份到账日", required=False),
                cash_per_share=_per_share(row.get("派息比例"), field="CNINFO 派息比例"),
                stock_ratio=(
                    _per_share(row.get("送股比例"), field="CNINFO 送股比例")
                    + _per_share(row.get("转增比例"), field="CNINFO 转增比例")
                ),
            )
        )
    for row in allotments.to_dict("records"):
        ex_date = _date_value(row.get("除权基准日"), field="CNINFO 配股除权日", required=False)
        if not _in_window(ex_date, start, end):
            continue
        events.append(
            CorporateActionEvent.create(
                symbol=symbol,
                action_type="rights_issue",
                known_at=fetched_at,
                record_date=_date_value(row.get("股权登记日"), field="CNINFO 配股登记日"),
                ex_date=ex_date,
                listing_date=_date_value(row.get("配股上市日"), field="CNINFO 配股上市日"),
                rights_ratio=_per_share(row.get("配股比例"), field="CNINFO 配股比例"),
                rights_price=_number(row.get("配股价格"), field="CNINFO 配股价格"),
            )
        )
    return tuple(sorted(events, key=lambda item: (item.effective_date, item.action_type)))


def _detail_date(frame: pd.DataFrame, *, contains: str) -> date | None:
    if frame is None or frame.empty or len(frame.columns) < 2:
        return None
    for row in frame.itertuples(index=False, name=None):
        if contains in str(row[0]):
            return _date_value(row[1], field=f"Sina {contains}", required=False)
    return None


def sina_events(
    symbol: str,
    start: date,
    end: date,
    *,
    fetched_at: datetime,
    dividends: pd.DataFrame,
    allotments: pd.DataFrame,
    dividend_detail_fetcher: Callable[[str], pd.DataFrame],
) -> tuple[CorporateActionEvent, ...]:
    """Normalize successful Sina dividend and allotment snapshots."""

    events = []
    for row in dividends.to_dict("records"):
        ex_date = _date_value(row.get("除权除息日"), field="Sina 除权除息日", required=False)
        if not _in_window(ex_date, start, end):
            continue
        cash = _per_share(row.get("派息"), field="Sina 派息")
        stock = (
            _per_share(row.get("送股"), field="Sina 送股")
            + _per_share(row.get("转增"), field="Sina 转增")
        )
        detail = (
            dividend_detail_fetcher(
                _date_value(row.get("公告日期"), field="Sina 公告日期").isoformat()
            )
            if cash > 0
            else pd.DataFrame()
        )
        events.append(
            _distribution_event(
                symbol=symbol,
                known_at=fetched_at,
                record_date=_date_value(row.get("股权登记日"), field="Sina 股权登记日"),
                ex_date=ex_date,
                payment_date=_detail_date(detail, contains="派息日"),
                listing_date=_date_value(row.get("红股上市日"), field="Sina 红股上市日", required=False),
                cash_per_share=cash,
                stock_ratio=stock,
            )
        )
    for row in allotments.to_dict("records"):
        ex_date = _date_value(row.get("除权日"), field="Sina 配股除权日", required=False)
        if not _in_window(ex_date, start, end):
            continue
        events.append(
            CorporateActionEvent.create(
                symbol=symbol,
                action_type="rights_issue",
                known_at=fetched_at,
                record_date=_date_value(row.get("股权登记日"), field="Sina 配股登记日"),
                ex_date=ex_date,
                listing_date=_date_value(row.get("配股上市日"), field="Sina 配股上市日"),
                rights_ratio=_per_share(row.get("配股方案"), field="Sina 配股方案"),
                rights_price=_number(row.get("配股价格"), field="Sina 配股价格"),
            )
        )
    return tuple(sorted(events, key=lambda item: (item.effective_date, item.action_type)))


def _observation(
    *,
    source_id: str,
    symbol: str,
    start: date,
    end: date,
    fetched_at: datetime,
    events: tuple[CorporateActionEvent, ...],
) -> CorporateActionObservation:
    return CorporateActionObservation.create(
        source_id=source_id,
        source_data_as_of=fetched_at,
        fetched_at=fetched_at,
        symbol=symbol,
        query_start=start,
        query_end=end,
        query_status=QUERY_STATUS_SUCCESS,
        query_result=QUERY_RESULT_EVENTS if events else QUERY_RESULT_NO_EVENT,
        events=events,
    )


def fetch_corporate_action_pair(
    symbol: str,
    start: date,
    end: date,
    *,
    allow_network: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> tuple[CorporateActionObservation, CorporateActionObservation]:
    """Fetch one strict CNINFO/Sina pair with no fallback or partial success."""

    if allow_network is not True:
        raise CorporateActionSourceError("explicit network opt-in is required")
    now = clock or (lambda: datetime.now(SHANGHAI_TZ))
    try:
        import akshare as ak

        primary_dividends = ak.stock_dividend_cninfo(symbol=symbol)
        primary_allotments = ak.stock_allotment_cninfo(
            symbol=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        primary_fetched_at = now()
        cross_dividends = ak.stock_history_dividend_detail(
            symbol=symbol, indicator="分红", date=""
        )
        cross_allotments = ak.stock_history_dividend_detail(
            symbol=symbol, indicator="配股", date=""
        )
        details = {}
        for row in cross_dividends.to_dict("records"):
            ex_date = _date_value(
                row.get("除权除息日"), field="Sina 除权除息日", required=False
            )
            if (
                _in_window(ex_date, start, end)
                and _per_share(row.get("派息"), field="Sina 派息") > 0
            ):
                announcement = _date_value(
                    row.get("公告日期"), field="Sina 公告日期"
                ).isoformat()
                details[announcement] = ak.stock_history_dividend_detail(
                    symbol=symbol, indicator="分红", date=announcement
                )
        cross_fetched_at = now()
        primary_events = cninfo_events(
            symbol,
            start,
            end,
            fetched_at=primary_fetched_at,
            dividends=primary_dividends,
            allotments=primary_allotments,
        )
        cross_events = sina_events(
            symbol,
            start,
            end,
            fetched_at=cross_fetched_at,
            dividends=cross_dividends,
            allotments=cross_allotments,
            dividend_detail_fetcher=lambda announcement: details[announcement],
        )
    except CorporateActionSourceError:
        raise
    except Exception as exc:
        raise CorporateActionSourceError(type(exc).__name__) from exc

    left = tuple(item.semantic_sha256 for item in primary_events)
    right = tuple(item.semantic_sha256 for item in cross_events)
    if left != right:
        raise CorporateActionSourceError("independent source conflict")
    return (
        _observation(
            source_id=PRIMARY_SOURCE_ID,
            symbol=symbol,
            start=start,
            end=end,
            fetched_at=primary_fetched_at,
            events=primary_events,
        ),
        _observation(
            source_id=CROSS_SOURCE_ID,
            symbol=symbol,
            start=start,
            end=end,
            fetched_at=cross_fetched_at,
            events=cross_events,
        ),
    )


def observation_payload(observation: CorporateActionObservation) -> Mapping[str, Any]:
    """Serialize complete Private evidence for the existing request parser."""

    return {
        "events": [event.evidence_dict() for event in observation.events],
        "fetched_at": observation.fetched_at.isoformat(timespec="seconds"),
        "query_end": observation.query_end.isoformat() if observation.query_end else None,
        "query_result": observation.query_result,
        "query_start": observation.query_start.isoformat() if observation.query_start else None,
        "query_status": observation.query_status,
        "source_data_as_of": observation.source_data_as_of.isoformat(timespec="seconds"),
        "source_id": observation.source_id,
        "symbol": observation.symbol,
    }
