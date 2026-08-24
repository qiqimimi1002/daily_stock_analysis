"""Prepare one same-day Private acquisition request before formal screening."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import os
from pathlib import Path
from typing import Callable, Mapping

import pandas as pd

from research.benchmarks.corporate_actions import CorporateActionObservation
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.trade_calendar import VerifiedTradeCalendar
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
    v21_hard_filter_codes,
)
from research.data_sources.corporate_actions import (
    fetch_corporate_action_pair,
    observation_payload,
)
from research.data_sources.trade_calendar import fetch_verified_trade_calendar
from research.private_acquisition import (
    ACQUISITION_REQUEST_SCHEMA_VERSION,
    CALENDAR_LOOKBACK_DAYS,
    PRIVATE_ARCHIVE_POLICY,
    REQUIRED_HISTORY_OBSERVATIONS,
    PrivateAcquisitionError,
    _content_sha256,
    _time,
    _validated_request,
    canonical_private_spot_rows,
)
from research.prospective_batch import PRIVATE_UNIVERSE_SOURCE_SCHEMA_VERSION
from src.services.market_screener import (
    PublicMarketDataSource,
    ScreeningConfig,
)


SpotFetcher = Callable[[], pd.DataFrame]
CalendarFetcher = Callable[..., VerifiedTradeCalendar]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class PrivateRequestResult:
    status: str
    path: Path
    spot_row_count: int
    universe_count: int
    universe_sha256: str


def fetch_primary_production_spot() -> pd.DataFrame:
    """Use the production primary full-market endpoint without its fallbacks."""

    import akshare as ak

    frame = ak.stock_zh_a_spot_em()
    if frame is None or frame.empty:
        raise PrivateAcquisitionError(
            "universe_acquisition_failed", "primary production spot source returned no rows"
        )
    return PublicMarketDataSource._mark_spot_snapshot(frame, "akshare_eastmoney")


def _write_once(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise PrivateAcquisitionError(
                "private_request_conflict", "same-day Private request already differs"
            )
        return "exists"
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != payload:
            raise PrivateAcquisitionError(
                "private_request_conflict", "same-day Private request already differs"
            )
        return "exists"
    return "created"


def prepare_private_acquisition_request(
    *,
    signal_date: date,
    request_path: Path | str,
    deadline_at: datetime | str,
    provider_terms_reviewed_for_private_capture: bool,
    allow_network: bool = False,
    spot_fetcher: SpotFetcher = fetch_primary_production_spot,
    calendar_fetcher: CalendarFetcher = fetch_verified_trade_calendar,
    action_fetcher: Callable[..., tuple[CorporateActionObservation, CorporateActionObservation]] = fetch_corporate_action_pair,
    clock: Clock | None = None,
) -> PrivateRequestResult:
    """Fetch complete same-run request evidence and write it exactly once."""

    if allow_network is not True:
        raise PrivateAcquisitionError(
            "network_opt_in_required", "request preparation requires explicit network opt-in"
        )
    if provider_terms_reviewed_for_private_capture is not True:
        raise PrivateAcquisitionError(
            "private_license_boundary_unconfirmed",
            "Private provider terms review must be explicitly confirmed",
        )
    now = clock or (lambda: datetime.now(SHANGHAI_TZ))
    request_at = _time(now(), field="request_at")
    deadline = _time(deadline_at, field="deadline_at")
    if request_at.date() != signal_date or deadline.date() != signal_date:
        raise PrivateAcquisitionError(
            "not_prospective_wall_clock", "request and deadline must use the signal date"
        )
    if request_at >= deadline:
        raise PrivateAcquisitionError(
            "prospective_deadline_missed", "request preparation started after the deadline"
        )

    try:
        spot_frame = spot_fetcher()
    except PrivateAcquisitionError:
        raise
    except Exception as exc:
        raise PrivateAcquisitionError(
            "universe_acquisition_failed", type(exc).__name__
        ) from exc
    source_id = str(spot_frame.attrs.get("market_data_source") or "").strip()
    source_data_as_of = _time(
        spot_frame.attrs.get("market_data_at"), field="universe_source.source_data_as_of"
    )
    fetched_at = _time(now(), field="universe_source.fetched_at")
    if not (
        request_at <= source_data_as_of <= fetched_at < deadline
        and source_data_as_of.date() == fetched_at.date() == signal_date
    ):
        raise PrivateAcquisitionError(
            "universe_time_contract_failed", "spot snapshot missed the prospective window"
        )
    rows = canonical_private_spot_rows(spot_frame)
    normalized_frame = pd.DataFrame(rows)
    codes = v21_hard_filter_codes(normalized_frame, config=ScreeningConfig())
    if not codes:
        raise PrivateAcquisitionError(
            "universe_contract_failed", "V2.1 Universe cannot be empty"
        )
    generated_at = _time(now(), field="universe_source.generated_at")
    if generated_at >= deadline:
        raise PrivateAcquisitionError(
            "prospective_deadline_missed", "Universe generation missed the deadline"
        )

    try:
        calendar = calendar_fetcher(
            signal_date - timedelta(days=CALENDAR_LOOKBACK_DAYS),
            signal_date,
            allow_network=True,
        )
    except Exception as exc:
        raise PrivateAcquisitionError(
            "calendar_acquisition_failed", type(exc).__name__
        ) from exc
    if signal_date not in calendar.trading_dates:
        raise PrivateAcquisitionError(
            "signal_date_not_trading_day", "signal date is not a verified trading day"
        )
    cutoff = calendar.previous_completed_trade_date(request_at)
    eligible_dates = tuple(item for item in calendar.trading_dates if item <= cutoff)
    if cutoff >= signal_date or len(eligible_dates) < REQUIRED_HISTORY_OBSERVATIONS:
        raise PrivateAcquisitionError(
            "history_window_incomplete", "61 completed T-1 sessions are required"
        )
    query_start = eligible_dates[-REQUIRED_HISTORY_OBSERVATIONS]

    actions: dict[str, Mapping[str, object]] = {}
    for code in codes:
        if _time(now(), field="observed_at") >= deadline:
            raise PrivateAcquisitionError(
                "prospective_deadline_missed", "corporate-action collection missed the deadline"
            )
        try:
            primary, cross = action_fetcher(
                code, query_start, cutoff, allow_network=True
            )
        except Exception as exc:
            raise PrivateAcquisitionError(
                "corporate_action_acquisition_failed", type(exc).__name__
            ) from exc
        checked_at = _time(now(), field="observed_at")
        for observation in (primary, cross):
            if not (
                request_at
                <= observation.source_data_as_of
                <= observation.fetched_at
                <= checked_at
                < deadline
                and observation.fetched_at.date() == signal_date
            ):
                raise PrivateAcquisitionError(
                    "corporate_action_time_contract_failed",
                    "corporate-action evidence missed the prospective window",
                )
        actions[code] = {
            "cross": observation_payload(cross),
            "primary": observation_payload(primary),
        }

    config = asdict(ScreeningConfig())
    universe_sha256 = _content_sha256(list(codes))
    request = {
        "corporate_actions": actions,
        "private_archive_policy": {
            "corporate_actions": PRIVATE_ARCHIVE_POLICY,
            "provider_terms_reviewed_for_private_capture": True,
            "raw_history": PRIVATE_ARCHIVE_POLICY,
            "universe": PRIVATE_ARCHIVE_POLICY,
        },
        "request_at": request_at.isoformat(timespec="seconds"),
        "schema_version": ACQUISITION_REQUEST_SCHEMA_VERSION,
        "signal_date": signal_date.isoformat(),
        "universe": {
            "config_hash": universe_config_hash(),
            "content_sha256": universe_sha256,
            "contract_version": UNIVERSE_CONTRACT_VERSION,
            "stock_codes": list(codes),
        },
        "universe_source": {
            "config": config,
            "config_sha256": _content_sha256(config),
            "fetched_at": fetched_at.isoformat(timespec="seconds"),
            "generated_at": generated_at.isoformat(timespec="seconds"),
            "model_version": "V2.1",
            "row_count": len(rows),
            "schema_version": PRIVATE_UNIVERSE_SOURCE_SCHEMA_VERSION,
            "source_data_as_of": source_data_as_of.isoformat(timespec="seconds"),
            "source_id": source_id,
            "spot_content_sha256": _content_sha256(rows),
            "spot_rows": rows,
        },
    }
    completed_at = _time(now(), field="observed_at")
    if completed_at >= deadline:
        raise PrivateAcquisitionError(
            "prospective_deadline_missed", "request completion missed the deadline"
        )
    _validated_request(request, observed_at=completed_at)
    payload = canonical_json_bytes(request) + b"\n"
    path = Path(request_path).resolve()
    status = _write_once(path, payload)
    return PrivateRequestResult(
        status=status,
        path=path,
        spot_row_count=len(rows),
        universe_count=len(codes),
        universe_sha256=universe_sha256,
    )
