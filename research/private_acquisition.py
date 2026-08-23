"""Explicit, research-only acquisition for one prospective shared batch.

The command-facing path fetches the frozen dual-source calendar and raw daily
history.  One same-day Private production-spot snapshot is classified by the
existing V2.1 Universe contract, while explicit dual-source event or no-event
corporate-action evidence enters through the same request. Nothing is written
until the complete bundle passes ``capture_prospective_batch``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence, Tuple

import pandas as pd

from research.benchmarks.raw_history import RawHistoryObservation
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.trade_calendar import VerifiedTradeCalendar
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    UniverseStatus,
    evaluate_v21_universe,
    universe_config_hash,
    v21_hard_filter_codes,
)
from research.data_sources.raw_history import fetch_raw_history_pair
from research.data_sources.trade_calendar import fetch_verified_trade_calendar
from research.prospective_batch import (
    INPUT_SCHEMA_VERSION,
    PRIVATE_UNIVERSE_SOURCE_SCHEMA_VERSION,
    CaptureResult,
    ProspectiveBatchError,
    capture_prospective_batch,
)
from src.services.market_screener import ScreeningConfig, normalize_spot_frame


ACQUISITION_REQUEST_SCHEMA_VERSION = "private-acquisition-request-v2"
PRIVATE_ARCHIVE_POLICY = "private_only_no_redistribution"
PRIVATE_UNIVERSE_SOURCE_IDS = {
    "akshare_eastmoney",
    "efinance_eastmoney",
    "sina",
}
CALENDAR_LOOKBACK_DAYS = 370
REQUIRED_HISTORY_OBSERVATIONS = 61
_CODE_RE = re.compile(r"^[0-9]{6}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CalendarFetcher = Callable[..., VerifiedTradeCalendar]
RawHistoryFetcher = Callable[..., Tuple[RawHistoryObservation, RawHistoryObservation]]
Clock = Callable[[], datetime]


class PrivateAcquisitionError(ValueError):
    """Fail-closed acquisition error with a Public-safe reason code."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class AcquisitionResult:
    """Successful acquisition plus the immutable shared-batch result."""

    capture: CaptureResult
    source_count: int
    symbol_count: int


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrivateAcquisitionError("acquisition_input_invalid", f"{field} must be an object")
    return value


def _time(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip())
        except ValueError:
            raise PrivateAcquisitionError(
                "acquisition_time_invalid", f"{field} must be ISO-8601"
            ) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=8):
        raise PrivateAcquisitionError(
            "acquisition_time_invalid", f"{field} must use Asia/Shanghai semantics"
        )
    return parsed.astimezone(SHANGHAI_TZ)


def _signal_date(value: Any) -> date:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise PrivateAcquisitionError(
            "acquisition_input_invalid", "signal_date must be canonical YYYY-MM-DD"
        ) from None
    if parsed.isoformat() != text:
        raise PrivateAcquisitionError(
            "acquisition_input_invalid", "signal_date must be canonical YYYY-MM-DD"
        )
    return parsed


