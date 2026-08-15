"""Load the authoritative full-market quote snapshot for one screening run."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .realtime_types import RealtimeSource, UnifiedRealtimeQuote


MARKET_SNAPSHOT_ENV = "MARKET_SNAPSHOT_PATH"
MARKET_SNAPSHOT_SCHEMA_VERSION = "1.0"
PRICE_CHANGE_FORMULA = "(price - prev_close) / prev_close * 100"


class MarketSnapshotError(ValueError):
    """Raised when a configured run snapshot cannot provide a valid quote."""


def calculate_change_pct(price: Any, prev_close: Any) -> Optional[float]:
    """Return the canonical percentage change for a quote pair."""
    try:
        current = float(price)
        previous = float(prev_close)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or not math.isfinite(previous) or previous <= 0:
        return None
    return (current - previous) / previous * 100.0


def load_market_snapshot_quote(path: Path | str, stock_code: str) -> UnifiedRealtimeQuote:
    """Load and validate one quote from a run-scoped market snapshot."""
    snapshot_path = Path(path)
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketSnapshotError(f"market snapshot cannot be read: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MarketSnapshotError("market snapshot root must be an object")
    if payload.get("schema_version") != MARKET_SNAPSHOT_SCHEMA_VERSION:
        raise MarketSnapshotError("market snapshot schema_version is unsupported")
    if payload.get("price_change_formula") != PRICE_CHANGE_FORMULA:
        raise MarketSnapshotError("market snapshot price_change_formula is unsupported")

    market_data_at = str(payload.get("market_data_at") or "").strip()
    upstream_source = str(payload.get("data_source") or "").strip()
    quotes = payload.get("quotes")
    if not market_data_at or not upstream_source or not isinstance(quotes, Mapping):
        raise MarketSnapshotError("market snapshot metadata is incomplete")
    try:
        parsed_market_data_at = datetime.fromisoformat(
            market_data_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise MarketSnapshotError("market snapshot market_data_at is invalid") from exc
    if parsed_market_data_at.tzinfo is None or parsed_market_data_at.utcoffset() is None:
        raise MarketSnapshotError("market snapshot market_data_at must include timezone")

    code = str(stock_code or "").strip()
    raw_quote = quotes.get(code)
    if not isinstance(raw_quote, Mapping):
        raise MarketSnapshotError(f"market snapshot has no quote for {code}")

    price = _required_float(raw_quote, "price", positive=True)
    prev_close = _required_float(raw_quote, "prev_close", positive=True)
    canonical_change_pct = calculate_change_pct(price, prev_close)
    if canonical_change_pct is None:
        raise MarketSnapshotError(f"market snapshot quote is invalid for {code}")
    stored_change_pct = _required_float(raw_quote, "change_pct")
    if abs(stored_change_pct - canonical_change_pct) > 0.011:
        raise MarketSnapshotError(f"market snapshot change_pct is inconsistent for {code}")

    return UnifiedRealtimeQuote(
        code=code,
        name=str(raw_quote.get("name") or ""),
        source=RealtimeSource.MARKET_SNAPSHOT,
        fetched_at=market_data_at,
        provider_timestamp=market_data_at,
        market="cn",
        currency="CNY",
        data_quality="ok",
        price=price,
        change_pct=round(canonical_change_pct, 2),
        change_amount=round(price - prev_close, 4),
        volume=_optional_int(raw_quote.get("volume")),
        amount=_optional_float(raw_quote.get("amount")),
        volume_ratio=_optional_float(raw_quote.get("volume_ratio")),
        turnover_rate=_optional_float(raw_quote.get("turnover_rate")),
        amplitude=_optional_float(raw_quote.get("amplitude")),
        open_price=_optional_float(raw_quote.get("open")),
        high=_optional_float(raw_quote.get("high")),
        low=_optional_float(raw_quote.get("low")),
        pre_close=prev_close,
        pe_ratio=_optional_float(raw_quote.get("pe_ratio")),
        pb_ratio=_optional_float(raw_quote.get("pb_ratio")),
        upstream_source=upstream_source,
        price_change_formula=PRICE_CHANGE_FORMULA,
    )


def validate_market_snapshot(
    path: Path | str,
    stock_codes: Iterable[str],
) -> dict[str, UnifiedRealtimeQuote]:
    """Validate every selected code before starting same-run deep analysis."""
    quotes: dict[str, UnifiedRealtimeQuote] = {}
    for raw_code in stock_codes:
        code = str(raw_code or "").strip()
        if not code or code in quotes:
            raise MarketSnapshotError("market snapshot selected codes are invalid")
        quotes[code] = load_market_snapshot_quote(path, code)
    return quotes


def _required_float(values: Mapping[str, Any], field: str, *, positive: bool = False) -> float:
    value = _optional_float(values.get(field))
    if value is None or (positive and value <= 0):
        raise MarketSnapshotError(f"market snapshot field {field} is invalid")
    return value


def _optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> Optional[int]:
    number = _optional_float(value)
    return int(number) if number is not None else None
