from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import research.benchmarks.qlib_daily as daily


CN_TZ = ZoneInfo("Asia/Shanghai")
TARGET = date(2026, 1, 30)
NEXT_SESSION = date(2026, 2, 2)


def _source(tmp_path: Path, *, codes: tuple[str, ...] = ("600000",)) -> Path:
    source = tmp_path / f"raw-through-{TARGET:%Y%m%d}"
    symbols = source / "symbols"
    symbols.mkdir(parents=True)
    dates = [TARGET - timedelta(days=offset) for offset in range(29, -1, -1)]
    for code_index, code in enumerate(codes):
        rows = []
        previous = 9.9 + code_index
        for index, trade_date in enumerate(dates):
            close = 10.0 + code_index + index / 10.0
            rows.append(
                {
                    "date": trade_date.isoformat(),
                    "code": f"sh.{code}",
                    "name": f"股票{code_index}",
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "preclose": previous,
                    "volume": 1000 + index,
                    "amount": (1000 + index) * close,
                    "price_basis": "raw_unadjusted",
                    "source_id": daily.RAW_SOURCE_ID,
                    "turn": 1.0,
                    "pctChg": (close / previous - 1.0) * 100.0,
                    "tradestatus": "1",
                    "isST": "0",
                }
            )
            previous = close
        pd.DataFrame(rows).to_csv(symbols / f"{code}.csv", index=False)
    calendar_rows = [
        {"calendar_date": item.isoformat(), "is_trading_day": "1"}
        for item in dates + [NEXT_SESSION]
    ]
    pd.DataFrame(calendar_rows).to_csv(source / "calendar.csv", index=False)
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "collection_end": TARGET.isoformat(),
                "completed_symbol_file_count": len(codes),
                "failure_count": 0,
                "failures": [],
                "status": "complete",
            }
        ),
        encoding="utf-8",
    )
    return source


def _provider(tmp_path: Path, source_info: dict) -> Path:
    provider = tmp_path / "provider"
    (provider / "calendars").mkdir(parents=True)
    (provider / "metadata").mkdir()
    (provider / "calendars" / "day.txt").write_text(
        f"{TARGET.isoformat()}\n{NEXT_SESSION.isoformat()}\n", encoding="utf-8"
    )
    (provider / "metadata" / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_sha256": "a" * 64,
                "source_symbol_file_count": source_info["symbol_file_count"],
            }
        ),
        encoding="utf-8",
    )
    rows = []
    source = Path(source_info["source_dir"])
    for path in sorted((source / "symbols").glob("*.csv")):
        row = pd.read_csv(path).iloc[-1]
        rows.append(
            {
                "datetime": pd.Timestamp(TARGET),
                "stock_code": path.stem,
                "stock_name": row["name"],
                "close": row["close"],
                "preclose": row["preclose"],
                "volume": row["volume"],
                "amount": row["amount"],
                "turn": row["turn"],
                "pctChg": row["pctChg"],
            }
        )
    pd.DataFrame(rows).to_csv(
        provider / "metadata" / "eligible_rows.parquet", index=False
    )
    return provider


def _shadow_result(codes: tuple[str, ...]) -> tuple[dict, dict]:
    candidates = [
        {
            "code": code,
            "data_as_of": TARGET.isoformat(),
            "generated_at": "2026-01-30T17:00:00+08:00",
            "model_version": daily.FROZEN_MODEL_VERSION,
            "name": f"股票{index}",
            "rank": index + 1,
            "score": 1.0 - index / 10,
            "status": "ok",
            "trade_date": NEXT_SESSION.isoformat(),
        }
        for index, code in enumerate(codes)
    ]
    result = {
        "candidate_count": 5,
        "candidates": candidates,
        "data_as_of": TARGET.isoformat(),
        "generated_at": "2026-01-30T17:00:00+08:00",
        "model_artifact_manifest_sha256": "b" * 64,
        "model_version": daily.FROZEN_MODEL_VERSION,
        "provider_content_sha256": "c" * 64,
        "run_sha256": "d" * 64,
        "status": "ok",
        "trade_date": NEXT_SESSION.isoformat(),
    }
    return result, {"manifest_sha256": "e" * 64}


def test_source_and_provider_require_exact_completed_date_and_coverage(
    tmp_path, monkeypatch
):
    source = _source(tmp_path)
    source_info = daily.inspect_completed_source(source, TARGET)
    provider = _provider(tmp_path, source_info)
    monkeypatch.setattr(daily.pd, "read_parquet", pd.read_csv)

    inspected = daily.inspect_prepared_provider(
        provider, completed_date=TARGET, source_info=source_info
    )

    assert source_info["symbol_file_count"] == 1
    assert source_info["target_row_coverage_count"] == 1
    assert source_info["eligible_target_count"] == 1
    assert inspected["max_complete_trading_date"] == TARGET.isoformat()
    assert inspected["next_trading_date"] == NEXT_SESSION.isoformat()


