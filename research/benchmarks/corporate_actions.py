"""Fail-closed Phase 2B corporate-action acceptance contract.

This module is research-only.  It keeps raw prices unchanged and validates a
separate action overlay before any later model may interpret mechanical price
jumps.  It deliberately contains no provider fallback and no model signal
logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Dict, Mapping, Sequence, Tuple

from research.benchmarks.raw_history import RawDailyBar
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.trade_calendar import VerifiedTradeCalendar


CORPORATE_ACTION_SCHEMA_VERSION = "phase2b-corporate-action-v1"
CORPORATE_ACTION_CALCULATION_VERSION = "raw-price-action-overlay-v1"
ACCEPTANCE_STATUS = "conditional_pass"
ACTION_TYPES = {
    "cash_dividend",
    "stock_dividend_or_transfer",
    "rights_issue",
    "suspension_resumption",
}
_SYMBOL_RE = re.compile(r"^[0-9]{6}$")


class CorporateActionContractError(ValueError):
    """Raised when action evidence cannot safely be used."""


def _date(value: Any, *, field: str, nullable: bool = False) -> date | None:
    if value is None or str(value).strip() == "":
        if nullable:
            return None
        raise CorporateActionContractError(f"{field} is required")
    if isinstance(value, datetime):
        raise CorporateActionContractError(f"{field} must be a date")
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise CorporateActionContractError(
            f"{field} must be canonical YYYY-MM-DD"
        ) from None
    if text != parsed.isoformat():
        raise CorporateActionContractError(
            f"{field} must be canonical YYYY-MM-DD"
        )
    return parsed


def _time(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise CorporateActionContractError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorporateActionContractError(f"{field} must include timezone")
    if parsed.utcoffset() != timedelta(hours=8):
        raise CorporateActionContractError(
            f"{field} must use Asia/Shanghai semantics"
        )
    timezone_key = getattr(parsed.tzinfo, "key", None)
    if timezone_key is not None and timezone_key != "Asia/Shanghai":
        raise CorporateActionContractError(
            f"{field} must use Asia/Shanghai semantics"
        )
    return parsed.astimezone(SHANGHAI_TZ)


def _decimal(
    value: Any,
    *,
    field: str,
    nullable: bool = True,
    positive: bool = False,
) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if nullable:
            return None
        raise CorporateActionContractError(f"{field} is required")
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation:
        raise CorporateActionContractError(f"{field} must be finite decimal") from None
    if not parsed.is_finite() or parsed < 0 or (positive and parsed <= 0):
        qualifier = "positive " if positive else "non-negative "
        raise CorporateActionContractError(f"{field} must be {qualifier}finite decimal")
    return parsed


def _canonical_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


@dataclass(frozen=True)
class CorporateActionEvent:
    symbol: str
    action_type: str
    known_at: datetime
    record_date: date | None
    ex_date: date | None
    payment_date: date | None
    listing_date: date | None
    suspension_start: date | None
    resumption_date: date | None
    cash_per_share: Decimal | None
    stock_ratio: Decimal | None
    rights_ratio: Decimal | None
    rights_price: Decimal | None

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        action_type: str,
        known_at: datetime | str,
        record_date: date | str | None = None,
        ex_date: date | str | None = None,
        payment_date: date | str | None = None,
        listing_date: date | str | None = None,
        suspension_start: date | str | None = None,
        resumption_date: date | str | None = None,
        cash_per_share: Any = None,
        stock_ratio: Any = None,
        rights_ratio: Any = None,
        rights_price: Any = None,
    ) -> "CorporateActionEvent":
        code = str(symbol or "").strip()
        if not _SYMBOL_RE.fullmatch(code):
            raise CorporateActionContractError("symbol must contain six digits")
        kind = str(action_type or "").strip()
        if kind not in ACTION_TYPES:
            raise CorporateActionContractError("unsupported corporate action type")
        values = {
            "cash_per_share": _decimal(cash_per_share, field="cash_per_share"),
            "stock_ratio": _decimal(stock_ratio, field="stock_ratio"),
            "rights_ratio": _decimal(rights_ratio, field="rights_ratio"),
            "rights_price": _decimal(rights_price, field="rights_price"),
        }
        dates = {
            name: _date(value, field=name, nullable=True)
            for name, value in {
                "record_date": record_date,
                "ex_date": ex_date,
                "payment_date": payment_date,
                "listing_date": listing_date,
                "suspension_start": suspension_start,
                "resumption_date": resumption_date,
            }.items()
        }

        if kind == "cash_dividend":
            if not values["cash_per_share"] or values["stock_ratio"] not in (None, Decimal("0")):
                raise CorporateActionContractError("cash dividend terms are incomplete")
            if values["rights_ratio"] not in (None, Decimal("0")) or values["rights_price"] not in (None, Decimal("0")):
                raise CorporateActionContractError("cash dividend cannot contain rights terms")
            if any(dates[name] is None for name in ("record_date", "ex_date", "payment_date")):
                raise CorporateActionContractError("cash dividend dates are incomplete")
        elif kind == "stock_dividend_or_transfer":
            if not values["stock_ratio"]:
                raise CorporateActionContractError("stock/transfer ratio is required")
            if values["rights_ratio"] not in (None, Decimal("0")) or values["rights_price"] not in (None, Decimal("0")):
                raise CorporateActionContractError("stock/transfer event cannot contain rights terms")
            if any(dates[name] is None for name in ("record_date", "ex_date", "listing_date")):
                raise CorporateActionContractError("stock/transfer dates are incomplete")
            if values["cash_per_share"] not in (None, Decimal("0")) and dates["payment_date"] is None:
                raise CorporateActionContractError("combined cash payment date is required")
        elif kind == "rights_issue":
            if not values["rights_ratio"] or not values["rights_price"]:
                raise CorporateActionContractError("rights terms are incomplete")
            if any(dates[name] is None for name in ("record_date", "ex_date", "listing_date")):
                raise CorporateActionContractError("rights dates are incomplete")
        else:
            if dates["suspension_start"] is None or dates["resumption_date"] is None:
                raise CorporateActionContractError("suspension/resumption dates are incomplete")
            if dates["suspension_start"] >= dates["resumption_date"]:
                raise CorporateActionContractError("resumption must follow suspension start")
            if any(value not in (None, Decimal("0")) for value in values.values()):
                raise CorporateActionContractError("suspension event cannot contain distribution terms")

        if dates["record_date"] and dates["ex_date"] and dates["record_date"] > dates["ex_date"]:
            raise CorporateActionContractError("record_date cannot follow ex_date")
        return cls(
            symbol=code,
            action_type=kind,
            known_at=_time(known_at, field="known_at"),
            **dates,
            **values,
        )

    @property
    def effective_date(self) -> date:
        return self.ex_date or self.resumption_date  # validated by action type

    def semantic_dict(self) -> Dict[str, Any]:
        """Comparable terms; provider capture latency is intentionally excluded."""

        return {
            "action_type": self.action_type,
            "cash_per_share": _canonical_decimal(self.cash_per_share),
            "ex_date": self.ex_date.isoformat() if self.ex_date else None,
            "listing_date": self.listing_date.isoformat() if self.listing_date else None,
            "payment_date": self.payment_date.isoformat() if self.payment_date else None,
            "record_date": self.record_date.isoformat() if self.record_date else None,
            "resumption_date": self.resumption_date.isoformat() if self.resumption_date else None,
            "rights_price": _canonical_decimal(self.rights_price),
            "rights_ratio": _canonical_decimal(self.rights_ratio),
            "stock_ratio": _canonical_decimal(self.stock_ratio),
            "suspension_start": self.suspension_start.isoformat() if self.suspension_start else None,
            "symbol": self.symbol,
        }

    def evidence_dict(self) -> Dict[str, Any]:
        return {**self.semantic_dict(), "known_at": self.known_at.isoformat(timespec="seconds")}

    @property
    def semantic_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.semantic_dict())).hexdigest()


@dataclass(frozen=True)
class CorporateActionObservation:
    source_id: str
    source_data_as_of: datetime
    fetched_at: datetime
    events: Tuple[CorporateActionEvent, ...]
    content_sha256: str

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        source_data_as_of: datetime | str,
        fetched_at: datetime | str,
        events: Sequence[CorporateActionEvent],
    ) -> "CorporateActionObservation":
        source = str(source_id or "").strip()
        if not source:
            raise CorporateActionContractError("source_id is required")
        as_of = _time(source_data_as_of, field="source_data_as_of")
        fetched = _time(fetched_at, field="fetched_at")
        if as_of > fetched:
            raise CorporateActionContractError("source_data_as_of cannot follow fetched_at")
        normalized = tuple(events)
        keys = tuple((item.effective_date, item.action_type, item.semantic_sha256) for item in normalized)
        if keys != tuple(sorted(keys)):
            raise CorporateActionContractError("events must be strictly sorted")
        identities = tuple((item.effective_date, item.action_type) for item in normalized)
        if len(set(identities)) != len(identities):
            raise CorporateActionContractError("events cannot contain duplicate identities")
        payload = {
            "events": [item.evidence_dict() for item in normalized],
            "source_data_as_of": as_of.isoformat(timespec="seconds"),
            "source_id": source,
        }
        return cls(
            source_id=source,
            source_data_as_of=as_of,
            fetched_at=fetched,
            events=normalized,
            content_sha256=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )


@dataclass(frozen=True)
class CorporateActionAcceptance:
    manifest: Mapping[str, Any]

    def serialize(self) -> bytes:
        return canonical_json_bytes(self.manifest) + b"\n"


def action_safe_bar_view(bars: Sequence[RawDailyBar]) -> Tuple[Mapping[str, Any], ...]:
    """Return an in-memory view that never exposes provider-carried suspension OHLC."""

    output = []
    for bar in bars:
        if bar.is_trading:
            output.append(bar.to_dict())
        else:
            output.append({
                "amount": None,
                "close": None,
                "high": None,
                "is_trading": False,
                "low": None,
                "open": None,
                "trade_date": bar.trade_date.isoformat(),
                "volume": None,
            })
    return tuple(output)


def reference_price(previous_close: Any, event: CorporateActionEvent) -> Decimal:
    """Exchange-style diagnostic reference; raw bars remain unchanged."""

    if event.action_type == "suspension_resumption":
        raise CorporateActionContractError("suspension alone has no action reference price")
    previous = _decimal(previous_close, field="previous_close", nullable=False, positive=True)
    cash = event.cash_per_share or Decimal("0")
    stock = event.stock_ratio or Decimal("0")
    rights = event.rights_ratio or Decimal("0")
    rights_price_value = event.rights_price or Decimal("0")
    return ((previous - cash) + rights_price_value * rights) / (
        Decimal("1") + stock + rights
    )


def distribution_economic_return(
    previous_close: Any,
    event_close: Any,
    event: CorporateActionEvent,
) -> Decimal:
    """Holder return for cash/stock distributions only; rights require review."""

    if event.action_type not in {"cash_dividend", "stock_dividend_or_transfer"}:
        raise CorporateActionContractError(
            "rights and suspension events require an explicit holder decision"
        )
    previous = _decimal(previous_close, field="previous_close", nullable=False, positive=True)
    current = _decimal(event_close, field="event_close", nullable=False, positive=True)
    cash = event.cash_per_share or Decimal("0")
    stock = event.stock_ratio or Decimal("0")
    return (current * (Decimal("1") + stock) + cash) / previous - Decimal("1")


def evaluate_corporate_actions(
    *,
    calendar: VerifiedTradeCalendar,
    market_data_at: datetime | str,
    primary: CorporateActionObservation,
    cross: CorporateActionObservation,
    raw_bars: Sequence[RawDailyBar],
) -> CorporateActionAcceptance:
    """Require exact semantic agreement and emit hashes/metadata only."""

    market_time = _time(market_data_at, field="market_data_at")
    if primary.source_id == cross.source_id:
        raise CorporateActionContractError("primary and cross sources must be independent")
    for observation in (primary, cross):
        if observation.source_data_as_of > market_time or observation.fetched_at > market_time:
            raise CorporateActionContractError("action evidence must exist before market_data_at")
        if any(event.known_at > market_time for event in observation.events):
            raise CorporateActionContractError("future-known corporate action is forbidden")
    if (
        calendar.primary_source_data_as_of > market_time
        or calendar.cross_source_data_as_of > market_time
    ):
        raise CorporateActionContractError("calendar evidence must exist before market_data_at")
    if not primary.events or not cross.events:
        raise CorporateActionContractError("both action sources must return evidence")

    left = tuple(item.semantic_sha256 for item in primary.events)
    right = tuple(item.semantic_sha256 for item in cross.events)
    if left != right:
        raise CorporateActionContractError("corporate-action source conflict or missing event")
    events = primary.events
    if len({item.symbol for item in events}) != 1:
        raise CorporateActionContractError("one acceptance manifest must cover one symbol")
    calendar_dates = set(calendar.trading_dates)
    for event in events:
        for label, value in (
            ("record_date", event.record_date),
            ("ex_date", event.ex_date),
            ("payment_date", event.payment_date),
            ("listing_date", event.listing_date),
            ("suspension_start", event.suspension_start),
            ("resumption_date", event.resumption_date),
        ):
            if value is not None and value not in calendar_dates:
                raise CorporateActionContractError(f"{label} must use frozen trade calendar")

    bars = tuple(raw_bars)
    dates = tuple(item.trade_date for item in bars)
    if dates != tuple(sorted(set(dates))):
        raise CorporateActionContractError("raw bars must be unique and sorted")
    inactive_dates = {item.trade_date for item in bars if not item.is_trading}
    for event in events:
        if event.action_type != "suspension_resumption":
            continue
        expected = {
            item for item in calendar.trading_dates
            if event.suspension_start <= item < event.resumption_date
        }
        if inactive_dates != expected:
            raise CorporateActionContractError("suspension dates conflict with raw history")
        if event.resumption_date not in dates:
            raise CorporateActionContractError("resumption bar is missing")
        resumed = next(item for item in bars if item.trade_date == event.resumption_date)
        if not resumed.is_trading:
            raise CorporateActionContractError("resumption bar must contain real trading")

    semantic_hash = hashlib.sha256(
        canonical_json_bytes([item.semantic_dict() for item in events])
    ).hexdigest()
    manifest = {
        "acceptance_status": ACCEPTANCE_STATUS,
        "action_count": len(events),
        "action_dates": [item.effective_date.isoformat() for item in events],
        "action_types": [item.action_type for item in events],
        "calculation_version": CORPORATE_ACTION_CALCULATION_VERSION,
        "calendar_content_sha256": calendar.content_sha256,
        "cross_source": {
            "content_sha256": cross.content_sha256,
            "fetched_at": cross.fetched_at.isoformat(timespec="seconds"),
            "source_data_as_of": cross.source_data_as_of.isoformat(timespec="seconds"),
            "source_id": cross.source_id,
        },
        "event_semantic_sha256": semantic_hash,
        "license_boundary": "software_or_interface_access_does_not_grant_raw_event_redistribution",
        "market_data_at": market_time.isoformat(timespec="seconds"),
        "no_lookahead_policy": "known_at_and_source_data_as_of_must_not_follow_market_data_at",
        "price_layering": "raw_unadjusted_immutable_action_adjustment_derived_only",
        "primary_source": {
            "content_sha256": primary.content_sha256,
            "fetched_at": primary.fetched_at.isoformat(timespec="seconds"),
            "source_data_as_of": primary.source_data_as_of.isoformat(timespec="seconds"),
            "source_id": primary.source_id,
        },
        "public_payload_policy": "metadata_and_hashes_only_no_raw_event_or_market_rows",
        "revision_boundary": "current_snapshot_no_historical_vintage_prospective_capture_required",
        "schema_version": CORPORATE_ACTION_SCHEMA_VERSION,
        "suspended_date_count": len(inactive_dates),
        "suspended_dates": [item.isoformat() for item in sorted(inactive_dates)],
        "suspension_price_policy": "inactive_provider_ohlc_discarded_no_forward_fill",
        "symbol": events[0].symbol,
    }
    manifest_hash = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    return CorporateActionAcceptance(
        manifest={**manifest, "manifest_sha256": manifest_hash}
    )
