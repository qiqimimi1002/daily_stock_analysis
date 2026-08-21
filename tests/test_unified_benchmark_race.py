from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import json

import pytest

from research.benchmarks.low_volatility import MODEL_NAME as LOW_VOLATILITY_MODEL
from research.benchmarks.short_term import MODEL_NAME as SHORT_TERM_MODEL
from research.benchmarks.unified_race import (
    ROUND_TRIP_COST_PCT,
    SCHEMA_VERSION,
    UnifiedRaceError,
    evaluate_unified_race,
)


BASE_SHA = "a" * 40
UNIVERSE_HASH = "b" * 64
HASHES = {
    "calendar_content_sha256": "c" * 64,
    "raw_history_manifest_sha256": "d" * 64,
    "corporate_action_manifest_sha256": "e" * 64,
    "reference_snapshot_sha256": "f" * 64,
}
SHORT_CODES = tuple(f"60000{index}" for index in range(1, 6))
LOW_CODES = tuple(f"60000{index}" for index in range(6, 10)) + ("600010",)
UNIVERSE = SHORT_CODES + LOW_CODES
FIRST_DAY_RETURNS = {
    **{code: float(index) for index, code in enumerate(SHORT_CODES, start=1)},
    **{code: float(index) for index, code in enumerate(LOW_CODES, start=-1)},
}


def _signal(
    *,
    model_name: str,
    code: str,
    rank: int,
    signal_date: str,
    market_data_at: str,
    source_data_as_of: str,
    window_end: str,
) -> dict:
    if model_name == SHORT_TERM_MODEL:
        metric = {"ret_20": 0.1, "ret_5": 0.10 - rank * 0.01}
    else:
        metric = {"volatility_daily_60d": rank * 0.01}
    metric.update(
        {
            "universe_contract_version": "v2_1_mainboard_v1",
            "universe_config_hash": UNIVERSE_HASH,
            "window_end": window_end,
        }
    )
    return {
        "model_name": model_name,
        "stock_code": code,
        "signal_date": signal_date,
        "market_data_at": market_data_at,
        "source_data_as_of": source_data_as_of,
        "reference_price": 100.0,
        "rank": rank,
        "raw_metric": metric,
        "parameters": {"top_n": 5},
    }


def _bars(code: str, forward_dates: list[str]) -> list[dict]:
    first_return = FIRST_DAY_RETURNS[code]
    output = []
    for offset, trade_date in enumerate(forward_dates):
        return_pct = first_return + offset * 0.5
        close = 100.0 * (1.0 + return_pct / 100.0)
        output.append(
            {
                "trade_date": trade_date,
                "high": max(100.0, close + 1.0),
                "low": min(100.0, close - 1.0),
                "close": close,
            }
        )
    return output


def _batch(signal_date: date = date(2026, 8, 3)) -> dict:
    market_time = datetime(signal_date.year, signal_date.month, signal_date.day, 10, 0).astimezone()
    market_data_at = market_time.replace(tzinfo=None).isoformat() + "+08:00"
    source_data_as_of = market_time.replace(tzinfo=None, hour=9, minute=59).isoformat() + "+08:00"
    previous_completed_trade_date = (signal_date - timedelta(days=1)).isoformat()
    forward_dates = [
        (signal_date + timedelta(days=index)).isoformat() for index in range(1, 11)
    ]
    signals = {
        SHORT_TERM_MODEL: [
            _signal(
                model_name=SHORT_TERM_MODEL,
                code=code,
                rank=rank,
                signal_date=signal_date.isoformat(),
                market_data_at=market_data_at,
                source_data_as_of=source_data_as_of,
                window_end=previous_completed_trade_date,
            )
            for rank, code in enumerate(SHORT_CODES, start=1)
        ],
        LOW_VOLATILITY_MODEL: [
            _signal(
                model_name=LOW_VOLATILITY_MODEL,
                code=code,
                rank=rank,
                signal_date=signal_date.isoformat(),
                market_data_at=market_data_at,
                source_data_as_of=source_data_as_of,
                window_end=previous_completed_trade_date,
            )
            for rank, code in enumerate(LOW_CODES, start=1)
        ],
    }
    return {
        "signal_date": signal_date.isoformat(),
        "market_data_at": market_data_at,
        "source_data_as_of": source_data_as_of,
        "previous_completed_trade_date": previous_completed_trade_date,
        "universe_contract_version": "v2_1_mainboard_v1",
        "universe_config_hash": UNIVERSE_HASH,
        "shared_universe": list(UNIVERSE),
        "model_universes": {
            SHORT_TERM_MODEL: list(UNIVERSE),
            LOW_VOLATILITY_MODEL: list(UNIVERSE),
        },
        "reference_prices": {code: 100.0 for code in UNIVERSE},
        "signals": signals,
        "evidence": {
            "calendar_consistency_status": "pass",
            "price_basis": "raw_unadjusted",
            "acquisition_mode": "prospective_cutoff",
            "private_archive": True,
            "immutable_archive": True,
            "raw_history_acceptance_status": "conditional_pass",
            "corporate_action_review_status": "reviewed_clear",
            "public_payload_policy": "metadata_and_hashes_only_no_raw_rows",
            **HASHES,
        },
        "forward_trade_dates": forward_dates,
        "forward_bars": {code: _bars(code, forward_dates) for code in UNIVERSE},
        "hs300": {
            "reference_price": 100.0,
            "forward_bars": [
                {
                    "trade_date": trade_date,
                    "high": 101.5 + index * 0.5,
                    "low": 99.0,
                    "close": 100.0 + (index + 1) * 0.5,
                }
                for index, trade_date in enumerate(forward_dates)
            ],
        },
        "factor_values": {
            code: {
                "vol_contraction_10_60": FIRST_DAY_RETURNS[code],
                "breakout_strength_20": FIRST_DAY_RETURNS[code] * 2.0,
                "volume_ratio_5": -FIRST_DAY_RETURNS[code],
            }
            for code in UNIVERSE
        },
    }


