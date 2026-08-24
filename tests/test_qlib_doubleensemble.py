from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from research.benchmarks.qlib_doubleensemble import (
    EXPECTED_ALPHA158_FEATURE_COUNT,
    OFFICIAL_MODEL_KWARGS,
    QlibDoubleEnsembleError,
    TimeSplits,
    alpha158_feature_names,
    baseline_batches,
    build_candidate_batches,
    build_qlib_provider,
    evaluate_out_of_sample,
    fit_and_predict,
    model_config_payload,
    normalize_raw_frame,
)


def _raw_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-08-21",
                "code": "sh.600000",
                "name": "浦发银行",
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10.5",
                "preclose": "10",
                "volume": "100",
                "amount": "1050",
                "price_basis": "raw_unadjusted",
                "source_id": "baostock.query_history_k_data_plus.adjustflag_3",
                "turn": "1.2",
                "pctChg": "5",
                "tradestatus": "1",
                "isST": "0",
            }
        ]
    )


def test_time_segments_must_be_chronological_and_disjoint():
    with pytest.raises(QlibDoubleEnsembleError, match="non-overlapping"):
        TimeSplits.create(
            train_start="2020-01-01",
            train_end="2022-12-31",
            valid_start="2022-12-31",
            valid_end="2023-12-31",
            test_start="2024-01-01",
            test_end="2025-12-31",
        )


def test_raw_adapter_derives_vwap_without_adjusting_prices():
    normalized = normalize_raw_frame(_raw_frame())
    assert normalized.loc[0, "code"] == "SH600000"
    assert normalized.loc[0, "close"] == 10.5
    assert normalized.loc[0, "vwap"] == 10.5
    assert normalized.loc[0, "factor"] == 1.0


def test_raw_adapter_fails_closed_when_alpha158_input_is_missing():
    with pytest.raises(QlibDoubleEnsembleError, match="amount"):
        normalize_raw_frame(_raw_frame().drop(columns=["amount"]))


def test_pytdx_turnover_can_be_missing_but_is_not_invented():
    frame = _raw_frame()
    frame.loc[0, "source_id"] = "pytdx.get_security_bars.raw"
    frame.loc[0, "turn"] = None
    normalized = normalize_raw_frame(frame)
    assert pd.isna(normalized.loc[0, "turn"])


def test_model_config_is_the_official_alpha158_workflow_config():
    assert OFFICIAL_MODEL_KWARGS["num_models"] == 3
    assert OFFICIAL_MODEL_KWARGS["epochs"] == 28
    assert OFFICIAL_MODEL_KWARGS["sample_ratios"] == [0.8, 0.7, 0.6, 0.5, 0.4]
    assert model_config_payload()["alpha158_feature_count"] == 158


def test_installed_official_alpha158_has_all_158_features():
    pytest.importorskip("qlib")
    names = alpha158_feature_names()
    assert len(names) == EXPECTED_ALPHA158_FEATURE_COUNT
    assert len(set(names)) == EXPECTED_ALPHA158_FEATURE_COUNT


def test_candidate_scores_are_shifted_from_t_minus_one_and_ranked_stably(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "names.json").write_text(
        json.dumps({f"60000{i}": f"股票{i}" for i in range(6)}, ensure_ascii=False),
        encoding="utf-8",
    )
    index = pd.MultiIndex.from_tuples(
        [(pd.Timestamp("2026-08-21"), f"SH60000{i}") for i in range(6)],
        names=["datetime", "instrument"],
    )
    predictions = pd.Series([1.0, 2.0, 2.0, 4.0, 3.0, 0.0], index=index)
    batches = build_candidate_batches(
        predictions=predictions,
        provider_uri=tmp_path,
        calendar=["2026-08-21", "2026-08-25"],
    )
    assert batches[0]["trade_date"] == "2026-08-25"
    assert batches[0]["data_cutoff_date"] == "2026-08-21"
    assert [item["stock_code"] for item in batches[0]["candidates"]] == [
        "600003",
        "600004",
        "600001",
        "600002",
        "600000",
    ]


