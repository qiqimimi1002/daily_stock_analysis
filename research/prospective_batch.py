"""Prospective, private and immutable shared research-batch capture.

The collector has no network client.  It validates one caller-supplied private
bundle with the existing calendar, raw-history and corporate-action contracts,
then archives the normalized evidence atomically.  Public output contains only
hashes, counts, status and fixed model bindings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Dict, Mapping, Sequence, Tuple

from research.benchmarks.corporate_actions import (
    ACCEPTANCE_STATUS as CORPORATE_ACTION_ACCEPTANCE_STATUS,
    CorporateActionContractError,
    CorporateActionEvent,
    CorporateActionObservation,
    evaluate_corporate_actions,
)
from research.benchmarks.low_volatility import MODEL_NAME as LOW_VOLATILITY_MODEL
from research.benchmarks.raw_history import (
    ACCEPTANCE_STATUS as RAW_HISTORY_ACCEPTANCE_STATUS,
    CROSS_RAW_SOURCE_ID,
    PRICE_BASIS,
    PRIMARY_RAW_SOURCE_ID,
    RawDailyBar,
    RawHistoryContractError,
    RawHistoryObservation,
    evaluate_raw_history,
)
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.short_term import MODEL_NAME as SHORT_TERM_MODEL
from research.benchmarks.trade_calendar import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    CalendarSourceObservation,
    HistoryWindowContract,
    TradeCalendarContractError,
    VerifiedTradeCalendar,
)
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
)


INPUT_SCHEMA_VERSION = "prospective-shared-batch-input-v1"
PRIVATE_SCHEMA_VERSION = "prospective-shared-batch-private-v1"
PRIVATE_MANIFEST_SCHEMA_VERSION = "prospective-shared-batch-private-manifest-v1"
PUBLIC_MANIFEST_SCHEMA_VERSION = "prospective-shared-batch-public-manifest-v1"
CALCULATION_VERSION = "shared-evidence-capture-v1"
REQUIRED_HISTORY_OBSERVATIONS = 61
PUBLIC_PAYLOAD_POLICY = "metadata_hashes_counts_status_reason_codes_only"
MODEL_CONSUMERS = (SHORT_TERM_MODEL, LOW_VOLATILITY_MODEL)
PRIVATE_UNIVERSE_SOURCE_SCHEMA_VERSION = "private-v21-universe-source-v1"
_CODE_RE = re.compile(r"^[0-9]{6}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ProspectiveBatchError(ValueError):
    """Fail-closed error with a stable, public-safe reason code."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class ProspectiveBatchConflictError(ProspectiveBatchError):
    """Existing immutable state differs from the fully validated input."""


@dataclass(frozen=True)
class CaptureResult:
    status: str
    archive_dir: Path
    batch_id: str
    private_content_sha256: str
    private_manifest_sha256: str
    public_manifest: Mapping[str, Any]


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProspectiveBatchError("input_invalid", f"{field} must be an object")
    return value


