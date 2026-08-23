"""Explicit, research-only acquisition for one prospective shared batch.

The command-facing path fetches the frozen dual-source calendar and raw daily
history.  V2.1 Universe identity and reviewed corporate-action snapshots enter
through one same-day Private request because their accepted contracts do not
define a safe provider fallback or a reviewed-clear empty-event representation.
Nothing is written until the complete bundle passes ``capture_prospective_batch``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence, Tuple

from research.benchmarks.raw_history import RawHistoryObservation
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.trade_calendar import VerifiedTradeCalendar
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
)
from research.data_sources.raw_history import fetch_raw_history_pair
from research.data_sources.trade_calendar import fetch_verified_trade_calendar
from research.prospective_batch import (
    INPUT_SCHEMA_VERSION,
    CaptureResult,
    ProspectiveBatchError,
    capture_prospective_batch,
)


ACQUISITION_REQUEST_SCHEMA_VERSION = "private-acquisition-request-v1"
PRIVATE_ARCHIVE_POLICY = "private_only_no_redistribution"
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


def _validated_request(
    value: Mapping[str, Any], *, observed_at: datetime
) -> tuple[date, datetime, Mapping[str, Any], Tuple[str, ...], Mapping[str, Any]]:
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
        or policy.get("provider_terms_reviewed_for_private_capture") is not True
    ):
        raise PrivateAcquisitionError(
            "private_license_boundary_unconfirmed",
            "Private-only storage and provider-terms review must be explicit",
        )

    universe = _mapping(value.get("universe"), field="universe")
    codes = _codes(universe.get("stock_codes"))
    expected_universe = {
        "config_hash": universe_config_hash(),
        "contract_version": UNIVERSE_CONTRACT_VERSION,
        "stock_codes": list(codes),
    }
    if (
        universe.get("contract_version") != UNIVERSE_CONTRACT_VERSION
        or universe.get("config_hash") != expected_universe["config_hash"]
        or not _SHA256_RE.fullmatch(str(universe.get("snapshot_sha256") or ""))
        or universe.get("snapshot_sha256") != _content_sha256(expected_universe)
    ):
        raise PrivateAcquisitionError(
            "universe_contract_failed", "V2.1 Universe identity or hash mismatch"
        )

    actions = _mapping(value.get("corporate_actions"), field="corporate_actions")
    if tuple(sorted(str(code) for code in actions)) != codes:
        raise PrivateAcquisitionError(
            "shared_universe_evidence_mismatch",
            "every Universe stock needs one reviewed corporate-action evidence pair",
        )
    return signal_date, request_at, expected_universe, codes, actions


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
    signal_date, request_at, universe, codes, actions = _validated_request(
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