def test_provider_content_mismatch_fails_closed(tmp_path, monkeypatch):
    source = _source(tmp_path)
    source_info = daily.inspect_completed_source(source, TARGET)
    provider = _provider(tmp_path, source_info)
    eligible_path = provider / "metadata" / "eligible_rows.parquet"
    monkeypatch.setattr(daily.pd, "read_parquet", pd.read_csv)
    frame = pd.read_csv(eligible_path)
    frame.loc[0, "close"] += 1.0
    frame.to_csv(eligible_path, index=False)

    with pytest.raises(daily.QlibDailyError, match="content differs"):
        daily.inspect_prepared_provider(
            provider, completed_date=TARGET, source_info=source_info
        )


def test_existing_today_source_is_rejected_before_1630(tmp_path):
    _source(tmp_path)
    with pytest.raises(daily.QlibDailyError, match="16:30"):
        daily.refresh_completed_source(
            runtime_root=tmp_path,
            completed_date=TARGET,
            observed_at=datetime(2026, 1, 30, 15, 59, tzinfo=CN_TZ),
        )


def _baostock_row(symbol: str, target: date) -> dict[str, str]:
    return {
        "date": target.isoformat(),
        "code": symbol,
        "open": "13.0",
        "high": "13.2",
        "low": "12.8",
        "close": "13.1",
        "preclose": "12.9",
        "volume": "1200",
        "amount": "15720",
        "adjustflag": "3",
        "turn": "1.1",
        "pctChg": "1.55",
        "tradestatus": "1",
        "isST": "0",
    }


class _FakeResult:
    def __init__(self, row=None, *, error_code="0"):
        self.error_code = error_code
        self.fields = list(daily.RAW_FIELDS)
        self._row = row
        self._used = False

    def next(self):
        return self._row is not None and not self._used

    def get_row_data(self):
        self._used = True
        return [self._row[field] for field in daily.RAW_FIELDS]


class _FakeSocket:
    def __init__(self):
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


def test_baostock_socket_guard_turns_peer_eof_into_bounded_failure():
    raw = SimpleNamespace(recv=lambda *_args: b"")

    with pytest.raises(ConnectionError, match="closed"):
        daily._BaostockSocketGuard(raw).recv(8192)


class _FakeBaostock:
    def __init__(self, context, results):
        self.context = context
        self.results = list(results)
        self.calls = 0
        self.sockets = []

    def login(self):
        active = _FakeSocket()
        self.sockets.append(active)
        self.context.default_socket = active
        return SimpleNamespace(error_code="0")

    def logout(self):
        return SimpleNamespace(error_code="0")

    def query_history_k_data_plus(self, *_args, **_kwargs):
        self.calls += 1
        return self.results.pop(0)


def test_baostock_request_has_timeout_retry_and_attempt_limit():
    symbol = "sh.600000"
    context = SimpleNamespace(default_socket=None)
    provider = _FakeBaostock(
        context,
        [
            _FakeResult(error_code="100"),
            _FakeResult(_baostock_row(symbol, NEXT_SESSION)),
        ],
    )
    completed = []
    events = []

    result = daily._baostock_rows(
        [symbol],
        NEXT_SESSION,
        on_success=lambda *values: completed.append(values),
        event_sink=events.append,
        request_timeout_seconds=3.5,
        max_attempts=2,
        max_failures=1,
        baostock_module=provider,
        baostock_context=context,
    )

    assert provider.calls == 2
    assert result["retry_count"] == 1
    assert completed[0][0] == symbol
    assert completed[0][2] == 2
    assert all(item.timeout == 3.5 for item in provider.sockets)
    assert [event["event"] for event in events] == ["stock_retry"]


def test_baostock_failure_limit_fails_closed_after_bounded_attempts():
    context = SimpleNamespace(default_socket=None)
    provider = _FakeBaostock(
        context,
        [_FakeResult(error_code="100"), _FakeResult(error_code="100")],
    )

    with pytest.raises(daily.QlibDailyError, match="failure_count=1"):
        daily._baostock_rows(
            ["sh.600000", "sh.600001"],
            NEXT_SESSION,
            on_success=lambda *_args: None,
            request_timeout_seconds=1,
            max_attempts=2,
            max_failures=1,
            baostock_module=provider,
            baostock_context=context,
        )

    assert provider.calls == 2