def _codes(value: Any) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PrivateAcquisitionError(
            "universe_contract_failed", "universe.stock_codes must be an array"
        )
    codes = tuple(str(item or "").strip() for item in value)
    if (
        not codes
        or any(not _CODE_RE.fullmatch(code) for code in codes)
        or codes != tuple(sorted(set(codes)))
    ):
        raise PrivateAcquisitionError(
            "universe_contract_failed", "stock codes must be non-empty, unique and sorted"
        )
    return codes


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validated_universe_source(
    value: Any,
    *,
    signal_date: date,
    request_at: datetime,
    observed_at: datetime,
) -> tuple[pd.DataFrame, Tuple[str, ...], Mapping[str, Any]]:
    source = _mapping(value, field="universe_source")
    if source.get("schema_version") != PRIVATE_UNIVERSE_SOURCE_SCHEMA_VERSION:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Private Universe source schema mismatch"
        )
    if source.get("model_version") != "V2.1":
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Private Universe must use V2.1"
        )
    source_id = str(source.get("source_id") or "").strip()
    if source_id not in PRIVATE_UNIVERSE_SOURCE_IDS:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Universe source is not a production spot provider"
        )
    source_as_of = _time(
        source.get("source_data_as_of"), field="universe_source.source_data_as_of"
    )
    fetched_at = _time(source.get("fetched_at"), field="universe_source.fetched_at")
    generated_at = _time(
        source.get("generated_at"), field="universe_source.generated_at"
    )
    if not (
        source_as_of <= fetched_at
        and request_at <= fetched_at <= generated_at <= observed_at
        and source_as_of.date()
        == fetched_at.date()
        == generated_at.date()
        == signal_date
    ):
        raise PrivateAcquisitionError(
            "universe_time_contract_failed",
            "Universe source must be a same-day prospective production snapshot",
        )

    config_value = _mapping(source.get("config"), field="universe_source.config")
    expected_config = asdict(ScreeningConfig())
    if dict(config_value) != expected_config:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "V2.1 production config must remain frozen"
        )
    config_sha256 = _content_sha256(expected_config)
    if source.get("config_sha256") != config_sha256:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "V2.1 production config hash mismatch"
        )

    rows_value = source.get("spot_rows")
    if isinstance(rows_value, (str, bytes)) or not isinstance(rows_value, Sequence):
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Private full-market spot_rows must be an array"
        )
    try:
        normalized = normalize_spot_frame(pd.DataFrame(list(rows_value)))
    except (TypeError, ValueError) as exc:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Private full-market spot snapshot is invalid"
        ) from exc
    if normalized["code"].duplicated().any():
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Universe snapshot contains duplicate codes"
        )
    canonical_rows = []
    for row in normalized.sort_values("code", kind="stable").to_dict("records"):
        code = str(row["code"])
        name = str(row["name"]).strip()
        numeric = {
            key: row[key]
            for key in ("close", "pct_change", "volume", "amount", "turnover")
        }
        if (
            not _CODE_RE.fullmatch(code)
            or not name
            or any(
                not math.isfinite(float(item))
                for item in numeric.values()
            )
        ):
            raise PrivateAcquisitionError(
                "universe_contract_failed", "Universe snapshot has invalid required fields"
            )
        canonical_rows.append({"code": code, "name": name, **numeric})
    if not canonical_rows or source.get("row_count") != len(canonical_rows):
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Universe snapshot row count is incomplete"
        )
    spot_content_sha256 = _content_sha256(canonical_rows)
    if source.get("spot_content_sha256") != spot_content_sha256:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "Universe spot snapshot hash mismatch"
        )
    frame = pd.DataFrame(canonical_rows)
    try:
        codes = v21_hard_filter_codes(frame, config=ScreeningConfig())
    except ValueError as exc:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "V2.1 Universe generation failed"
        ) from exc
    if not codes:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "V2.1 Universe cannot be empty"
        )
    manifest_source = {
        "config_sha256": config_sha256,
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "model_version": "V2.1",
        "row_count": len(canonical_rows),
        "schema_version": PRIVATE_UNIVERSE_SOURCE_SCHEMA_VERSION,
        "source_data_as_of": source_as_of.isoformat(timespec="seconds"),
        "source_id": source_id,
        "spot_content_sha256": spot_content_sha256,
        "universe_config_hash": universe_config_hash(),
    }
    source_manifest = {
        **manifest_source,
        "manifest_sha256": _content_sha256(manifest_source),
    }
    return frame, codes, source_manifest


def _validated_request(
    value: Mapping[str, Any], *, observed_at: datetime
) -> tuple[
    date,
    datetime,
    pd.DataFrame,
    Tuple[str, ...],
    Mapping[str, Any],
    Mapping[str, Any],
]:
    if value.get("schema_version") != ACQUISITION_REQUEST_SCHEMA_VERSION:
        raise PrivateAcquisitionError(
            "acquisition_input_invalid",
            f"schema_version must be {ACQUISITION_REQUEST_SCHEMA_VERSION}",
        )
    signal_date = _signal_date(value.get("signal_date"))
    request_at = _time(value.get("request_at"), field="request_at")
    if request_at.date() != signal_date or observed_at.date() != signal_date:
        raise PrivateAcquisitionError(
            "not_prospective_wall_clock",
            "request and execution must occur on the current signal date",
        )
    if request_at > observed_at:
        raise PrivateAcquisitionError(
            "acquisition_time_invalid", "request_at cannot follow observed_at"
        )

    policy = _mapping(value.get("private_archive_policy"), field="private_archive_policy")
    if (
        policy.get("raw_history") != PRIVATE_ARCHIVE_POLICY
        or policy.get("corporate_actions") != PRIVATE_ARCHIVE_POLICY
        or policy.get("universe") != PRIVATE_ARCHIVE_POLICY
        or policy.get("provider_terms_reviewed_for_private_capture") is not True
    ):
        raise PrivateAcquisitionError(
            "private_license_boundary_unconfirmed",
            "Private-only Universe/history/action storage and provider-terms review must be explicit",
        )

    frame, codes, source_manifest = _validated_universe_source(
        value.get("universe_source"),
        signal_date=signal_date,
        request_at=request_at,
        observed_at=observed_at,
    )

    actions = _mapping(value.get("corporate_actions"), field="corporate_actions")
    if tuple(sorted(str(code) for code in actions)) != codes:
        raise PrivateAcquisitionError(
            "shared_universe_evidence_mismatch",
            "every Universe stock needs one reviewed corporate-action evidence pair",
        )
    return signal_date, request_at, frame, codes, source_manifest, actions


