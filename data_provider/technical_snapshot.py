"""Load the authoritative technical-indicator context for one screening run."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


TECHNICAL_SNAPSHOT_ENV = "TECHNICAL_SNAPSHOT_PATH"
TECHNICAL_SNAPSHOT_SCHEMA_VERSION = "1.0"


class TechnicalSnapshotError(ValueError):
    """Raised when screening-triggered analysis lacks its exact run context."""


@dataclass(frozen=True)
class TechnicalIndicatorContext:
    trade_date: str
    run_id: str
    run_number: str
    code: str
    technical_as_of: str
    history_data_through: str
    reference_price: float
    ma5: float
    ma10: float
    ma20: float
    five_day_pct: float
    watch_zone: str
    provider_volume_ratio: Optional[float]
    completed_day_volume_ratio_5d: Optional[float]
    history_source: str
    history_price_adjustment: str


def load_technical_snapshot_context(
    path: Path | str,
    stock_code: str,
    *,
    expected_trade_date: str,
    expected_run_id: str,
    expected_run_number: str,
) -> TechnicalIndicatorContext:
    """Load one indicator context and reject cross-date/run reuse."""
    snapshot_path = Path(path)
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TechnicalSnapshotError(f"technical snapshot cannot be read: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise TechnicalSnapshotError("technical snapshot root must be an object")
    if payload.get("schema_version") != TECHNICAL_SNAPSHOT_SCHEMA_VERSION:
        raise TechnicalSnapshotError("technical snapshot schema_version is unsupported")

    _match_identity(payload, "trade_date", expected_trade_date)
    _match_identity(payload, "run_id", expected_run_id)
    _match_identity(payload, "run_number", expected_run_number)

    technical_as_of = _aware_timestamp(payload.get("technical_as_of"), "technical_as_of")
    if technical_as_of.date().isoformat() != expected_trade_date:
        raise TechnicalSnapshotError("technical snapshot technical_as_of trade_date mismatch")

    code = str(stock_code or "").strip()
    indicators = payload.get("indicators")
    raw = indicators.get(code) if isinstance(indicators, Mapping) else None
    if not isinstance(raw, Mapping):
        raise TechnicalSnapshotError(f"technical snapshot has no indicator for {code}")
    if str(raw.get("code") or "").strip() != code:
        raise TechnicalSnapshotError(f"technical snapshot code mismatch for {code}")

    history_data_through = str(raw.get("history_data_through") or "").strip()
    try:
        history_date = date.fromisoformat(history_data_through)
        trade_date = date.fromisoformat(expected_trade_date)
    except ValueError as exc:
        raise TechnicalSnapshotError("technical snapshot history date is invalid") from exc
    if history_date >= trade_date:
        raise TechnicalSnapshotError("technical snapshot history_data_through must precede trade_date")

    return TechnicalIndicatorContext(
        trade_date=expected_trade_date,
        run_id=str(expected_run_id),
        run_number=str(expected_run_number),
        code=code,
        technical_as_of=technical_as_of.isoformat(),
        history_data_through=history_data_through,
        reference_price=_required_float(raw, "reference_price", positive=True),
        ma5=_required_float(raw, "ma5", positive=True),
        ma10=_required_float(raw, "ma10", positive=True),
        ma20=_required_float(raw, "ma20", positive=True),
        five_day_pct=_required_float(raw, "five_day_pct"),
        watch_zone=str(raw.get("watch_zone") or "无法确认"),
        provider_volume_ratio=_optional_float(raw.get("provider_volume_ratio")),
        completed_day_volume_ratio_5d=_optional_float(
            raw.get("completed_day_volume_ratio_5d")
        ),
        history_source=str(raw.get("history_source") or "unknown"),
        history_price_adjustment=str(raw.get("history_price_adjustment") or "unknown"),
    )


def validate_technical_snapshot(
    path: Path | str,
    stock_codes: Iterable[str],
    *,
    expected_run_id: str,
    expected_run_number: str,
) -> dict[str, TechnicalIndicatorContext]:
    """Validate all selected codes against the snapshot's own trade date."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TechnicalSnapshotError(f"technical snapshot cannot be read: {exc}") from exc
    trade_date = str(payload.get("trade_date") or "") if isinstance(payload, Mapping) else ""
    if not trade_date:
        raise TechnicalSnapshotError("technical snapshot trade_date is missing")
    result: dict[str, TechnicalIndicatorContext] = {}
    for raw_code in stock_codes:
        code = str(raw_code or "").strip()
        if not code or code in result:
            raise TechnicalSnapshotError("technical snapshot selected codes are invalid")
        result[code] = load_technical_snapshot_context(
            path,
            code,
            expected_trade_date=trade_date,
            expected_run_id=expected_run_id,
            expected_run_number=expected_run_number,
        )
    return result


def load_configured_technical_snapshot_context(
    stock_code: str,
    *,
    expected_trade_date: Optional[str] = None,
) -> Optional[TechnicalIndicatorContext]:
    """Return configured same-run context, or None for independent analysis."""
    path = os.getenv(TECHNICAL_SNAPSHOT_ENV)
    if not path:
        return None
    run_id = str(os.getenv("GITHUB_RUN_ID") or "").strip()
    run_number = str(os.getenv("GITHUB_RUN_NUMBER") or "").strip()
    if not run_id or not run_number:
        raise TechnicalSnapshotError(
            "screening-triggered technical snapshot requires GITHUB_RUN_ID and GITHUB_RUN_NUMBER"
        )
    if expected_trade_date is None:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TechnicalSnapshotError(f"technical snapshot cannot be read: {exc}") from exc
        expected_trade_date = (
            str(payload.get("trade_date") or "")
            if isinstance(payload, Mapping)
            else ""
        )
    return load_technical_snapshot_context(
        path,
        stock_code,
        expected_trade_date=expected_trade_date,
        expected_run_id=run_id,
        expected_run_number=run_number,
    )


def _match_identity(payload: Mapping[str, Any], field: str, expected: str) -> None:
    actual = str(payload.get(field) or "").strip()
    if not expected or actual != str(expected):
        raise TechnicalSnapshotError(f"technical snapshot {field} mismatch")


def _aware_timestamp(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise TechnicalSnapshotError(f"technical snapshot {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TechnicalSnapshotError(f"technical snapshot {field} must include timezone")
    return parsed


def _required_float(values: Mapping[str, Any], field: str, *, positive: bool = False) -> float:
    value = _optional_float(values.get(field))
    if value is None or (positive and value <= 0):
        raise TechnicalSnapshotError(f"technical snapshot field {field} is invalid")
    return value


def _optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
