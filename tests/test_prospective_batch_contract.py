from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json

import pytest

from research.benchmarks.corporate_actions import (
    CROSS_SOURCE_IDS,
    PRIMARY_SOURCE_IDS,
)
from research.benchmarks.low_volatility import MODEL_NAME as LOW_VOLATILITY_MODEL
from research.benchmarks.raw_history import (
    CROSS_ADJUSTMENT,
    CROSS_RAW_SOURCE_ID,
    PRICE_BASIS,
    PRIMARY_ADJUSTMENT,
    PRIMARY_RAW_SOURCE_ID,
)
from research.benchmarks.short_term import MODEL_NAME as SHORT_TERM_MODEL
from research.benchmarks.trade_calendar import CROSS_SOURCE_ID, PRIMARY_SOURCE_ID
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
)
from research.prospective_batch import (
    INPUT_SCHEMA_VERSION,
    PUBLIC_PAYLOAD_POLICY,
    ProspectiveBatchConflictError,
    ProspectiveBatchError,
    capture_prospective_batch as _capture_prospective_batch,
)


SYMBOLS = ("600001", "600002")
SIGNAL_DATE = date(2026, 3, 4)
TRADE_DATES = tuple(
    (SIGNAL_DATE - timedelta(days=61 - index)).isoformat()
    for index in range(62)
)
CUTOFF = TRADE_DATES[-2]
RAW_DATES = TRADE_DATES[:-1]


def _bars() -> list[dict]:
    return [
        {
            "amount": str(100000 + index),
            "close": "10",
            "high": "11",
            "is_trading": True,
            "low": "9",
            "open": "10",
            "trade_date": trade_date,
            "volume": str(10000 + index),
        }
        for index, trade_date in enumerate(RAW_DATES)
    ]


def _raw(source_id: str) -> dict:
    return {
        "adjustment": (
            PRIMARY_ADJUSTMENT
            if source_id == PRIMARY_RAW_SOURCE_ID
            else CROSS_ADJUSTMENT
        ),
        "amount_unit": "CNY",
        "bars": _bars(),
        "fetched_at": (
            "2026-03-04T09:05:00+08:00"
            if source_id == PRIMARY_RAW_SOURCE_ID
            else "2026-03-04T09:06:00+08:00"
        ),
        "price_basis": PRICE_BASIS,
        "requested_end": CUTOFF,
        "requested_start": RAW_DATES[0],
        "volume_unit": "share",
    }


def _event(symbol: str, *, cash: str = "0.5") -> dict:
    return {
        "action_type": "cash_dividend",
        "cash_per_share": cash,
        "ex_date": TRADE_DATES[5],
        "known_at": "2026-01-03T18:00:00+08:00",
        "payment_date": TRADE_DATES[6],
        "record_date": TRADE_DATES[4],
        "symbol": symbol,
    }


def _actions(symbol: str, *, primary: bool) -> dict:
    return {
        "events": [_event(symbol)],
        "fetched_at": (
            "2026-03-04T09:07:00+08:00"
            if primary
            else "2026-03-04T09:08:00+08:00"
        ),
        "source_data_as_of": "2026-03-04T08:30:00+08:00",
        "source_id": (
            sorted(PRIMARY_SOURCE_IDS)[0]
            if primary
            else sorted(CROSS_SOURCE_IDS)[0]
        ),
    }


def _bundle() -> dict:
    calendar_without_hash = {
        "cross_source": {
            "fetched_at": "2026-03-04T08:01:00+08:00",
            "source_data_as_of": "2026-03-04T08:00:00+08:00",
            "source_id": CROSS_SOURCE_ID,
        },
        "primary_source": {
            "fetched_at": "2026-03-04T08:01:00+08:00",
            "source_data_as_of": "2026-03-04T08:00:00+08:00",
            "source_id": PRIMARY_SOURCE_ID,
        },
        "query_end": SIGNAL_DATE.isoformat(),
        "query_start": TRADE_DATES[0],
        "trading_dates": list(TRADE_DATES),
    }
    from research.benchmarks.trade_calendar import (
        CalendarSourceObservation,
        VerifiedTradeCalendar,
    )

    def observation(name: str) -> CalendarSourceObservation:
        source = calendar_without_hash[name]
        return CalendarSourceObservation(
            source_id=source["source_id"],
            query_start=calendar_without_hash["query_start"],
            query_end=calendar_without_hash["query_end"],
            trading_dates=calendar_without_hash["trading_dates"],
            source_data_as_of=source["source_data_as_of"],
            fetched_at=source["fetched_at"],
        )

    calendar = VerifiedTradeCalendar.create(
        query_start=calendar_without_hash["query_start"],
        query_end=calendar_without_hash["query_end"],
        primary=observation("primary_source"),
        cross=observation("cross_source"),
    )
    return {
        "calendar": {**calendar_without_hash, "content_sha256": calendar.content_sha256},
        "captured_at": "2026-03-04T09:31:00+08:00",
        "market_data_at": "2026-03-04T09:30:00+08:00",
        "request_at": "2026-03-04T08:00:00+08:00",
        "schema_version": INPUT_SCHEMA_VERSION,
        "signal_date": SIGNAL_DATE.isoformat(),
        "symbols": {
            symbol: {
                "corporate_actions": {
                    "cross": _actions(symbol, primary=False),
                    "primary": _actions(symbol, primary=True),
                },
                "raw_history": {
                    "cross": _raw(CROSS_RAW_SOURCE_ID),
                    "primary": _raw(PRIMARY_RAW_SOURCE_ID),
                },
            }
            for symbol in SYMBOLS
        },
        "universe": {
            "config_hash": universe_config_hash(),
            "contract_version": UNIVERSE_CONTRACT_VERSION,
            "stock_codes": list(SYMBOLS),
        },
    }


