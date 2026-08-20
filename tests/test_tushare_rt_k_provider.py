"""Contract tests for the paid Tushare ``rt_k`` realtime daily provider.

All rows in this module are synthetic fixtures.  They are not market evidence.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data_provider.tushare_rt_k_provider import (
    TushareRtKAuthError,
    TushareRtKProvider,
    TushareRtKValidationError,
    validate_rt_k_stock_codes,
)


CN_TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _synthetic_token_environment(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "synthetic-placeholder")


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def query(self, api_name: str, fields: str = "", **kwargs):
        self.calls.append((api_name, fields, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome.copy()


def _row(**overrides):
    row = {
        "ts_code": "600000.SH",
        "name": "合成样本A",
        "pre_close": 10.0,
        "open": 10.1,
        "high": 10.3,
        "low": 10.0,
        "close": 10.2,
        "vol": 12_345_600,
        "amount": 125_678_900,
        "num": 8_888,
        "bid_price1": 10.19,
        "bid_volume1": 12_300,
        "ask_price1": 10.20,
        "ask_volume1": 9_800,
        "trade_time": "2026-08-20 10:00:05",
    }
    row.update(overrides)
    return row


def _provider(
    outcomes,
    *,
    now=datetime(2026, 8, 20, 10, 0, 20, tzinfo=CN_TZ),
    phase="intraday",
    sleeps=None,
):
    client = FakeClient(outcomes)
    sleep_calls = sleeps if sleeps is not None else []
    provider = TushareRtKProvider(
        client=client,
        now=lambda: now,
        market_phase=lambda _: phase,
        sleep=lambda seconds: sleep_calls.append(seconds),
        max_attempts=3,
        max_stale_seconds=180,
    )
    return provider, client, sleep_calls


@pytest.mark.parametrize("stock_code", ["600000.SH", "000001.SZ"])
def test_stock_code_validator_accepts_exact_uppercase_codes(stock_code):
    assert validate_rt_k_stock_codes(stock_code) == [stock_code]


@pytest.mark.parametrize(
    "stock_codes",
    [
        "BAD.SH",
        "60000.SH",
        "6000000.SH",
        "60A000.SH",
        "600000.SS",
        "600000",
        "600000.sh",
        "000001.sz",
        "",
        "600000.SH,",
        ",600000.SH",
        "600000.SH,,000001.SZ",
    ],
)
def test_stock_code_validator_rejects_invalid_or_empty_items(stock_codes):
    with pytest.raises(TushareRtKValidationError, match="stock code"):
        validate_rt_k_stock_codes(stock_codes)


@pytest.mark.parametrize(
    "stock_codes",
    [
        "600000.SH,600000.SH",
        "600000.SH, 600000.SH",
        "600000.SH,000001.SZ,600000.SH",
    ],
)
def test_stock_code_validator_rejects_duplicates(stock_codes):
    with pytest.raises(TushareRtKValidationError, match="duplicate"):
        validate_rt_k_stock_codes(stock_codes)


def test_stock_code_validator_rejects_more_than_five_codes():
    with pytest.raises(TushareRtKValidationError, match="at most 5"):
        validate_rt_k_stock_codes(
            "600000.SH,601138.SH,000001.SZ,000333.SZ,600519.SH,601318.SH"
        )


@pytest.mark.parametrize(
    "stock_codes",
    [
        "BAD.SH",
        "600000.sh",
        "600000.SH,",
        "600000.SH,600000.SH",
    ],
)
def test_invalid_request_fails_before_secret_or_provider_access(
    stock_codes,
    monkeypatch,
):
    secret_reads = []

    def getenv(name, default=None):
        if name == "TUSHARE_TOKEN":
            secret_reads.append(name)
        return default

    monkeypatch.setattr(
        "data_provider.tushare_rt_k_provider.os.getenv",
        getenv,
    )
    client = FakeClient([pd.DataFrame([_row()])])
    provider = TushareRtKProvider(client=client)

    with pytest.raises(TushareRtKValidationError):
        provider.fetch(stock_codes)

    assert secret_reads == []
    assert client.calls == []


def test_normal_mapping_uses_close_as_latest_and_preserves_native_units():
    provider, client, _ = _provider([pd.DataFrame([_row()])])

    frame = provider.fetch("600000.SH")

    assert list(frame["code"]) == ["600000"]
    assert frame.iloc[0]["close"] == 10.2
    assert frame.iloc[0]["prev_close"] == 10.0
    assert frame.iloc[0]["pct_change"] == pytest.approx(2.0)
    assert frame.iloc[0]["volume"] == 12_345_600  # rt_k is shares
    assert frame.iloc[0]["amount"] == 125_678_900  # rt_k is yuan
    assert frame.iloc[0]["num"] == 8_888
    assert frame.iloc[0]["bid_price1"] == 10.19
    assert frame.iloc[0]["ask_volume1"] == 9_800
    assert frame.attrs["market_data_source"] == "tushare_rt_k"
    assert frame.attrs["market_data_at"] == "2026-08-20T10:00:05+08:00"
    assert frame.attrs["market_state"] == "intraday"
    assert frame.attrs["api_name"] == "rt_k"
    assert frame.attrs["row_count"] == 1
    assert client.calls[0][0] == "rt_k"
    assert client.calls[0][2] == {"ts_code": "600000.SH"}


@pytest.mark.parametrize("pre_close", [None, 0, "bad"])
def test_invalid_pre_close_is_rejected(pre_close):
    provider, _, _ = _provider([pd.DataFrame([_row(pre_close=pre_close)])])

    with pytest.raises(TushareRtKValidationError, match="no valid rows"):
        provider.fetch("600000.SH")


@pytest.mark.parametrize(
    "changes",
    [
        {"low": 10.4},
        {"high": 9.9},
        {"close": -1},
        {"open": "bad"},
    ],
)
def test_invalid_ohlc_is_rejected(changes):
    provider, _, _ = _provider([pd.DataFrame([_row(**changes)])])

    with pytest.raises(TushareRtKValidationError, match="no valid rows"):
        provider.fetch("600000.SH")


def test_duplicate_code_is_rejected():
    provider, _, _ = _provider([pd.DataFrame([_row(), _row(close=10.21)])])

    with pytest.raises(TushareRtKValidationError, match="duplicate ts_code"):
        provider.fetch("600000.SH")


def test_main_board_filter_still_applies_to_valid_exact_requests():
    provider, _, _ = _provider(
        [
            pd.DataFrame(
                [
                    _row(ts_code="600000.SH"),
                    _row(ts_code="688001.SH", name="科创测试"),
                    _row(ts_code="300001.SZ", name="创业测试"),
                    _row(ts_code="000001.SZ", name="合成样本B"),
                    _row(ts_code="002001.SZ", name="ST测试"),
                ]
            )
        ]
    )

    frame = provider.fetch(
        "600000.SH,688001.SH,300001.SZ,000001.SZ,002001.SZ"
    )

    assert set(frame["code"]) == {"600000", "000001"}


@pytest.mark.parametrize(
    ("now", "trade_time", "phase", "expected_state"),
    [
        (datetime(2026, 8, 20, 10, 0, 20, tzinfo=CN_TZ), "2026-08-20 10:00:05", "intraday", "intraday"),
        (datetime(2026, 8, 20, 12, 0, 0, tzinfo=CN_TZ), "2026-08-20 11:30:00", "lunch_break", "lunch_break"),
        (datetime(2026, 8, 20, 16, 0, 0, tzinfo=CN_TZ), "2026-08-20 15:00:00", "postmarket", "market_closed"),
    ],
)
def test_trade_time_market_states(now, trade_time, phase, expected_state):
    provider, _, _ = _provider(
        [pd.DataFrame([_row(trade_time=trade_time)])],
        now=now,
        phase=phase,
    )

    frame = provider.fetch("600000.SH")

    assert frame.attrs["market_state"] == expected_state
    assert frame.iloc[0]["market_data_at"].endswith("+08:00")


@pytest.mark.parametrize(
    ("now", "trade_time", "phase", "message"),
    [
        (datetime(2026, 8, 20, 9, 20, tzinfo=CN_TZ), "2026-08-20 09:20:00", "premarket", "premarket"),
        (datetime(2026, 8, 22, 10, 0, tzinfo=CN_TZ), "2026-08-22 10:00:00", "non_trading", "non_trading"),
        (datetime(2026, 8, 20, 10, 5, tzinfo=CN_TZ), "2026-08-20 10:00:00", "intraday", "stale"),
        (datetime(2026, 8, 20, 10, 0, tzinfo=CN_TZ), "2026-08-20 10:01:00", "intraday", "future"),
        (datetime(2026, 8, 20, 10, 0, tzinfo=CN_TZ), "2026-08-19 15:00:00", "intraday", "trade_date_mismatch"),
    ],
)
def test_invalid_market_time_fails_closed(now, trade_time, phase, message):
    provider, _, _ = _provider(
        [pd.DataFrame([_row(trade_time=trade_time)])],
        now=now,
        phase=phase,
    )

    with pytest.raises(TushareRtKValidationError, match=message):
        provider.fetch("600000.SH")


def test_missing_token_fails_without_calling_client(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    client = FakeClient([pd.DataFrame([_row()])])
    provider = TushareRtKProvider(client=client)

    with pytest.raises(TushareRtKAuthError, match="missing"):
        provider.fetch("600000.SH")

    assert client.calls == []


@pytest.mark.parametrize("message", ["权限不足", "token invalid", "认证失败"])
def test_auth_and_permission_errors_are_not_retried(message):
    provider, client, sleeps = _provider([RuntimeError(message)])

    with pytest.raises(TushareRtKAuthError):
        provider.fetch("600000.SH")

    assert len(client.calls) == 1
    assert sleeps == []


def test_provider_exception_does_not_echo_sensitive_upstream_text():
    synthetic_secret = "synthetic-sensitive-placeholder"
    provider, _, _ = _provider(
        [RuntimeError(f"HTTP 403 Authorization Bearer {synthetic_secret}")]
    )

    with pytest.raises(TushareRtKAuthError) as captured:
        provider.fetch("600000.SH")

    assert synthetic_secret not in str(captured.value)
    assert "Bearer" not in str(captured.value)


@pytest.mark.parametrize("message", ["Tushare API HTTP 429", "timeout", "Tushare API HTTP 503"])
def test_recoverable_errors_use_finite_exponential_retry(message):
    provider, client, sleeps = _provider(
        [RuntimeError(message), pd.DataFrame([_row()])]
    )

    frame = provider.fetch("600000.SH")

    assert not frame.empty
    assert len(client.calls) == 2
    assert sleeps == [1.0]


def test_same_day_cache_is_reused_but_never_crosses_trade_date():
    now_box = [datetime(2026, 8, 20, 10, 0, 20, tzinfo=CN_TZ)]
    client = FakeClient(
        [
            pd.DataFrame([_row()]),
            pd.DataFrame([_row(trade_time="2026-08-21 10:00:05", close=10.3)]),
        ]
    )
    provider = TushareRtKProvider(
        client=client,
        now=lambda: now_box[0],
        market_phase=lambda _: "intraday",
        sleep=lambda _: None,
    )

    first = provider.fetch("600000.SH")
    cached = provider.fetch("600000.SH")
    now_box[0] = datetime(2026, 8, 21, 10, 0, 20, tzinfo=CN_TZ)
    next_day = provider.fetch("600000.SH")

    assert first.attrs["cache_hit"] is False
    assert cached.attrs["cache_hit"] is True
    assert next_day.iloc[0]["close"] == 10.3
    assert len(client.calls) == 2


def test_quote_time_rollback_for_same_code_fails_closed():
    client = FakeClient(
        [
            pd.DataFrame([_row(trade_time="2026-08-20 10:00:05")]),
            pd.DataFrame([_row(trade_time="2026-08-20 10:00:04")]),
        ]
    )
    provider = TushareRtKProvider(
        client=client,
        now=lambda: datetime(2026, 8, 20, 10, 0, 20, tzinfo=CN_TZ),
        market_phase=lambda _: "intraday",
    )
    provider.fetch("600000.SH")

    with pytest.raises(TushareRtKValidationError, match="time_rollback"):
        provider.fetch("600000.SH,000001.SZ")