def _payload(*batches: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "base_sha": BASE_SHA,
        "inventory": {
            "prospective_private_immutable_batch_count": len(batches),
            "excluded_evidence": [],
        },
        "batches": list(batches),
    }


def test_models_share_date_universe_top_n_and_cost() -> None:
    result = evaluate_unified_race(_payload(_batch()))

    short = result["models"][SHORT_TERM_MODEL]["1d"]
    low = result["models"][LOW_VOLATILITY_MODEL]["1d"]
    assert short["sample_count"] == low["sample_count"] == 5
    assert short["signal_date_count"] == low["signal_date_count"] == 1
    assert short["gross_return_mean_pct"] == 3.0
    assert short["net_return_mean_pct"] == 3.0 - ROUND_TRIP_COST_PCT
    assert low["gross_return_mean_pct"] == 1.0
    assert result["cost_bps"] == 30
    assert result["comparison"]["1d"]["common_evaluable_signal_samples_per_model"] == 5


def test_horizon_mapping_uses_exact_future_session_count() -> None:
    result = evaluate_unified_race(_payload(_batch()))
    short = result["models"][SHORT_TERM_MODEL]

    assert short["1d"]["gross_return_mean_pct"] == 3.0
    assert short["3d"]["gross_return_mean_pct"] == 4.0
    assert short["5d"]["gross_return_mean_pct"] == 5.0
    assert short["10d"]["gross_return_mean_pct"] == 7.5
    assert result["pending_horizons"] == {
        "20d": "merged Benchmark 20d execution chain is unavailable"
    }


def test_missing_selected_bar_excludes_both_models_for_that_horizon() -> None:
    batch = _batch()
    batch["forward_bars"][SHORT_CODES[0]] = batch["forward_bars"][SHORT_CODES[0]][:5]

    result = evaluate_unified_race(_payload(batch))

    assert result["models"][SHORT_TERM_MODEL]["5d"]["sample_count"] == 5
    assert result["models"][LOW_VOLATILITY_MODEL]["5d"]["sample_count"] == 5
    assert result["models"][SHORT_TERM_MODEL]["10d"]["sample_count"] == 0
    assert result["models"][LOW_VOLATILITY_MODEL]["10d"]["sample_count"] == 0
    assert result["exclusions"]["reason_counts"] == {
        "incomplete_shared_forward_window": 1
    }


def test_different_model_universe_fails_closed() -> None:
    batch = _batch()
    batch["model_universes"][SHORT_TERM_MODEL] = list(UNIVERSE[:-1])

    with pytest.raises(UnifiedRaceError, match="exact shared Universe"):
        evaluate_unified_race(_payload(batch))


def test_signal_timestamp_and_future_dates_enforce_no_lookahead() -> None:
    batch = _batch()
    batch["source_data_as_of"] = "2026-08-03T10:01:00+08:00"
    with pytest.raises(UnifiedRaceError, match="no-lookahead"):
        evaluate_unified_race(_payload(batch))

    batch = _batch()
    batch["forward_trade_dates"][0] = batch["signal_date"]
    with pytest.raises(UnifiedRaceError, match="strictly after signal_date"):
        evaluate_unified_race(_payload(batch))


def test_both_models_must_use_the_shared_t_minus_one_cutoff() -> None:
    batch = _batch()
    batch["previous_completed_trade_date"] = batch["signal_date"]
    with pytest.raises(UnifiedRaceError, match="strictly earlier"):
        evaluate_unified_race(_payload(batch))

    batch = _batch()
    batch["signals"][LOW_VOLATILITY_MODEL][0]["raw_metric"]["window_end"] = (
        date.fromisoformat(batch["previous_completed_trade_date"])
        - timedelta(days=1)
    ).isoformat()
    with pytest.raises(UnifiedRaceError, match="shared T-1 cutoff"):
        evaluate_unified_race(_payload(batch))