def capture_prospective_batch(bundle, **kwargs):
    """Run the deterministic fixture at its recorded wall-clock instant."""

    observed_at = kwargs.pop("observed_at", "2026-03-04T09:31:01+08:00")
    return _capture_prospective_batch(
        bundle,
        observed_at=observed_at,
        **kwargs,
    )


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def test_capture_creates_private_batch_and_sanitized_public_manifest(tmp_path) -> None:
    private_root = tmp_path / "private"
    public_path = tmp_path / "public" / "manifest.json"

    result = capture_prospective_batch(
        _bundle(),
        private_root=private_root,
        public_manifest_path=public_path,
    )

    assert result.status == "created"
    assert sorted(item.name for item in result.archive_dir.iterdir()) == [
        "manifest.json",
        "private-batch.json",
        "public-manifest.json",
    ]
    private = json.loads((result.archive_dir / "private-batch.json").read_text())
    public = json.loads(public_path.read_text())
    assert private["evidence"]["symbols"][SYMBOLS[0]]["raw_history"]["primary"][
        "bars"
    ]
    raw_primary = private["evidence"]["symbols"][SYMBOLS[0]]["raw_history"][
        "primary"
    ]
    assert raw_primary["source_data_as_of"] == raw_primary["fetched_at"]
    assert public["public_payload_policy"] == PUBLIC_PAYLOAD_POLICY
    assert public["symbol_count"] == len(SYMBOLS)
    forbidden = {
        "amount",
        "bars",
        "close",
        "events",
        "high",
        "low",
        "open",
        "stock_codes",
        "symbols",
        "volume",
    }
    assert forbidden.isdisjoint(set(_all_keys(public)))


def test_same_input_is_idempotent_and_does_not_add_files(tmp_path) -> None:
    first = capture_prospective_batch(_bundle(), private_root=tmp_path)
    second = capture_prospective_batch(_bundle(), private_root=tmp_path)

    assert first.status == "created"
    assert second.status == "exists"
    assert first.batch_id == second.batch_id
    assert first.private_content_sha256 == second.private_content_sha256
    assert len(list(first.archive_dir.iterdir())) == 3


def test_same_day_different_valid_content_is_conflict(tmp_path) -> None:
    capture_prospective_batch(_bundle(), private_root=tmp_path)
    changed = _bundle()
    for source in ("primary", "cross"):
        row = changed["symbols"][SYMBOLS[0]]["raw_history"][source]["bars"][-1]
        row.update(open="11", high="12", low="10", close="11")

    with pytest.raises(
        ProspectiveBatchConflictError, match="same-day immutable content differs"
    ):
        capture_prospective_batch(changed, private_root=tmp_path)


def test_t_day_completed_bar_is_rejected() -> None:
    bundle = _bundle()
    bundle["market_data_at"] = "2026-03-04T15:01:00+08:00"
    bundle["captured_at"] = "2026-03-04T15:02:00+08:00"

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(
            bundle,
            private_root="unused",
            observed_at="2026-03-04T15:02:01+08:00",
        )

    assert exc_info.value.reason_code == "t_minus_one_cutoff_required"


def test_backdated_capture_is_not_prospective_same_day() -> None:
    bundle = _bundle()
    bundle["captured_at"] = "2026-03-05T09:31:00+08:00"

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root="unused")

    assert exc_info.value.reason_code == "not_prospective_same_day"


def test_historical_bundle_cannot_pass_a_later_wall_clock(tmp_path) -> None:
    with pytest.raises(ProspectiveBatchError) as exc_info:
        _capture_prospective_batch(
            _bundle(),
            private_root=tmp_path,
            observed_at="2026-03-05T09:31:00+08:00",
        )

    assert exc_info.value.reason_code == "not_prospective_wall_clock"
    assert not any(tmp_path.iterdir())


def test_calendar_fetch_before_request_fails_closed(tmp_path) -> None:
    bundle = _bundle()
    bundle["request_at"] = "2026-03-04T08:02:00+08:00"

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root=tmp_path)

    assert exc_info.value.reason_code == "time_contract_failed"
    assert not any(tmp_path.iterdir())


