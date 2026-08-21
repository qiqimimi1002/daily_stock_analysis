"""Fail-closed Phase 2B raw, unadjusted daily-history acceptance contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Dict, Mapping, Sequence, Tuple

from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.trade_calendar import VerifiedTradeCalendar


RAW_HISTORY_SCHEMA_VERSION = "phase2b-raw-history-v1"
RAW_HISTORY_CALCULATION_VERSION = "dual-source-raw-daily-v1"
PRIMARY_RAW_SOURCE_ID = "baostock.query_history_k_data_plus.raw"
CROSS_RAW_SOURCE_ID = "akshare.stock_zh_a_daily.sina.raw"
PRICE_BASIS = "raw_unadjusted"
PRIMARY_ADJUSTMENT = "3"
CROSS_ADJUSTMENT = ""
AMOUNT_TOLERANCE_CNY = Decimal("0.50")
ACCEPTANCE_STATUS = "conditional_pass"
_SYMBOL_RE = re.compile(r"^[0-9]{6}$")


class RawHistoryContractError(ValueError):
    """Raised when raw-history evidence cannot be accepted safely."""


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        raise RawHistoryContractError(f"{field} must be a date")
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise RawHistoryContractError(f"{field} must be an ISO-8601 date") from None
    if text != parsed.isoformat():
        raise RawHistoryContractError(f"{field} must use canonical YYYY-MM-DD")
    return parsed


def _time(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise RawHistoryContractError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RawHistoryContractError(f"{field} must include timezone")
    if parsed.utcoffset() != timedelta(hours=8):
        raise RawHistoryContractError(f"{field} must use Asia/Shanghai semantics")
    timezone_key = getattr(parsed.tzinfo, "key", None)
    if timezone_key is not None and timezone_key != "Asia/Shanghai":
        raise RawHistoryContractError(f"{field} must use Asia/Shanghai semantics")
    return parsed.astimezone(SHANGHAI_TZ)


def _decimal(value: Any, *, field: str, nullable: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if nullable:
            return None
        raise RawHistoryContractError(f"{field} is required")
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation:
        raise RawHistoryContractError(f"{field} must be finite decimal") from None
    if not parsed.is_finite():
        raise RawHistoryContractError(f"{field} must be finite decimal")
    return parsed


def _canonical_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    return format(normalized, "f")


@dataclass(frozen=True)
class RawDailyBar:
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    amount: Decimal | None
    is_trading: bool

    @classmethod
    def create(
        cls,
        *,
        trade_date: date | str,
        open: Any,
        high: Any,
        low: Any,
        close: Any,
        volume: Any,
        amount: Any,
        is_trading: bool,
    ) -> "RawDailyBar":
        if not isinstance(is_trading, bool):
            raise RawHistoryContractError("is_trading must be bool")
        values = {
            name: _decimal(value, field=name, nullable=not is_trading)
            for name, value in {
                "open": open,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }.items()
        }
        if is_trading:
            if any(values[name] <= 0 for name in ("open", "high", "low", "close")):
                raise RawHistoryContractError("active OHLC values must be positive")
            if values["high"] < max(values["open"], values["low"], values["close"]):
                raise RawHistoryContractError("high violates OHLC bounds")
            if values["low"] > min(values["open"], values["high"], values["close"]):
                raise RawHistoryContractError("low violates OHLC bounds")
            if values["volume"] < 0 or values["volume"] != values["volume"].to_integral_value():
                raise RawHistoryContractError("volume must be non-negative whole shares")
            if values["amount"] < 0:
                raise RawHistoryContractError("amount must be non-negative CNY")
        else:
            if values["volume"] not in (None, Decimal("0")) or values["amount"] not in (None, Decimal("0")):
                raise RawHistoryContractError("suspended rows must have zero volume and amount")
            prices = [values[name] for name in ("open", "high", "low", "close")]
            has_carried_prices = any(value not in (None, Decimal("0")) for value in prices)
            if has_carried_prices:
                if any(value is None or value <= 0 for value in prices):
                    raise RawHistoryContractError("suspended carried OHLC must be complete and positive")
                if values["high"] < max(values["open"], values["low"], values["close"]):
                    raise RawHistoryContractError("suspended carried high violates OHLC bounds")
                if values["low"] > min(values["open"], values["high"], values["close"]):
                    raise RawHistoryContractError("suspended carried low violates OHLC bounds")
        return cls(trade_date=_date(trade_date, field="trade_date"), is_trading=is_trading, **values)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "amount": _canonical_decimal(self.amount),
            "close": _canonical_decimal(self.close),
            "high": _canonical_decimal(self.high),
            "is_trading": self.is_trading,
            "low": _canonical_decimal(self.low),
            "open": _canonical_decimal(self.open),
            "trade_date": self.trade_date.isoformat(),
            "volume": _canonical_decimal(self.volume),
        }


@dataclass(frozen=True)
class RawHistoryObservation:
    source_id: str
    symbol: str
    requested_start: date
    requested_end: date
    fetched_at: datetime
    price_basis: str
    adjustment: str
    volume_unit: str
    amount_unit: str
    bars: Tuple[RawDailyBar, ...]
    content_sha256: str

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        symbol: str,
        requested_start: date | str,
        requested_end: date | str,
        fetched_at: datetime | str,
        price_basis: str,
        adjustment: str,
        volume_unit: str,
        amount_unit: str,
        bars: Sequence[RawDailyBar],
    ) -> "RawHistoryObservation":
        if source_id not in {PRIMARY_RAW_SOURCE_ID, CROSS_RAW_SOURCE_ID}:
            raise RawHistoryContractError("unexpected raw-history source")
        if not _SYMBOL_RE.fullmatch(str(symbol)):
            raise RawHistoryContractError("symbol must contain six digits")
        start = _date(requested_start, field="requested_start")
        end = _date(requested_end, field="requested_end")
        if start > end:
            raise RawHistoryContractError("requested_start cannot follow requested_end")
        expected_adjustment = PRIMARY_ADJUSTMENT if source_id == PRIMARY_RAW_SOURCE_ID else CROSS_ADJUSTMENT
        if price_basis != PRICE_BASIS or adjustment != expected_adjustment:
            raise RawHistoryContractError("source must explicitly declare raw unadjusted data")
        if volume_unit != "share" or amount_unit != "CNY":
            raise RawHistoryContractError("volume/amount units must be share and CNY")
        normalized = tuple(bars)
        if not normalized:
            raise RawHistoryContractError("raw-history response cannot be empty")
        dates = tuple(item.trade_date for item in normalized)
        if len(set(dates)) != len(dates):
            raise RawHistoryContractError("raw-history dates cannot contain duplicates")
        if dates != tuple(sorted(dates)):
            raise RawHistoryContractError("raw-history dates must be strictly increasing")
        if any(item < start or item > end for item in dates):
            raise RawHistoryContractError("raw-history contains out-of-range date")
        if source_id == CROSS_RAW_SOURCE_ID and any(not item.is_trading for item in normalized):
            raise RawHistoryContractError("cross source must omit suspended dates")
        payload = {
            "adjustment": adjustment,
            "amount_unit": amount_unit,
            "bars": [item.to_dict() for item in normalized],
            "price_basis": price_basis,
            "requested_end": end.isoformat(),
            "requested_start": start.isoformat(),
            "source_id": source_id,
            "symbol": symbol,
            "volume_unit": volume_unit,
        }
        return cls(
            source_id=source_id,
            symbol=symbol,
            requested_start=start,
            requested_end=end,
            fetched_at=_time(fetched_at, field="fetched_at"),
            price_basis=price_basis,
            adjustment=adjustment,
            volume_unit=volume_unit,
            amount_unit=amount_unit,
            bars=normalized,
            content_sha256=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )


@dataclass(frozen=True)
class RawHistoryAcceptance:
    manifest: Mapping[str, Any]

    def serialize(self) -> bytes:
        return canonical_json_bytes(self.manifest) + b"\n"


def evaluate_raw_history(
    *,
    calendar: VerifiedTradeCalendar,
    request_at: datetime | str,
    market_data_at: datetime | str,
    primary: RawHistoryObservation,
    cross: RawHistoryObservation,
) -> RawHistoryAcceptance:
    """Compare complete rows and emit metadata/hashes only, never raw bars."""

    request_time = _time(request_at, field="request_at")
    market_time = _time(market_data_at, field="market_data_at")
    if request_time > market_time:
        raise RawHistoryContractError("request_at cannot follow market_data_at")
    if (
        calendar.primary_source_data_as_of > market_time
        or calendar.cross_source_data_as_of > market_time
    ):
        raise RawHistoryContractError("calendar source data must exist before market_data_at")
    request_cutoff = calendar.previous_completed_trade_date(request_time)
    final_cutoff = calendar.previous_completed_trade_date(market_time)
    if request_cutoff != final_cutoff:
        raise RawHistoryContractError("completed-session cutoff changed during acquisition")
    if primary.source_id != PRIMARY_RAW_SOURCE_ID or cross.source_id != CROSS_RAW_SOURCE_ID:
        raise RawHistoryContractError("primary/cross source roles are fixed")
    if primary.symbol != cross.symbol or primary.requested_start != cross.requested_start:
        raise RawHistoryContractError("source symbol and requested range must match")
    if primary.requested_end != cross.requested_end or primary.requested_end > request_cutoff:
        raise RawHistoryContractError("providers must not be queried beyond frozen cutoff")
    if primary.fetched_at > market_time or cross.fetched_at > market_time:
        raise RawHistoryContractError("source must be fetched before market_data_at")

    expected_dates = tuple(
        item for item in calendar.trading_dates
        if primary.requested_start <= item <= primary.requested_end
    )
    primary_dates = tuple(item.trade_date for item in primary.bars)
    if primary_dates != expected_dates:
        missing = sorted(set(expected_dates) - set(primary_dates))
        extra = sorted(set(primary_dates) - set(expected_dates))
        raise RawHistoryContractError(
            "primary rows must exactly cover frozen trade calendar: "
            f"missing={[item.isoformat() for item in missing]}, "
            f"extra={[item.isoformat() for item in extra]}"
        )
    active_primary = tuple(item for item in primary.bars if item.is_trading)
    suspended_dates = tuple(item.trade_date for item in primary.bars if not item.is_trading)
    cross_dates = tuple(item.trade_date for item in cross.bars)
    active_dates = tuple(item.trade_date for item in active_primary)
    if cross_dates != active_dates:
        raise RawHistoryContractError("cross rows must exactly match active primary dates")

    cross_by_date = {item.trade_date: item for item in cross.bars}
    conflicts = {name: [] for name in ("open", "high", "low", "close", "volume", "amount_exact")}
    amount_over_tolerance = []
    max_amount_abs_diff = Decimal("0")
    for left in active_primary:
        right = cross_by_date[left.trade_date]
        for field in ("open", "high", "low", "close", "volume"):
            if getattr(left, field) != getattr(right, field):
                conflicts[field].append(left.trade_date.isoformat())
        amount_diff = abs(left.amount - right.amount)
        max_amount_abs_diff = max(max_amount_abs_diff, amount_diff)
        if amount_diff != 0:
            conflicts["amount_exact"].append(left.trade_date.isoformat())
        if amount_diff > AMOUNT_TOLERANCE_CNY:
            amount_over_tolerance.append(left.trade_date.isoformat())
    hard_conflicts = sum(len(conflicts[name]) for name in ("open", "high", "low", "close", "volume"))
    if hard_conflicts or amount_over_tolerance:
        raise RawHistoryContractError(
            "dual-source field conflict: "
            f"ohlcv={hard_conflicts}, amount_over_tolerance={amount_over_tolerance}"
        )

    manifest = {
        "acceptance_status": ACCEPTANCE_STATUS,
        "acquisition_mode": (
            "prospective_cutoff"
            if primary.requested_end == request_cutoff
            else "backfill_current_snapshot"
        ),
        "actual_range": {
            "end": primary.bars[-1].trade_date.isoformat(),
            "start": primary.bars[0].trade_date.isoformat(),
        },
        "amount_comparison": {
            "exact_conflict_count": len(conflicts["amount_exact"]),
            "exact_conflict_dates": conflicts["amount_exact"],
            "max_absolute_difference_cny": _canonical_decimal(max_amount_abs_diff),
            "tolerance_cny": _canonical_decimal(AMOUNT_TOLERANCE_CNY),
            "tolerance_status": "pass",
        },
        "calculation_version": RAW_HISTORY_CALCULATION_VERSION,
        "calendar_content_sha256": calendar.content_sha256,
        "cross_source": {
            "adjustment": cross.adjustment,
            "content_sha256": cross.content_sha256,
            "fetched_at": cross.fetched_at.isoformat(timespec="seconds"),
            "row_count": len(cross.bars),
            "source_data_as_of": cross.fetched_at.isoformat(timespec="seconds"),
            "source_id": cross.source_id,
        },
        "field_conflict_counts": {name: len(conflicts[name]) for name in ("open", "high", "low", "close", "volume")},
        "license_boundary": "software_license_does_not_grant_raw_data_redistribution",
        "market_data_at": market_time.isoformat(timespec="seconds"),
        "price_basis": PRICE_BASIS,
        "primary_source": {
            "adjustment": primary.adjustment,
            "content_sha256": primary.content_sha256,
            "fetched_at": primary.fetched_at.isoformat(timespec="seconds"),
            "row_count": len(primary.bars),
            "source_data_as_of": primary.fetched_at.isoformat(timespec="seconds"),
            "source_id": primary.source_id,
        },
        "public_payload_policy": "metadata_and_hashes_only_no_raw_rows",
        "requested_range": {
            "end": primary.requested_end.isoformat(),
            "start": primary.requested_start.isoformat(),
        },
        "revision_boundary": "provider_current_snapshot_no_historical_vintage",
        "schema_version": RAW_HISTORY_SCHEMA_VERSION,
        "sorting": "trade_date_ascending",
        "suspended_date_count": len(suspended_dates),
        "suspended_dates": [item.isoformat() for item in suspended_dates],
        "suspension_row_policy": "tradestatus_0_excluded_from_comparison_zero_volume_amount_ohlc_ignored",
        "symbol": primary.symbol,
        "trading_row_count": len(active_primary),
        "units": {"amount": "CNY", "price": "CNY_per_share", "volume": "share"},
    }
    payload_hash = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return RawHistoryAcceptance(manifest={**manifest, "manifest_sha256": payload_hash})