def test_refresh_staging_resumes_verified_symbols_without_full_snapshot(tmp_path, monkeypatch):
    codes = ("600000", "600001")
    _source(tmp_path, codes=codes)
    target = NEXT_SESSION
    calendar = pd.DataFrame(
        [{"calendar_date": target.isoformat(), "is_trading_day": "1"}]
    )
    monkeypatch.setattr(
        daily,
        "_verified_calendar_rows",
        lambda _target: (calendar, {"status": "verified"}),
    )
    first_calls = []

    def interrupted(symbols, _target, *, on_success, **_kwargs):
        first_calls.extend(symbols)
        first = symbols[0]
        on_success(first, _baostock_row(first, target), 1, 0.1)
        raise daily.QlibDailyError("synthetic interruption")

    monkeypatch.setattr(daily, "_baostock_rows", interrupted)
    with pytest.raises(daily.QlibDailyError, match="synthetic interruption"):
        daily.refresh_completed_source(
            runtime_root=tmp_path,
            completed_date=target,
            observed_at=datetime(2026, 2, 2, 17, 0, tzinfo=CN_TZ),
        )

    staging = tmp_path / ".raw-through-20260202-staging"
    assert staging.is_dir()
    assert not (tmp_path / "raw-through-20260202").exists()
    assert pd.read_csv(staging / "symbols" / "600000.csv").iloc[-1]["date"] == target.isoformat()
    resumed_calls = []

    def resumed(symbols, _target, *, on_success, **_kwargs):
        resumed_calls.extend(symbols)
        for symbol in symbols:
            on_success(symbol, _baostock_row(symbol, target), 1, 0.1)
        return {"completed_count": len(symbols), "failure_count": 0, "retry_count": 0}

    monkeypatch.setattr(daily, "_baostock_rows", resumed)
    result = daily.refresh_completed_source(
        runtime_root=tmp_path,
        completed_date=target,
        observed_at=datetime(2026, 2, 2, 17, 1, tzinfo=CN_TZ),
    )

    assert first_calls == ["sh.600000", "sh.600001"]
    assert resumed_calls == ["sh.600001"]
    assert result["operation_status"] == "resumed"
    assert result["resumed_symbol_count"] == 1
    assert result["target_row_coverage_count"] == 2
    assert result["source_dir"] == str((tmp_path / "raw-through-20260202").resolve())
    assert (tmp_path / "raw-through-20260202").is_dir()


def test_technical_context_uses_completed_rows_only(tmp_path):
    source = _source(tmp_path)
    context = daily.candidate_technical_context(
        source_dir=source,
        candidates=[{"code": "600000"}],
        data_as_of=TARGET,
    )[0]

    assert context["close"] == pytest.approx(12.9)
    assert context["prev_close"] == pytest.approx(12.8)
    assert context["ma5"] == pytest.approx(12.7)
    assert context["ma10"] == pytest.approx(12.45)
    assert context["ma20"] == pytest.approx(11.95)
    assert context["atr14"] == pytest.approx(0.4)
    assert context["high_20d"] == pytest.approx(13.1)
    assert context["low_20d"] == pytest.approx(10.8)


def test_nightly_archive_is_immutable_and_morning_only_dispatches_codes(tmp_path):
    codes = ("600000", "600001", "600002", "600003", "600004")
    source = _source(tmp_path, codes=codes)
    result, shadow_manifest = _shadow_result(codes)
    first = daily.archive_nightly_ready(
        source_dir=source,
        ready_root=tmp_path / "ready",
        shadow_result=result,
        shadow_manifest=shadow_manifest,
        preparation={"failure_count": 0},
        model_identity={"fit_count": 1, "model_file_sha256": "f" * 64},
    )
    second = daily.archive_nightly_ready(
        source_dir=source,
        ready_root=tmp_path / "ready",
        shadow_result=result,
        shadow_manifest=shadow_manifest,
        preparation={"failure_count": 0},
        model_identity={"fit_count": 1, "model_file_sha256": "f" * 64},
    )
    calls = []

    morning = daily.dispatch_morning_quotes(
        ready_root=tmp_path / "ready",
        trade_date=NEXT_SESSION,
        observed_at=datetime(2026, 2, 2, 9, 20, tzinfo=CN_TZ),
        runner=lambda command, check: calls.append((command, check)),
    )

    assert first["operation_status"] == "created"
    assert second["operation_status"] == "exists"
    assert morning["qlib_ran"] is False
    assert morning["shadow_reordered"] is False
    assert morning["codes"] == [f"{code}.SH" for code in codes]
    assert calls[0][1] is True
    assert "workflow" in calls[0][0]
    assert f"stock_codes={','.join(morning['codes'])}" in calls[0][0]


def test_morning_missing_same_day_archive_says_prepare_first(tmp_path):
    with pytest.raises(daily.QlibDailyError, match="先运行收盘后 prepare"):
        daily.dispatch_morning_quotes(
            ready_root=tmp_path / "missing",
            trade_date=NEXT_SESSION,
            dry_run=True,
        )


def test_private_dispatch_error_fails_closed_without_quote_claim(tmp_path):
    codes = ("600000", "600001", "600002", "600003", "600004")
    source = _source(tmp_path, codes=codes)
    result, shadow_manifest = _shadow_result(codes)
    daily.archive_nightly_ready(
        source_dir=source,
        ready_root=tmp_path / "ready",
        shadow_result=result,
        shadow_manifest=shadow_manifest,
        preparation={"failure_count": 0},
        model_identity={"fit_count": 1, "model_file_sha256": "f" * 64},
    )

    def failed(command, check):
        raise subprocess.CalledProcessError(1, command)

    with pytest.raises(daily.QlibDailyError, match="no quote confirmation"):
        daily.dispatch_morning_quotes(
            ready_root=tmp_path / "ready",
            trade_date=NEXT_SESSION,
            observed_at=datetime(2026, 2, 2, 9, 20, tzinfo=CN_TZ),
            runner=failed,
        )