def _sequence(value: Any, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProspectiveBatchError("input_invalid", f"{field} must be an array")
    return value


def _text(value: Any, *, field: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        raise ProspectiveBatchError("input_invalid", f"{field} is required")
    return text_value


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        raise ProspectiveBatchError("input_invalid", f"{field} must be a date")
    text_value = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text_value)
    except ValueError:
        raise ProspectiveBatchError(
            "input_invalid", f"{field} must be canonical YYYY-MM-DD"
        ) from None
    if parsed.isoformat() != text_value:
        raise ProspectiveBatchError(
            "input_invalid", f"{field} must be canonical YYYY-MM-DD"
        )
    return parsed


def _time(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").strip())
        except ValueError:
            raise ProspectiveBatchError(
                "time_contract_failed", f"{field} must be ISO-8601"
            ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProspectiveBatchError(
            "time_contract_failed", f"{field} must include timezone"
        )
    if parsed.utcoffset() != timedelta(hours=8):
        raise ProspectiveBatchError(
            "time_contract_failed", f"{field} must use Asia/Shanghai semantics"
        )
    timezone_key = getattr(parsed.tzinfo, "key", None)
    if timezone_key is not None and timezone_key != "Asia/Shanghai":
        raise ProspectiveBatchError(
            "time_contract_failed", f"{field} must use Asia/Shanghai semantics"
        )
    return parsed.astimezone(SHANGHAI_TZ)


def _sha256(value: Any, *, field: str) -> str:
    digest = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ProspectiveBatchError("input_invalid", f"{field} must be SHA-256")
    return digest


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _strict_json_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _stock_codes(value: Any) -> Tuple[str, ...]:
    values = _sequence(value, field="universe.stock_codes")
    codes = tuple(str(item or "").strip() for item in values)
    if any(not _CODE_RE.fullmatch(code) for code in codes):
        raise ProspectiveBatchError(
            "universe_contract_failed", "stock codes must contain six digits"
        )
    if codes != tuple(sorted(set(codes))) or not codes:
        raise ProspectiveBatchError(
            "universe_contract_failed",
            "stock codes must be non-empty, unique and sorted",
        )
    return codes


def _calendar(
    value: Any,
    *,
    request_at: datetime,
    market_data_at: datetime,
) -> VerifiedTradeCalendar:
    source = _mapping(value, field="calendar")
    trading_dates = _sequence(source.get("trading_dates"), field="calendar.trading_dates")

    def observation(name: str, expected_source_id: str) -> CalendarSourceObservation:
        item = _mapping(source.get(name), field=f"calendar.{name}")
        if item.get("source_id") != expected_source_id:
            raise ProspectiveBatchError(
                "calendar_contract_failed", f"calendar.{name} source is fixed"
            )
        return CalendarSourceObservation(
            source_id=expected_source_id,
            query_start=source.get("query_start"),
            query_end=source.get("query_end"),
            trading_dates=trading_dates,
            source_data_as_of=item.get("source_data_as_of"),
            fetched_at=item.get("fetched_at"),
        )

    try:
        result = VerifiedTradeCalendar.create(
            query_start=source.get("query_start"),
            query_end=source.get("query_end"),
            primary=observation("primary_source", PRIMARY_SOURCE_ID),
            cross=observation("cross_source", CROSS_SOURCE_ID),
        )
    except (TradeCalendarContractError, ProspectiveBatchError) as exc:
        if isinstance(exc, ProspectiveBatchError):
            raise
        raise ProspectiveBatchError("calendar_contract_failed", str(exc)) from exc
    expected_hash = _sha256(source.get("content_sha256"), field="calendar.content_sha256")
    if result.content_sha256 != expected_hash:
        raise ProspectiveBatchError(
            "calendar_contract_failed", "calendar content hash mismatch"
        )
    if request_at > min(result.primary_fetched_at, result.cross_fetched_at):
        raise ProspectiveBatchError(
            "time_contract_failed", "request_at cannot follow a calendar fetch"
        )
    if result.primary_fetched_at > market_data_at or result.cross_fetched_at > market_data_at:
        raise ProspectiveBatchError(
            "calendar_contract_failed", "calendar must be fetched before market_data_at"
        )
    return result


def _raw_bar(value: Any) -> RawDailyBar:
    item = _mapping(value, field="raw_history.bar")
    try:
        return RawDailyBar.create(
            trade_date=item.get("trade_date"),
            open=item.get("open"),
            high=item.get("high"),
            low=item.get("low"),
            close=item.get("close"),
            volume=item.get("volume"),
            amount=item.get("amount"),
            is_trading=item.get("is_trading"),
        )
    except RawHistoryContractError as exc:
        raise ProspectiveBatchError("raw_history_contract_failed", str(exc)) from exc


def _raw_observation(
    value: Any,
    *,
    symbol: str,
    source_id: str,
) -> RawHistoryObservation:
    item = _mapping(value, field=f"raw_history.{source_id}")
    bars = tuple(_raw_bar(row) for row in _sequence(item.get("bars"), field="raw_history.bars"))
    try:
        return RawHistoryObservation.create(
            source_id=source_id,
            symbol=symbol,
            requested_start=item.get("requested_start"),
            requested_end=item.get("requested_end"),
            fetched_at=item.get("fetched_at"),
            price_basis=item.get("price_basis"),
            adjustment=item.get("adjustment"),
            volume_unit=item.get("volume_unit"),
            amount_unit=item.get("amount_unit"),
            bars=bars,
        )
    except RawHistoryContractError as exc:
        raise ProspectiveBatchError("raw_history_contract_failed", str(exc)) from exc


def _event(value: Any, *, symbol: str) -> CorporateActionEvent:
    item = _mapping(value, field="corporate_action.event")
    if item.get("symbol") != symbol:
        raise ProspectiveBatchError(
            "corporate_action_contract_failed", "corporate-action symbol mismatch"
        )
    try:
        return CorporateActionEvent.create(
            symbol=symbol,
            action_type=item.get("action_type"),
            known_at=item.get("known_at"),
            record_date=item.get("record_date"),
            ex_date=item.get("ex_date"),
            payment_date=item.get("payment_date"),
            listing_date=item.get("listing_date"),
            suspension_start=item.get("suspension_start"),
            resumption_date=item.get("resumption_date"),
            cash_per_share=item.get("cash_per_share"),
            stock_ratio=item.get("stock_ratio"),
            rights_ratio=item.get("rights_ratio"),
            rights_price=item.get("rights_price"),
        )
    except CorporateActionContractError as exc:
        raise ProspectiveBatchError(
            "corporate_action_contract_failed", str(exc)
        ) from exc


def _corporate_action_observation(
    value: Any,
    *,
    symbol: str,
) -> CorporateActionObservation:
    item = _mapping(value, field="corporate_action.observation")
    events = tuple(
        _event(row, symbol=symbol)
        for row in _sequence(item.get("events"), field="corporate_action.events")
    )
    try:
        return CorporateActionObservation.create(
            source_id=item.get("source_id"),
            source_data_as_of=item.get("source_data_as_of"),
            fetched_at=item.get("fetched_at"),
            symbol=item.get("symbol") or symbol,
            query_start=item.get("query_start"),
            query_end=item.get("query_end"),
            query_status=item.get("query_status", "success"),
            query_result=item.get("query_result"),
            events=events,
        )
    except CorporateActionContractError as exc:
        raise ProspectiveBatchError(
            "corporate_action_contract_failed", str(exc)
        ) from exc


def _raw_observation_payload(value: RawHistoryObservation) -> Dict[str, Any]:
    return {
        "adjustment": value.adjustment,
        "amount_unit": value.amount_unit,
        "bars": [item.to_dict() for item in value.bars],
        "content_sha256": value.content_sha256,
        "fetched_at": value.fetched_at.isoformat(timespec="seconds"),
        "price_basis": value.price_basis,
        "requested_end": value.requested_end.isoformat(),
        "requested_start": value.requested_start.isoformat(),
        "source_data_as_of": value.fetched_at.isoformat(timespec="seconds"),
        "source_id": value.source_id,
        "symbol": value.symbol,
        "volume_unit": value.volume_unit,
    }


def _action_observation_payload(value: CorporateActionObservation) -> Dict[str, Any]:
    return {
        "content_sha256": value.content_sha256,
        "events": [item.evidence_dict() for item in value.events],
        "fetched_at": value.fetched_at.isoformat(timespec="seconds"),
        "query_end": value.query_end.isoformat() if value.query_end else None,
        "query_result": value.query_result,
        "query_start": value.query_start.isoformat() if value.query_start else None,
        "query_status": value.query_status,
        "source_data_as_of": value.source_data_as_of.isoformat(timespec="seconds"),
        "source_id": value.source_id,
        "symbol": value.symbol,
    }


def _validate_symbol(
    value: Any,
    *,
    symbol: str,
    calendar: VerifiedTradeCalendar,
    request_at: datetime,
    market_data_at: datetime,
    captured_at: datetime,
    cutoff: date,
) -> Dict[str, Any]:
    item = _mapping(value, field=f"symbols.{symbol}")
    raw = _mapping(item.get("raw_history"), field=f"symbols.{symbol}.raw_history")
    primary = _raw_observation(
        raw.get("primary"), symbol=symbol, source_id=PRIMARY_RAW_SOURCE_ID
    )
    cross = _raw_observation(
        raw.get("cross"), symbol=symbol, source_id=CROSS_RAW_SOURCE_ID
    )
    if request_at > min(primary.fetched_at, cross.fetched_at):
        raise ProspectiveBatchError(
            "time_contract_failed", "request_at cannot follow a provider fetch"
        )
    try:
        raw_acceptance = evaluate_raw_history(
            calendar=calendar,
            request_at=request_at,
            market_data_at=market_data_at,
            primary=primary,
            cross=cross,
        )
    except RawHistoryContractError as exc:
        raise ProspectiveBatchError("raw_history_contract_failed", str(exc)) from exc
    if (
        raw_acceptance.manifest.get("acceptance_status")
        != RAW_HISTORY_ACCEPTANCE_STATUS
        or raw_acceptance.manifest.get("acquisition_mode") != "prospective_cutoff"
        or raw_acceptance.manifest.get("price_basis") != PRICE_BASIS
        or primary.requested_end != cutoff
    ):
        raise ProspectiveBatchError(
            "raw_history_contract_failed", "raw history is not prospective raw_unadjusted"
        )
    history_as_of = datetime.combine(cutoff, time(15, 0), tzinfo=SHANGHAI_TZ)
    latest_history_fetch = max(primary.fetched_at, cross.fetched_at)
    try:
        window = HistoryWindowContract.create(
            calendar=calendar,
            market_data_at=market_data_at,
            history_data_as_of=history_as_of,
            source_data_as_of=latest_history_fetch,
            fetched_at=latest_history_fetch,
            generated_at=captured_at,
            required_observations=REQUIRED_HISTORY_OBSERVATIONS,
            observed_trade_dates=[bar.trade_date for bar in primary.bars],
        )
    except TradeCalendarContractError as exc:
        raise ProspectiveBatchError("history_window_contract_failed", str(exc)) from exc

    actions = _mapping(
        item.get("corporate_actions"), field=f"symbols.{symbol}.corporate_actions"
    )
    action_primary = _corporate_action_observation(
        actions.get("primary"), symbol=symbol
    )
    action_cross = _corporate_action_observation(actions.get("cross"), symbol=symbol)
    if request_at > min(action_primary.fetched_at, action_cross.fetched_at):
        raise ProspectiveBatchError(
            "time_contract_failed", "request_at cannot follow an action fetch"
        )
    try:
        action_acceptance = evaluate_corporate_actions(
            calendar=calendar,
            market_data_at=market_data_at,
            primary=action_primary,
            cross=action_cross,
            raw_bars=primary.bars,
        )
    except CorporateActionContractError as exc:
        raise ProspectiveBatchError(
            "corporate_action_contract_failed", str(exc)
        ) from exc
    if (
        action_acceptance.manifest.get("acceptance_status")
        != CORPORATE_ACTION_ACCEPTANCE_STATUS
        or action_acceptance.manifest.get("calendar_content_sha256")
        != calendar.content_sha256
        or action_acceptance.manifest.get("market_data_at")
        != market_data_at.isoformat(timespec="seconds")
    ):
        raise ProspectiveBatchError(
            "corporate_action_contract_failed",
            "corporate-action acceptance is not bound to this batch",
        )

    return {
        "corporate_action_acceptance": dict(action_acceptance.manifest),
        "corporate_actions": {
            "cross": _action_observation_payload(action_cross),
            "primary": _action_observation_payload(action_primary),
        },
        "history_window": window.to_dict(),
        "raw_history": {
            "acceptance": dict(raw_acceptance.manifest),
            "cross": _raw_observation_payload(cross),
            "primary": _raw_observation_payload(primary),
        },
    }


def _validated_content(
    bundle: Mapping[str, Any],
    *,
    observed_at: datetime,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if bundle.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise ProspectiveBatchError(
            "input_invalid", f"schema_version must be {INPUT_SCHEMA_VERSION}"
        )
    signal_date = _date(bundle.get("signal_date"), field="signal_date")
    request_at = _time(bundle.get("request_at"), field="request_at")
    market_data_at = _time(bundle.get("market_data_at"), field="market_data_at")
    captured_at = _time(bundle.get("captured_at"), field="captured_at")
    if not (
        request_at.date()
        == market_data_at.date()
        == captured_at.date()
        == signal_date
    ):
        raise ProspectiveBatchError(
            "not_prospective_same_day",
            "request, market, capture and signal dates must be the same current date",
        )
    if not request_at <= market_data_at <= captured_at:
        raise ProspectiveBatchError(
            "time_contract_failed",
            "request_at <= market_data_at <= captured_at is required",
        )
    if observed_at.date() != signal_date or captured_at > observed_at:
        raise ProspectiveBatchError(
            "not_prospective_wall_clock",
            "capture must be completed on the collector's current signal date",
        )

    universe = _mapping(bundle.get("universe"), field="universe")
    if universe.get("contract_version") != UNIVERSE_CONTRACT_VERSION:
        raise ProspectiveBatchError(
            "universe_contract_failed", "V2.1 Universe contract version is required"
        )
    expected_config_hash = universe_config_hash()
    if _sha256(universe.get("config_hash"), field="universe.config_hash") != expected_config_hash:
        raise ProspectiveBatchError(
            "universe_contract_failed", "V2.1 Universe config hash mismatch"
        )
    codes = _stock_codes(universe.get("stock_codes"))
    source_manifest_value = universe.get("source_manifest")
    source_manifest = None
    if source_manifest_value is not None:
        source_manifest_item = _mapping(
            source_manifest_value, field="universe.source_manifest"
        )
        source_manifest_source = {
            key: source_manifest_item.get(key)
            for key in (
                "config_sha256",
                "fetched_at",
                "generated_at",
                "model_version",
                "row_count",
                "schema_version",
                "source_data_as_of",
                "source_id",
                "spot_content_sha256",
                "universe_config_hash",
            )
        }
        if (
            source_manifest_source["schema_version"]
            != PRIVATE_UNIVERSE_SOURCE_SCHEMA_VERSION
            or source_manifest_source["model_version"] != "V2.1"
            or source_manifest_source["universe_config_hash"]
            != expected_config_hash
            or not isinstance(source_manifest_source["row_count"], int)
            or isinstance(source_manifest_source["row_count"], bool)
            or source_manifest_source["row_count"] < len(codes)
        ):
            raise ProspectiveBatchError(
                "universe_contract_failed", "Private Universe source manifest is invalid"
            )
        for key in ("config_sha256", "spot_content_sha256"):
            _sha256(
                source_manifest_source[key],
                field=f"universe.source_manifest.{key}",
            )
        source_as_of = _time(
            source_manifest_source["source_data_as_of"],
            field="universe.source_manifest.source_data_as_of",
        )
        source_fetched = _time(
            source_manifest_source["fetched_at"],
            field="universe.source_manifest.fetched_at",
        )
        source_generated = _time(
            source_manifest_source["generated_at"],
            field="universe.source_manifest.generated_at",
        )
        if not (
            source_as_of <= source_fetched
            and request_at <= source_fetched <= source_generated <= market_data_at
            and source_as_of.date()
            == source_fetched.date()
            == source_generated.date()
            == signal_date
        ):
            raise ProspectiveBatchError(
                "time_contract_failed", "Private Universe source times are invalid"
            )
        expected_manifest_hash = _content_sha256(source_manifest_source)
        if (
            _sha256(
                source_manifest_item.get("manifest_sha256"),
                field="universe.source_manifest.manifest_sha256",
            )
            != expected_manifest_hash
        ):
            raise ProspectiveBatchError(
                "universe_contract_failed", "Private Universe source manifest hash mismatch"
            )
        source_manifest = {
            **source_manifest_source,
            "manifest_sha256": expected_manifest_hash,
        }
    universe_snapshot = {
        "config_hash": expected_config_hash,
        "contract_version": UNIVERSE_CONTRACT_VERSION,
        "stock_codes": list(codes),
    }
    if source_manifest is not None:
        universe_snapshot["source_manifest"] = source_manifest
    universe_snapshot_hash = _content_sha256(universe_snapshot)

    calendar = _calendar(
        bundle.get("calendar"),
        request_at=request_at,
        market_data_at=market_data_at,
    )
    try:
        cutoff = calendar.previous_completed_trade_date(market_data_at)
    except TradeCalendarContractError as exc:
        raise ProspectiveBatchError("calendar_contract_failed", str(exc)) from exc
    if cutoff >= signal_date:
        raise ProspectiveBatchError(
            "t_minus_one_cutoff_required", "shared daily evidence must end at T-1"
        )
    symbols = _mapping(bundle.get("symbols"), field="symbols")
    if tuple(sorted(str(code) for code in symbols)) != codes:
        raise ProspectiveBatchError(
            "shared_universe_evidence_mismatch",
            "every V2.1 Universe stock must have exactly one shared evidence record",
        )
    normalized_symbols = {
        code: _validate_symbol(
            symbols[code],
            symbol=code,
            calendar=calendar,
            request_at=request_at,
            market_data_at=market_data_at,
            captured_at=captured_at,
            cutoff=cutoff,
        )
        for code in codes
    }

    evidence = {
        "calendar": calendar.to_dict(),
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "market_data_at": market_data_at.isoformat(timespec="seconds"),
        "previous_completed_trade_date": cutoff.isoformat(),
        "request_at": request_at.isoformat(timespec="seconds"),
        "signal_date": signal_date.isoformat(),
        "symbols": normalized_symbols,
        "universe": {
            **universe_snapshot,
            "snapshot_sha256": universe_snapshot_hash,
        },
    }
    shared_evidence_hash = _content_sha256(evidence)
    batch_identity = {
        "calculation_version": CALCULATION_VERSION,
        "calendar_content_sha256": calendar.content_sha256,
        "signal_date": signal_date.isoformat(),
        "universe_snapshot_sha256": universe_snapshot_hash,
    }
    batch_id = _content_sha256(batch_identity)
    model_bindings = {
        model: {
            "batch_id": batch_id,
            "shared_evidence_sha256": shared_evidence_hash,
        }
        for model in MODEL_CONSUMERS
    }
    private_payload = {
        "batch_id": batch_id,
        "calculation_version": CALCULATION_VERSION,
        "evidence": evidence,
        "model_bindings": model_bindings,
        "private_archive": True,
        "schema_version": PRIVATE_SCHEMA_VERSION,
    }
    raw_manifest_hashes = [
        normalized_symbols[code]["raw_history"]["acceptance"]["manifest_sha256"]
        for code in codes
    ]
    action_manifest_hashes = [
        normalized_symbols[code]["corporate_action_acceptance"]["manifest_sha256"]
        for code in codes
    ]
    public_summary = {
        "batch_id": batch_id,
        "calculation_version": CALCULATION_VERSION,
        "calendar_content_sha256": calendar.content_sha256,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "corporate_action_acceptance_count": len(action_manifest_hashes),
        "corporate_action_manifest_set_sha256": _content_sha256(action_manifest_hashes),
        "immutable_archive": True,
        "market_data_at": market_data_at.isoformat(timespec="seconds"),
        "model_bindings": model_bindings,
        "previous_completed_trade_date": cutoff.isoformat(),
        "price_basis": PRICE_BASIS,
        "private_archive": True,
        "public_payload_policy": PUBLIC_PAYLOAD_POLICY,
        "raw_history_acceptance_count": len(raw_manifest_hashes),
        "raw_history_manifest_set_sha256": _content_sha256(raw_manifest_hashes),
        "reason_codes": [],
        "schema_version": PUBLIC_MANIFEST_SCHEMA_VERSION,
        "signal_date": signal_date.isoformat(),
        "status": "accepted",
        "symbol_count": len(codes),
        "universe_config_hash": expected_config_hash,
        "universe_contract_version": UNIVERSE_CONTRACT_VERSION,
        "universe_snapshot_sha256": universe_snapshot_hash,
    }
    if source_manifest is not None:
        public_summary["universe_source_manifest_sha256"] = source_manifest[
            "manifest_sha256"
        ]
    return private_payload, public_summary


def _archive_dir(root: Path, signal_date: str) -> Path:
    year, month, day = signal_date.split("-")
    return root / year / month / day / "shared-batch-v1"


def _private_manifest(
    *,
    batch_id: str,
    private_bytes: bytes,
) -> Dict[str, Any]:
    source = {
        "batch_id": batch_id,
        "files": {
            "private-batch.json": hashlib.sha256(private_bytes).hexdigest(),
        },
        "schema_version": PRIVATE_MANIFEST_SCHEMA_VERSION,
    }
    return {**source, "manifest_sha256": _content_sha256(source)}


def _public_manifest(
    summary: Mapping[str, Any],
    *,
    private_content_sha256: str,
    private_manifest_sha256: str,
) -> Dict[str, Any]:
    source = {
        **summary,
        "private_content_sha256": private_content_sha256,
        "private_manifest_sha256": private_manifest_sha256,
    }
    return {**source, "manifest_sha256": _content_sha256(source)}


def _verify_existing(
    target: Path,
    *,
    expected_files: Mapping[str, bytes],
) -> None:
    if not target.is_dir():
        raise ProspectiveBatchConflictError(
            "immutable_archive_conflict", "immutable archive path is not a directory"
        )
    actual_names = {item.name for item in target.iterdir()}
    if actual_names != set(expected_files):
        raise ProspectiveBatchConflictError(
            "immutable_archive_conflict", "immutable archive file set differs"
        )
    for name, expected in expected_files.items():
        try:
            actual = (target / name).read_bytes()
        except OSError as exc:
            raise ProspectiveBatchConflictError(
                "immutable_archive_conflict", "immutable archive cannot be verified"
            ) from exc
        if actual != expected:
            raise ProspectiveBatchConflictError(
                "immutable_archive_conflict", "same-day immutable content differs"
            )


def _write_public(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise ProspectiveBatchConflictError(
                "public_manifest_conflict", "public manifest cannot be verified"
            ) from exc
        if existing != content:
            raise ProspectiveBatchConflictError(
                "public_manifest_conflict", "public manifest is immutable"
            )
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.rename(temporary, path)
        except OSError:
            if path.exists() and path.read_bytes() == content:
                temporary.unlink(missing_ok=True)
                return
            raise
    finally:
        temporary.unlink(missing_ok=True)


def _check_existing_public(path: Path, content: bytes) -> None:
    if not path.exists():
        return
    try:
        existing = path.read_bytes()
    except OSError as exc:
        raise ProspectiveBatchConflictError(
            "public_manifest_conflict", "public manifest cannot be verified"
        ) from exc
    if existing != content:
        raise ProspectiveBatchConflictError(
            "public_manifest_conflict", "public manifest is immutable"
        )


def capture_prospective_batch(
    bundle: Mapping[str, Any],
    *,
    private_root: Path | str,
    public_manifest_path: Path | str | None = None,
    observed_at: datetime | str | None = None,
) -> CaptureResult:
    """Validate first, then create or verify one same-day immutable batch."""

    current_time = _time(
        observed_at if observed_at is not None else datetime.now(SHANGHAI_TZ),
        field="observed_at",
    )
    private_payload, public_summary = _validated_content(
        bundle,
        observed_at=current_time,
    )
    private_bytes = _strict_json_bytes(private_payload)
    private_content_hash = hashlib.sha256(private_bytes).hexdigest()
    private_manifest = _private_manifest(
        batch_id=private_payload["batch_id"],
        private_bytes=private_bytes,
    )
    public_manifest = _public_manifest(
        public_summary,
        private_content_sha256=private_content_hash,
        private_manifest_sha256=private_manifest["manifest_sha256"],
    )
    files = {
        "manifest.json": _strict_json_bytes(private_manifest),
        "private-batch.json": private_bytes,
        "public-manifest.json": _strict_json_bytes(public_manifest),
    }
    root = Path(private_root).resolve()
    target = _archive_dir(root, private_payload["evidence"]["signal_date"])
    public_path = Path(public_manifest_path) if public_manifest_path is not None else None
    if public_path is not None:
        _check_existing_public(public_path, files["public-manifest.json"])
    status = "created"
    try:
        if target.exists():
            _verify_existing(target, expected_files=files)
            status = "exists"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent)
            )
            try:
                for name, content in files.items():
                    path = temporary / name
                    path.write_bytes(content)
                try:
                    os.rename(temporary, target)
                except OSError:
                    if target.exists():
                        _verify_existing(target, expected_files=files)
                        shutil.rmtree(temporary, ignore_errors=True)
                        status = "exists"
                    else:
                        raise
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        if public_path is not None:
            _write_public(public_path, files["public-manifest.json"])
    except ProspectiveBatchError:
        raise
    except OSError as exc:
        raise ProspectiveBatchError(
            "archive_write_failed", "private or public archive write failed"
        ) from exc
    return CaptureResult(
        status=status,
        archive_dir=target,
        batch_id=private_payload["batch_id"],
        private_content_sha256=private_content_hash,
        private_manifest_sha256=private_manifest["manifest_sha256"],
        public_manifest=public_manifest,
    )


def load_private_bundle(path: Path | str) -> Mapping[str, Any]:
    """Load strict private JSON without writing or contacting a provider."""

    source = Path(path)
    try:
        raw = source.read_bytes()
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProspectiveBatchError("input_invalid", "private input is not strict JSON") from exc
    return _mapping(value, field="input")
