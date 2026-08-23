from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json

import pytest

from research.benchmarks.raw_history import CROSS_RAW_SOURCE_ID, PRIMARY_RAW_SOURCE_ID
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.trade_calendar import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    CalendarSourceObservation,
    VerifiedTradeCalendar,
)
from research.benchmarks.universe import UNIVERSE_CONTRACT_VERSION, universe_config_hash
from research.private_acquisition import (
    ACQUISITION_REQUEST_SCHEMA_VERSION,
    PRIVATE_ARCHIVE_POLICY,
    PrivateAcquisitionError,
    acquire_private_shared_batch,
)
from research.prospective_batch import (
    ProspectiveBatchConflictError,
    ProspectiveBatchError,
    _raw_observation,
)
from tests.test_prospective_batch_contract import (
    CUTOFF,
    RAW_DATES,
    SIGNAL_DATE,
    SYMBOLS,
    _bundle,
)


OBSERVED_AT = datetime(2026, 3, 4, 9, 31, 2, tzinfo=SHANGHAI_TZ)


def _request() -> dict:
    bundle = _bundle()
    universe = {
        "config_hash": universe_config_hash(),
        "contract_version": UNIVERSE_CONTRACT_VERSION,
        "stock_codes": list(SYMBOLS),
    }
    universe["snapshot_sha256"] = hashlib.sha256(
        canonical_json_bytes(universe)
    ).hexdigest()
    return {
        "corporate_actions": {
            code: deepcopy(bundle["symbols"][code]["corporate_actions"])
            for code in SYMBOLS
        },
        "private_archive_policy": {
            "corporate_actions": PRIVATE_ARCHIVE_POLICY,
            "provider_terms_reviewed_for_private_capture": True,
            "raw_history": PRIVATE_ARCHIVE_POLICY,
        },
        "request_at": bundle["request_at"],
        "schema_version": ACQUISITION_REQUEST_SCHEMA_VERSION,
        "signal_date": SIGNAL_DATE.isoformat(),
        "universe": universe,
    }


def _sources(*, cross_close: str | None = None, trading_date: bool = True):
    bundle = _bundle()
    calendar_payload = deepcopy(bundle["calendar"])
    if not trading_date:
        calendar_payload["trading_dates"].remove(SIGNAL_DATE.isoformat())
    dates = calendar_payload["trading_dates"]
    calendar = VerifiedTradeCalendar.create(
        query_start=calendar_payload["query_start"],
        query_end=calendar_payload["query_end"],
        primary=CalendarSourceObservation(
            source_id=PRIMARY_SOURCE_ID,
            query_start=calendar_payload["query_start"],
            query_end=calendar_payload["query_end"],
            trading_dates=dates,
            source_data_as_of=calendar_payload["primary_source"]["source_data_as_of"],
            fetched_at=calendar_payload["primary_source"]["fetched_at"],
        ),
        cross=CalendarSourceObservation(
            source_id=CROSS_SOURCE_ID,
            query_start=calendar_payload["query_start"],
            query_end=calendar_payload["query_end"],
            trading_dates=dates,
            source_data_as_of=calendar_payload["cross_source"]["source_data_as_of"],
            fetched_at=calendar_payload["cross_source"]["fetched_at"],
        ),
    )
    calls = {"calendar": 0, "raw": []}

    def fetch_calendar(start, end, *, allow_network):
        assert allow_network is True
        calls["calendar"] += 1
        return calendar

    def fetch_raw(code, start, end, *, allow_network):
        assert allow_network is True
        calls["raw"].append((code, start.isoformat(), end.isoformat()))
        primary_payload = deepcopy(bundle["symbols"][code]["raw_history"]["primary"])
        cross_payload = deepcopy(bundle["symbols"][code]["raw_history"]["cross"])
        if cross_close is not None:
            cross_payload["bars"][-1]["close"] = cross_close
        return (
            _raw_observation(primary_payload, symbol=code, source_id=PRIMARY_RAW_SOURCE_ID),
            _raw_observation(cross_payload, symbol=code, source_id=CROSS_RAW_SOURCE_ID),
        )

    return fetch_calendar, fetch_raw, calls


def _clock():
    values = iter(
        (
            datetime(2026, 3, 4, 9, 30, 0, tzinfo=SHANGHAI_TZ),
            datetime(2026, 3, 4, 9, 31, 0, tzinfo=SHANGHAI_TZ),
        )
    )
    return lambda: next(values)


def _acquire(tmp_path, request=None, **overrides):
    fetch_calendar, fetch_raw, calls = _sources()
    kwargs = {
        "allow_network": True,
        "calendar_fetcher": fetch_calendar,
        "clock": _clock(),
        "observed_at": OBSERVED_AT,
        "private_root": tmp_path / "private",
        "public_manifest_path": tmp_path / "public.json",
        "raw_history_fetcher": fetch_raw,
    }
    kwargs.update(overrides)
    result = acquire_private_shared_batch(request or _request(), **kwargs)
    return result, calls