def test_fewer_than_three_reliable_scores_produces_zero_candidates(tmp_path):
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "names.json").write_text("{}", encoding="utf-8")
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-08-21"), "SH600000"),
            (pd.Timestamp("2026-08-21"), "SZ000001"),
        ],
        names=["datetime", "instrument"],
    )
    batches = build_candidate_batches(
        predictions=pd.Series([1.0, 0.5], index=index),
        provider_uri=tmp_path,
        calendar=["2026-08-21", "2026-08-25"],
    )
    assert batches[0]["candidate_count"] == 0
    assert batches[0]["data_status"] == "insufficient_reliable_candidates"


def test_v21_baseline_reuses_existing_candidate_score(tmp_path):
    pytest.importorskip("pyarrow")
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    rows = []
    for offset, code in enumerate(("600000", "600001", "600002", "600003", "600004", "600005")):
        rows.append(
            {
                "datetime": pd.Timestamp("2026-08-21"),
                "instrument": f"SH{code}",
                "stock_code": code,
                "stock_name": f"股票{offset}",
                "close": 10.0 + offset * 0.1,
                "preclose": 9.9 + offset * 0.1,
                "pctChg": 1.0,
                "volume": 30_000_000.0,
                "amount": 300_000_000.0,
                "turn": 2.0,
                "history_close": 10.0 + offset * 0.1,
                "five_day_pct": 3.0,
                "ma5": 9.8,
                "ma10": 9.5,
                "ma20": 9.0,
                "volume_ratio_5d": 1.2,
                "avg_amount_20d": 300_000_000.0,
                "is_intraday": False,
                "history_data_through": "2026-08-21",
            }
        )
    pd.DataFrame(rows).to_parquet(metadata / "eligible_rows.parquet", index=False)
    batches = baseline_batches(
        provider_uri=tmp_path,
        candidate_batches=[
            {"trade_date": "2026-08-24", "data_cutoff_date": "2026-08-21"}
        ],
    )
    assert len(batches) == 1
    assert len(batches[0]["candidates"]) == 5


def test_oos_adapter_uses_existing_four_horizon_engine(tmp_path):
    symbols = tmp_path / "symbols"
    symbols.mkdir()
    dates = pd.bdate_range("2026-07-01", periods=15)

    def history(code, name, start):
        close = pd.Series(start + np.arange(len(dates)) * 0.1)
        return pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "code": code,
                "name": name,
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "preclose": close.shift(1).fillna(close.iloc[0]),
                "volume": 30_000_000,
                "amount": close * 30_000_000,
                "price_basis": "raw_unadjusted",
                "source_id": "baostock.query_history_k_data_plus.adjustflag_3",
                "turn": 1.0,
                "pctChg": close.pct_change().fillna(0.0) * 100.0,
                "tradestatus": "1",
                "isST": "0",
            }
        )

    pd.DataFrame(
        {"calendar_date": dates.strftime("%Y-%m-%d"), "is_trading_day": "1"}
    ).to_csv(tmp_path / "calendar.csv", index=False)
    history("sh.600000", "浦发银行", 10.0).to_csv(
        symbols / "600000.csv", index=False
    )
    history("sh.000300", "沪深300", 100.0).to_csv(
        tmp_path / "benchmark_sh000300.csv", index=False
    )
    batch = {
        "trade_date": dates[1].strftime("%Y-%m-%d"),
        "candidates": [{"stock_code": "600000"}],
    }
    result = evaluate_out_of_sample(
        source_dir=tmp_path,
        doubleensemble_batches=[batch],
        v21_batches=[batch],
    )
    assert set(result["models"]["doubleensemble"]) == {"1d", "3d", "5d", "10d"}
    assert result["models"]["doubleensemble"]["10d"]["sample_count"] == 1


