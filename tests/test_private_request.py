from __future__ import annotations

from datetime import date, datetime, timedelta
import json

import pandas as pd
import pytest

from research.benchmarks.corporate_actions import CorporateActionObservation
from research.benchmarks.schema import SHANGHAI_TZ
from research.data_sources.corporate_actions import (
    CROSS_SOURCE_ID,
    PRIMARY_SOURCE_ID,
    cninfo_events,
    sina_events,
)
from research.private_acquisition import PrivateAcquisitionError
from research.private_request import prepare_private_acquisition_request
from tests.test_private_acquisition import _sources
from tests.test_prospective_batch_contract import SIGNAL_DATE, SYMBOLS


class _Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def _spot(clock: _Clock, *, amount: float = 300_000_000) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "amount": amount,
                "close": 10.0,
                "code": code,
                "name": f"测试{code}",
                "pct_change": 0.0,
                "turnover": 2.0,
                "volume": 30_000_000,
            }
            for code in SYMBOLS
        ]
    )
    frame.attrs["market_data_source"] = "akshare_eastmoney"
    frame.attrs["market_data_at"] = clock().isoformat(timespec="seconds")
    return frame


def _action_fetcher(clock: _Clock):
    def fetch(code, start, end, *, allow_network):
        assert allow_network is True

        def observation(source_id):
            fetched_at = clock()
            return CorporateActionObservation.create(
                source_id=source_id,
                source_data_as_of=fetched_at,
                fetched_at=fetched_at,
                symbol=code,
                query_start=start,
                query_end=end,
                query_status="success",
                query_result="no_event",
                events=[],
            )

        return observation(PRIMARY_SOURCE_ID), observation(CROSS_SOURCE_ID)

    return fetch


def _prepare(tmp_path, *, clock=None, action_fetcher=None, spot_fetcher=None):
    selected_clock = clock or _Clock(
        datetime(2026, 3, 4, 9, 40, tzinfo=SHANGHAI_TZ)
    )
    calendar_fetcher, _, _ = _sources()
    return prepare_private_acquisition_request(
        signal_date=SIGNAL_DATE,
        request_path=tmp_path / "2026-03-04.json",
        deadline_at="2026-03-04T10:00:00+08:00",
        provider_terms_reviewed_for_private_capture=True,
        allow_network=True,
        spot_fetcher=spot_fetcher or (lambda: _spot(selected_clock)),
        calendar_fetcher=calendar_fetcher,
        action_fetcher=action_fetcher or _action_fetcher(selected_clock),
        clock=selected_clock,
    )


def test_prepare_writes_complete_private_request_once(tmp_path) -> None:
    result = _prepare(tmp_path)
    payload = json.loads(result.path.read_text(encoding="utf-8"))

    assert result.status == "created"
    assert payload["schema_version"] == "private-acquisition-request-v2"
    assert payload["signal_date"] == SIGNAL_DATE.isoformat()
    assert payload["universe"]["stock_codes"] == list(SYMBOLS)
    assert payload["universe"]["content_sha256"] == result.universe_sha256
    assert payload["universe_source"]["row_count"] == len(SYMBOLS)
    assert set(payload["corporate_actions"]) == set(SYMBOLS)
    assert all(
        item[role]["query_status"] == "success"
        and item[role]["query_result"] == "no_event"
        for item in payload["corporate_actions"].values()
        for role in ("primary", "cross")
    )


def test_identical_request_is_idempotent(tmp_path) -> None:
    first = _prepare(tmp_path)
    second = _prepare(tmp_path)
    assert first.status == "created"
    assert second.status == "exists"
    assert first.path.read_bytes() == second.path.read_bytes()


def test_changed_same_day_request_is_rejected(tmp_path) -> None:
    first = _prepare(tmp_path)
    changed_clock = _Clock(datetime(2026, 3, 4, 9, 40, tzinfo=SHANGHAI_TZ))
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _prepare(
            tmp_path,
            clock=changed_clock,
            spot_fetcher=lambda: _spot(changed_clock, amount=400_000_000),
        )
    assert exc_info.value.reason_code == "private_request_conflict"
    assert first.path.is_file()


def test_source_failure_writes_no_request(tmp_path) -> None:
    def fail(*args, **kwargs):
        raise OSError("synthetic source failure")

    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _prepare(tmp_path, action_fetcher=fail)
    assert exc_info.value.reason_code == "corporate_action_acquisition_failed"
    assert not (tmp_path / "2026-03-04.json").exists()


def test_failed_query_cannot_become_no_event(tmp_path) -> None:
    def failed_no_event(code, start, end, *, allow_network):
        return CorporateActionObservation.create(
            source_id=PRIMARY_SOURCE_ID,
            source_data_as_of="2026-03-04T09:41:00+08:00",
            fetched_at="2026-03-04T09:41:00+08:00",
            symbol=code,
            query_start=start,
            query_end=end,
            query_status="failed",
            query_result="no_event",
            events=[],
        )

    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _prepare(tmp_path, action_fetcher=failed_no_event)
    assert exc_info.value.reason_code == "corporate_action_acquisition_failed"
    assert not (tmp_path / "2026-03-04.json").exists()


def test_deadline_miss_writes_no_request(tmp_path) -> None:
    clock = _Clock(datetime(2026, 3, 4, 10, 0, tzinfo=SHANGHAI_TZ))
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        _prepare(tmp_path, clock=clock)
    assert exc_info.value.reason_code == "prospective_deadline_missed"
    assert not (tmp_path / "2026-03-04.json").exists()


def test_network_and_private_terms_are_explicit(tmp_path) -> None:
    with pytest.raises(PrivateAcquisitionError) as exc_info:
        prepare_private_acquisition_request(
            signal_date=SIGNAL_DATE,
            request_path=tmp_path / "request.json",
            deadline_at="2026-03-04T10:00:00+08:00",
            provider_terms_reviewed_for_private_capture=True,
            allow_network=False,
        )
    assert exc_info.value.reason_code == "network_opt_in_required"


def test_synthetic_real_event_sources_normalize_to_same_semantics() -> None:
    fetched_at = datetime(2026, 3, 4, 9, 45, tzinfo=SHANGHAI_TZ)
    primary = cninfo_events(
        SYMBOLS[0],
        date(2026, 1, 5),
        date(2026, 3, 3),
        fetched_at=fetched_at,
        dividends=pd.DataFrame(
            [
                {
                    "送股比例": 0,
                    "转增比例": 3,
                    "派息比例": 3.5,
                    "股权登记日": "2026-02-26",
                    "除权日": "2026-02-27",
                    "派息日": "2026-02-27",
                    "股份到账日": "2026-02-27",
                }
            ]
        ),
        allotments=pd.DataFrame(),
    )
    cross = sina_events(
        SYMBOLS[0],
        date(2026, 1, 5),
        date(2026, 3, 3),
        fetched_at=fetched_at,
        dividends=pd.DataFrame(
            [
                {
                    "公告日期": "2026-02-20",
                    "送股": 0,
                    "转增": 3,
                    "派息": 3.5,
                    "除权除息日": "2026-02-27",
                    "股权登记日": "2026-02-26",
                    "红股上市日": "2026-02-27",
                }
            ]
        ),
        allotments=pd.DataFrame(),
        dividend_detail_fetcher=lambda _: pd.DataFrame(
            [["派息日", "2026-02-27"]]
        ),
    )
    assert [item.semantic_sha256 for item in primary] == [
        item.semantic_sha256 for item in cross
    ]