def _all_keys(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


def test_success_acquires_one_t_minus_one_pair_per_shared_universe_symbol(tmp_path) -> None:
    result, calls = _acquire(tmp_path)

    assert result.capture.status == "created"
    assert calls["calendar"] == 1
    assert calls["raw"] == [
        (code, RAW_DATES[0], CUTOFF) for code in SYMBOLS
    ]
    bindings = result.capture.public_manifest["model_bindings"]
    assert len({row["batch_id"] for row in bindings.values()}) == 1
    assert len({row["shared_evidence_sha256"] for row in bindings.values()}) == 1


def test_network_requires_explicit_opt_in_before_any_source_call(tmp_path) -> None:
    fetch_calendar, fetch_raw, calls = _sources()
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        acquire_private_shared_batch(
            _request(),
            private_root=tmp_path,
            allow_network=False,
            observed_at=OBSERVED_AT,
            calendar_fetcher=fetch_calendar,
            raw_history_fetcher=fetch_raw,
        )
    assert exc_info.value.reason_code == "network_opt_in_required"
    assert calls == {"calendar": 0, "raw": []}


def test_dual_source_conflict_fails_without_archive(tmp_path) -> None:
    fetch_calendar, fetch_raw, _ = _sources(cross_close="10.5")
    with pytest.raises(ProspectiveBatchError) as exc_info:
        _acquire(
            tmp_path,
            calendar_fetcher=fetch_calendar,
            raw_history_fetcher=fetch_raw,
        )
    assert exc_info.value.reason_code == "raw_history_contract_failed"
    assert not (tmp_path / "private").exists()


def test_request_time_after_source_fetch_fails_closed(tmp_path) -> None:
    request = _request()
    request["request_at"] = "2026-03-04T08:02:00+08:00"
    with pytest.raises(ProspectiveBatchError) as exc_info:
        _acquire(tmp_path, request=request)
    assert exc_info.value.reason_code == "time_contract_failed"


def test_identical_repeat_returns_exists(tmp_path) -> None:
    first, _ = _acquire(tmp_path)
    second, _ = _acquire(tmp_path)
    assert first.capture.status == "created"
    assert second.capture.status == "exists"
    assert first.capture.private_content_sha256 == second.capture.private_content_sha256


def test_same_day_content_change_is_rejected_without_overwrite(tmp_path) -> None:
    first, _ = _acquire(tmp_path)
    original = (first.capture.archive_dir / "private-batch.json").read_bytes()
    changed = _request()
    for code in SYMBOLS:
        for role in ("primary", "cross"):
            changed["corporate_actions"][code][role]["events"][0]["cash_per_share"] = "0.6"
    with pytest.raises(ProspectiveBatchConflictError):
        _acquire(tmp_path, request=changed)
    assert (first.capture.archive_dir / "private-batch.json").read_bytes() == original


def test_failed_retry_does_not_reuse_old_success(tmp_path) -> None:
    first, _ = _acquire(tmp_path)
    fetch_calendar, _, _ = _sources()

    def fail_raw(*args, **kwargs):
        raise OSError("synthetic failure")

    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _acquire(
            tmp_path,
            calendar_fetcher=fetch_calendar,
            raw_history_fetcher=fail_raw,
        )
    assert exc_info.value.reason_code == "raw_history_acquisition_failed"
    assert (first.capture.archive_dir / "private-batch.json").is_file()


def test_corporate_action_evidence_must_cover_exact_universe(tmp_path) -> None:
    request = _request()
    del request["corporate_actions"][SYMBOLS[-1]]
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _acquire(tmp_path, request=request)
    assert exc_info.value.reason_code == "shared_universe_evidence_mismatch"


def test_public_manifest_is_redacted(tmp_path) -> None:
    result, _ = _acquire(tmp_path)
    public = json.loads((tmp_path / "public.json").read_text(encoding="utf-8"))
    serialized = json.dumps(public, ensure_ascii=False)
    forbidden_keys = {
        "amount",
        "bars",
        "close",
        "events",
        "high",
        "low",
        "open",
        "stock_codes",
        "suspended_dates",
        "symbol",
        "volume",
    }
    assert forbidden_keys.isdisjoint(set(_all_keys(public)))
    assert all(code not in serialized for code in SYMBOLS)
    assert public == result.capture.public_manifest


def test_universe_hash_mismatch_fails_before_network(tmp_path) -> None:
    request = _request()
    request["universe"]["snapshot_sha256"] = "0" * 64
    fetch_calendar, fetch_raw, calls = _sources()
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _acquire(
            tmp_path,
            request=request,
            calendar_fetcher=fetch_calendar,
            raw_history_fetcher=fetch_raw,
        )
    assert exc_info.value.reason_code == "universe_contract_failed"
    assert calls == {"calendar": 0, "raw": []}


def test_private_license_boundary_must_be_explicit(tmp_path) -> None:
    request = _request()
    request["private_archive_policy"]["provider_terms_reviewed_for_private_capture"] = False
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _acquire(tmp_path, request=request)
    assert exc_info.value.reason_code == "private_license_boundary_unconfirmed"


def test_non_trading_signal_date_fails_before_raw_fetch(tmp_path) -> None:
    fetch_calendar, fetch_raw, calls = _sources(trading_date=False)
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _acquire(
            tmp_path,
            calendar_fetcher=fetch_calendar,
            raw_history_fetcher=fetch_raw,
        )
    assert exc_info.value.reason_code == "signal_date_not_trading_day"
    assert calls["raw"] == []


def test_post_close_run_still_refuses_signal_date_bar(tmp_path) -> None:
    request = _request()
    request["request_at"] = "2026-03-04T15:01:00+08:00"
    fetch_calendar, fetch_raw, calls = _sources()
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _acquire(
            tmp_path,
            request=request,
            observed_at="2026-03-04T15:31:02+08:00",
            calendar_fetcher=fetch_calendar,
            raw_history_fetcher=fetch_raw,
        )
    assert exc_info.value.reason_code == "t_minus_one_cutoff_required"
    assert calls["raw"] == []