def test_private_raw_files_build_official_qlib_storage(tmp_path):
    pytest.importorskip("qlib")
    source = tmp_path / "source"
    symbols = source / "symbols"
    symbols.mkdir(parents=True)
    dates = pd.bdate_range("2025-01-02", periods=80)
    pd.DataFrame(
        {
            "calendar_date": dates.strftime("%Y-%m-%d"),
            "is_trading_day": "1",
        }
    ).to_csv(source / "calendar.csv", index=False)
    for offset, code in enumerate(("600000", "600001", "600002")):
        close = pd.Series(range(len(dates)), dtype=float) * 0.01 + 10 + offset
        frame = pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "code": f"sh.{code}",
                "name": f"股票{offset}",
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "preclose": close.shift(1).fillna(close.iloc[0]),
                "volume": 30_000_000,
                "amount": close * 30_000_000,
                "price_basis": "raw_unadjusted",
                "source_id": "baostock.query_history_k_data_plus.adjustflag_3",
                "turn": "1.0",
                "pctChg": "0.1",
                "tradestatus": "1",
                "isST": "0",
            }
        )
        frame.to_csv(symbols / f"{code}.csv", index=False)
    manifest = build_qlib_provider(
        source_dir=source,
        provider_uri=tmp_path / "provider",
    )
    assert manifest["instrument_count"] == 3
    assert (tmp_path / "provider" / "calendars" / "day.txt").is_file()
    assert (tmp_path / "provider" / "instruments" / "all.txt").is_file()
    assert (tmp_path / "provider" / "features" / "sh600000" / "vwap.day.bin").is_file()


@pytest.mark.integration
def test_official_alpha158_doubleensemble_smoke(tmp_path):
    pytest.importorskip("qlib")
    source = tmp_path / "source"
    symbols = source / "symbols"
    symbols.mkdir(parents=True)
    dates = pd.bdate_range("2022-01-04", periods=430)
    pd.DataFrame(
        {
            "calendar_date": dates.strftime("%Y-%m-%d"),
            "is_trading_day": "1",
        }
    ).to_csv(source / "calendar.csv", index=False)
    for offset, code in enumerate(
        ("600000", "600001", "600002", "600003", "600004", "600005")
    ):
        values = np.arange(len(dates), dtype=float)
        close = 10 + offset + values * 0.004 + np.sin(values / (7 + offset)) * 0.12
        pct = pd.Series(close).pct_change().fillna(0.0) * 100.0
        frame = pd.DataFrame(
            {
                "date": dates.strftime("%Y-%m-%d"),
                "code": f"sh.{code}",
                "name": f"股票{offset}",
                "open": close - 0.03,
                "high": close + 0.15,
                "low": close - 0.15,
                "close": close,
                "preclose": pd.Series(close).shift(1).fillna(close[0]),
                "volume": 30_000_000 + (values % 10) * 100_000,
                "amount": close * (30_000_000 + (values % 10) * 100_000),
                "price_basis": "raw_unadjusted",
                "source_id": "baostock.query_history_k_data_plus.adjustflag_3",
                "turn": "1.0",
                "pctChg": pct,
                "tradestatus": "1",
                "isST": "0",
            }
        )
        frame.to_csv(symbols / f"{code}.csv", index=False)
    provider = tmp_path / "provider"
    build_qlib_provider(source_dir=source, provider_uri=provider)
    splits = TimeSplits.create(
        train_start=dates[65].strftime("%Y-%m-%d"),
        train_end=dates[247].strftime("%Y-%m-%d"),
        valid_start=dates[250].strftime("%Y-%m-%d"),
        valid_end=dates[327].strftime("%Y-%m-%d"),
        test_start=dates[330].strftime("%Y-%m-%d"),
        test_end=dates[410].strftime("%Y-%m-%d"),
    )
    predictions, manifest = fit_and_predict(provider_uri=provider, splits=splits)
    assert len(predictions) > 0
    assert manifest["alpha158_complete"] is True
    assert manifest["alpha158_feature_count"] == 158