def test_signal_input_order_is_canonical_and_idempotent() -> None:
    original = _payload(_batch())
    shuffled = deepcopy(original)
    for model in (SHORT_TERM_MODEL, LOW_VOLATILITY_MODEL):
        shuffled["batches"][0]["signals"][model].reverse()

    first = evaluate_unified_race(original)
    second = evaluate_unified_race(shuffled)

    assert first["models"] == second["models"]
    assert first["comparison"] == second["comparison"]
    assert evaluate_unified_race(original) == first
    assert first["output_content_sha256"] == evaluate_unified_race(original)[
        "output_content_sha256"
    ]


def test_frozen_tie_break_is_validated() -> None:
    batch = _batch()
    first, second = batch["signals"][SHORT_TERM_MODEL][:2]
    first["raw_metric"]["ret_5"] = 0.05
    second["raw_metric"]["ret_5"] = 0.05
    first["stock_code"], second["stock_code"] = second["stock_code"], first["stock_code"]

    with pytest.raises(UnifiedRaceError, match="tie-break"):
        evaluate_unified_race(_payload(batch))


def test_nonprospective_evidence_excludes_date_without_fallback() -> None:
    batch = _batch()
    batch["evidence"]["acquisition_mode"] = "backfill_current_snapshot"

    result = evaluate_unified_race(_payload(batch))

    assert result["evidence_status"] == "insufficient_evidence"
    assert result["models"][SHORT_TERM_MODEL]["1d"]["sample_count"] == 0
    assert result["models"][LOW_VOLATILITY_MODEL]["1d"]["sample_count"] == 0
    assert result["exclusions"]["reason_counts"] == {"not_prospective_cutoff": 1}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("price_basis", "qfq", "price_basis_not_raw_unadjusted"),
        (
            "corporate_action_review_status",
            "review_required",
            "corporate_action_review_not_clear",
        ),
    ],
)
def test_price_and_corporate_action_gates_exclude_both_models(
    field: str,
    value: str,
    reason: str,
) -> None:
    batch = _batch()
    batch["evidence"][field] = value

    result = evaluate_unified_race(_payload(batch))

    for model in (SHORT_TERM_MODEL, LOW_VOLATILITY_MODEL):
        assert result["models"][model]["1d"]["sample_count"] == 0
    assert result["exclusions"]["reason_counts"] == {reason: 1}


def test_ablation_factors_are_diagnostic_only() -> None:
    positive = evaluate_unified_race(_payload(_batch()))
    inverted_payload = _payload(_batch())
    for values in inverted_payload["batches"][0]["factor_values"].values():
        for name in values:
            values[name] *= -1.0
    inverted = evaluate_unified_race(inverted_payload)

    assert positive["models"] == inverted["models"]
    assert positive["comparison"] == inverted["comparison"]
    assert (
        positive["ablation_factors"]["breakout_strength_20"]["1d"][
            "global_spearman_ic"
        ]
        == 1.0
    )
    assert (
        inverted["ablation_factors"]["breakout_strength_20"]["1d"][
            "global_spearman_ic"
        ]
        == -1.0
    )


def test_date_group_stability_and_drawdown_are_reported() -> None:
    first = _batch(date(2026, 8, 3))
    second = _batch(date(2026, 8, 20))
    for bars in second["forward_bars"].values():
        for bar in bars:
            bar["close"] = 98.0
            bar["high"] = 100.0
            bar["low"] = 97.0

    result = evaluate_unified_race(_payload(first, second))
    short = result["models"][SHORT_TERM_MODEL]["1d"]

    assert short["signal_date_count"] == 2
    assert short["date_stability"]["positive_date_count"] == 1
    assert short["date_stability"]["negative_date_count"] == 1
    assert short["max_drawdown_pct"] > 0
    assert [item["signal_date"] for item in short["per_date"]] == [
        "2026-08-03",
        "2026-08-20",
    ]


def test_empty_real_inventory_is_explicit_insufficient_evidence() -> None:
    payload = _payload()
    payload["inventory"] = {
        "prospective_private_immutable_batch_count": 0,
        "excluded_evidence": [
            {
                "sample_count": 4,
                "reason_codes": [
                    "backfill_current_snapshot",
                    "raw_rows_not_persisted",
                ],
                "content_sha256": "1" * 64,
            }
        ],
    }

    result = evaluate_unified_race(payload)

    assert result["conclusion_class"] == "表现接近/证据不足"
    assert result["minimum_common_dates_for_evidence"] == 20
    assert result["signal_date_range"] == {"start": None, "end": None}
    assert result["candidate_signal_count"] == {
        LOW_VOLATILITY_MODEL: 0,
        SHORT_TERM_MODEL: 0,
    }
    assert result["inventory"]["excluded_evidence_sample_count"] == 4
    serialized = json.dumps(result, sort_keys=True)
    assert "forward_bars" not in serialized
    assert "reference_prices" not in serialized


def test_duplicate_signal_date_is_rejected() -> None:
    batch = _batch()
    with pytest.raises(UnifiedRaceError, match="repeat a signal_date"):
        evaluate_unified_race(_payload(batch, deepcopy(batch)))