def _raw_payload(observation: RawHistoryObservation) -> dict[str, Any]:
    return {
        "adjustment": observation.adjustment,
        "amount_unit": observation.amount_unit,
        "bars": [bar.to_dict() for bar in observation.bars],
        "content_sha256": observation.content_sha256,
        "fetched_at": observation.fetched_at.isoformat(timespec="seconds"),
        "price_basis": observation.price_basis,
        "requested_end": observation.requested_end.isoformat(),
        "requested_start": observation.requested_start.isoformat(),
        "source_data_as_of": observation.fetched_at.isoformat(timespec="seconds"),
        "source_id": observation.source_id,
        "symbol": observation.symbol,
        "volume_unit": observation.volume_unit,
    }


def acquire_private_shared_batch(
    request: Mapping[str, Any],
    *,
    private_root: Path | str,
    public_manifest_path: Path | str | None = None,
    allow_network: bool = False,
    observed_at: datetime | str | None = None,
    calendar_fetcher: CalendarFetcher = fetch_verified_trade_calendar,
    raw_history_fetcher: RawHistoryFetcher = fetch_raw_history_pair,
    clock: Clock | None = None,
) -> AcquisitionResult:
    """Acquire both raw sources once, then create one shared immutable batch."""

    now = clock or (lambda: datetime.now(SHANGHAI_TZ))
    observed = _time(
        observed_at if observed_at is not None else now(), field="observed_at"
    )
    source = _mapping(request, field="request")
    signal_date, request_at, spot_frame, codes, source_manifest, actions = _validated_request(
        source, observed_at=observed
    )
    if allow_network is not True:
        raise PrivateAcquisitionError(
            "network_opt_in_required", "pass allow_network=True explicitly"
        )

    query_start = signal_date - timedelta(days=CALENDAR_LOOKBACK_DAYS)
    try:
        calendar = calendar_fetcher(query_start, signal_date, allow_network=True)
    except Exception as exc:
        raise PrivateAcquisitionError(
            "calendar_acquisition_failed", type(exc).__name__
        ) from exc
    if signal_date not in calendar.trading_dates:
        raise PrivateAcquisitionError(
            "signal_date_not_trading_day", "signal date is not a verified trading day"
        )
    try:
        cutoff = calendar.previous_completed_trade_date(request_at)
    except Exception as exc:
        raise PrivateAcquisitionError(
            "calendar_acquisition_failed", type(exc).__name__
        ) from exc
    if cutoff >= signal_date:
        raise PrivateAcquisitionError(
            "t_minus_one_cutoff_required",
            "Private acquisition never requests the signal-date daily bar",
        )
    eligible_dates = tuple(item for item in calendar.trading_dates if item <= cutoff)
    if len(eligible_dates) < REQUIRED_HISTORY_OBSERVATIONS:
        raise PrivateAcquisitionError(
            "history_window_incomplete", "verified calendar has fewer than 61 completed sessions"
        )
    requested_dates = eligible_dates[-REQUIRED_HISTORY_OBSERVATIONS:]
    requested_start = requested_dates[0]

    symbols: dict[str, Any] = {}
    history_rows_by_code: dict[str, int] = {}
    for code in codes:
        try:
            primary, cross = raw_history_fetcher(
                code, requested_start, cutoff, allow_network=True
            )
        except Exception as exc:
            raise PrivateAcquisitionError(
                "raw_history_acquisition_failed", f"{code}:{type(exc).__name__}"
            ) from exc
        symbols[code] = {
            "corporate_actions": actions[code],
            "raw_history": {
                "cross": _raw_payload(cross),
                "primary": _raw_payload(primary),
            },
        }
        history_rows_by_code[code] = len(primary.bars)

    decisions = evaluate_v21_universe(
        spot_frame,
        history_rows_by_code=history_rows_by_code,
        config=ScreeningConfig(),
    )
    final_codes = tuple(
        item.stock_code
        for item in decisions
        if item.status is UniverseStatus.ELIGIBLE
    )
    if final_codes != codes:
        raise PrivateAcquisitionError(
            "universe_history_binding_failed",
            "V2.1 Universe changed after binding the acquired history window",
        )
    universe = {
        "config_hash": universe_config_hash(),
        "contract_version": UNIVERSE_CONTRACT_VERSION,
        "source_manifest": source_manifest,
        "stock_codes": list(codes),
    }

    market_data_at = _time(now(), field="market_data_at")
    captured_at = _time(now(), field="captured_at")
    final_observed_at = (
        observed
        if observed_at is not None
        else _time(now(), field="observed_at")
    )
    bundle = {
        "calendar": calendar.to_dict(),
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "market_data_at": market_data_at.isoformat(timespec="seconds"),
        "request_at": request_at.isoformat(timespec="seconds"),
        "schema_version": INPUT_SCHEMA_VERSION,
        "signal_date": signal_date.isoformat(),
        "symbols": symbols,
        "universe": universe,
    }
    try:
        capture = capture_prospective_batch(
            bundle,
            private_root=private_root,
            public_manifest_path=public_manifest_path,
            observed_at=final_observed_at,
        )
    except ProspectiveBatchError:
        raise
    return AcquisitionResult(
        capture=capture,
        source_count=2,
        symbol_count=len(codes),
    )