def test_raw_dual_source_conflict_fails_closed(tmp_path) -> None:
    bundle = _bundle()
    bundle["symbols"][SYMBOLS[0]]["raw_history"]["cross"]["bars"][-1][
        "close"
    ] = "10.5"

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root=tmp_path)

    assert exc_info.value.reason_code == "raw_history_contract_failed"
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("price_basis", ["qfq", "hfq"])
def test_adjusted_history_is_rejected(tmp_path, price_basis: str) -> None:
    bundle = _bundle()
    for source in ("primary", "cross"):
        bundle["symbols"][SYMBOLS[0]]["raw_history"][source][
            "price_basis"
        ] = price_basis

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root=tmp_path)

    assert exc_info.value.reason_code == "raw_history_contract_failed"
    assert not any(tmp_path.iterdir())


def test_incomplete_61_session_window_fails_closed(tmp_path) -> None:
    bundle = _bundle()
    for source in ("primary", "cross"):
        raw = bundle["symbols"][SYMBOLS[0]]["raw_history"][source]
        raw["bars"] = raw["bars"][1:]
        raw["requested_start"] = RAW_DATES[1]

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root=tmp_path)

    assert exc_info.value.reason_code == "history_window_contract_failed"


def test_corporate_action_cross_conflict_fails_closed(tmp_path) -> None:
    bundle = _bundle()
    bundle["symbols"][SYMBOLS[0]]["corporate_actions"]["cross"]["events"][0][
        "cash_per_share"
    ] = "0.4"

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root=tmp_path)

    assert exc_info.value.reason_code == "corporate_action_contract_failed"


def test_future_known_action_fails_before_existing_success_is_reused(tmp_path) -> None:
    result = capture_prospective_batch(_bundle(), private_root=tmp_path)
    before = {
        item.name: item.read_bytes()
        for item in result.archive_dir.iterdir()
        if item.is_file()
    }
    failed = _bundle()
    for source in ("primary", "cross"):
        failed["symbols"][SYMBOLS[0]]["corporate_actions"][source]["events"][0][
            "known_at"
        ] = "2026-03-04T09:31:00+08:00"

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(failed, private_root=tmp_path)

    assert exc_info.value.reason_code == "corporate_action_contract_failed"
    assert before == {
        item.name: item.read_bytes()
        for item in result.archive_dir.iterdir()
        if item.is_file()
    }


def test_provider_fetch_after_market_data_at_fails_closed(tmp_path) -> None:
    bundle = _bundle()
    bundle["symbols"][SYMBOLS[0]]["raw_history"]["primary"]["fetched_at"] = (
        "2026-03-04T09:31:00+08:00"
    )

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root=tmp_path)

    assert exc_info.value.reason_code == "raw_history_contract_failed"


def test_models_are_bound_to_one_identical_evidence_hash(tmp_path) -> None:
    result = capture_prospective_batch(_bundle(), private_root=tmp_path)
    bindings = result.public_manifest["model_bindings"]

    assert set(bindings) == {SHORT_TERM_MODEL, LOW_VOLATILITY_MODEL}
    assert bindings[SHORT_TERM_MODEL] == bindings[LOW_VOLATILITY_MODEL]


def test_missing_one_universe_symbol_fails_before_writing(tmp_path) -> None:
    bundle = _bundle()
    del bundle["symbols"][SYMBOLS[-1]]

    with pytest.raises(ProspectiveBatchError) as exc_info:
        capture_prospective_batch(bundle, private_root=tmp_path)

    assert exc_info.value.reason_code == "shared_universe_evidence_mismatch"
    assert not any(tmp_path.iterdir())


def test_corrupted_existing_archive_is_never_overwritten(tmp_path) -> None:
    result = capture_prospective_batch(_bundle(), private_root=tmp_path)
    private_path = result.archive_dir / "private-batch.json"
    private_path.write_text("corrupt", encoding="utf-8")

    with pytest.raises(ProspectiveBatchConflictError):
        capture_prospective_batch(_bundle(), private_root=tmp_path)

    assert private_path.read_text(encoding="utf-8") == "corrupt"


def test_public_manifest_is_immutable(tmp_path) -> None:
    public_path = tmp_path / "public.json"
    public_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ProspectiveBatchConflictError) as exc_info:
        capture_prospective_batch(
            _bundle(),
            private_root=tmp_path / "private",
            public_manifest_path=public_path,
        )

    assert exc_info.value.reason_code == "public_manifest_conflict"
    assert public_path.read_text(encoding="utf-8") == "{}\n"
    assert not (tmp_path / "private").exists()


def test_input_order_is_deterministic(tmp_path) -> None:
    first_bundle = _bundle()
    second_bundle = deepcopy(first_bundle)
    second_bundle["symbols"] = dict(reversed(list(second_bundle["symbols"].items())))

    first = capture_prospective_batch(first_bundle, private_root=tmp_path / "one")
    second = capture_prospective_batch(second_bundle, private_root=tmp_path / "two")

    assert first.private_content_sha256 == second.private_content_sha256
    assert first.public_manifest == second.public_manifest
