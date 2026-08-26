from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import subprocess
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
