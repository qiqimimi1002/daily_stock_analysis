"""Research-only Microsoft Qlib Alpha158 + DoubleEnsemble integration.

Qlib is Copyright (c) Microsoft Corporation and licensed under the MIT
License.  The model and feature implementations are imported from Qlib; this
module only validates project data, writes Qlib's file-provider format, and
adapts ranked scores to the existing research contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.benchmarks.schema import canonical_json_bytes
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
)
from src.services.market_screener import (
    MAIN_BOARD_PREFIXES,
    ScreeningConfig,
    apply_spot_filters,
    build_candidate,
    is_excluded_name,
    is_main_board_code,
)


QLIB_PACKAGE_VERSION = "0.9.7"
MODEL_CONFIG_VERSION = "qlib-0.9.7-alpha158-doubleensemble-official-v1"
MODEL_NAME = "Microsoft Qlib Alpha158 + DoubleEnsemble"
MODEL_FAMILY = "doubleensemble"
MODEL_UNIVERSE_CONTRACT_VERSION = "mainboard_non_st_active_v1"
EXPECTED_ALPHA158_FEATURE_COUNT = 158
RANDOM_SEED = 0
TOP_N = 5
MIN_RELIABLE_CANDIDATES = 3
ROUND_TRIP_COST_BPS = 30
LABEL_LOOKAHEAD_SESSIONS = 2
EVIDENCE_STATUS = "INSUFFICIENT EVIDENCE"
EVIDENCE_STATUS_REASONS = (
    "current_active_universe_survivorship",
    "historical_constituent_name_vintage_unavailable",
    "full_history_not_dual_source_immutable",
    "pytdx_historical_turnover_unavailable_for_v21_baseline",
)

# Exact model kwargs from Qlib's official
# examples/benchmarks/DoubleEnsemble/workflow_config_doubleensemble_Alpha158.yaml.
OFFICIAL_MODEL_KWARGS: Mapping[str, Any] = {
    "base_model": "gbm",
    "loss": "mse",
    "num_models": 3,
    "enable_sr": True,
    "enable_fs": True,
    "alpha1": 1,
    "alpha2": 1,
    "bins_sr": 10,
    "bins_fs": 5,
    "decay": 0.5,
    "sample_ratios": [0.8, 0.7, 0.6, 0.5, 0.4],
    "sub_weights": [1, 1, 1],
    "epochs": 28,
    "colsample_bytree": 0.8879,
    "learning_rate": 0.2,
    "subsample": 0.8789,
    "lambda_l1": 205.6999,
    "lambda_l2": 580.9768,
    "max_depth": 8,
    "num_leaves": 210,
    "num_threads": 20,
    "verbosity": -1,
}

RAW_REQUIRED_COLUMNS = (
    "date",
    "code",
    "name",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "price_basis",
    "source_id",
    "turn",
    "pctChg",
    "tradestatus",
    "isST",
)
QLIB_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "factor")


class QlibDoubleEnsembleError(ValueError):
    """Raised when research input could leak or cannot satisfy Qlib."""


@dataclass(frozen=True)
class TimeSplits:
    train_start: date
    train_end: date
    valid_start: date
    valid_end: date
    test_start: date
    test_end: date

    @classmethod
    def create(
        cls,
        *,
        train_start: str,
        train_end: str,
        valid_start: str,
        valid_end: str,
        test_start: str,
        test_end: str,
    ) -> "TimeSplits":
        values = {
            key: _canonical_date(value, field=key)
            for key, value in {
                "train_start": train_start,
                "train_end": train_end,
                "valid_start": valid_start,
                "valid_end": valid_end,
                "test_start": test_start,
                "test_end": test_end,
            }.items()
        }
        if not (
            values["train_start"] <= values["train_end"]
            < values["valid_start"] <= values["valid_end"]
            < values["test_start"] <= values["test_end"]
        ):
            raise QlibDoubleEnsembleError(
                "train, valid, and test must be non-overlapping chronological segments"
            )
        return cls(**values)

    def to_dict(self) -> dict[str, str]:
        return {key: value.isoformat() for key, value in vars(self).items()}


def _canonical_date(value: Any, *, field: str) -> date:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise QlibDoubleEnsembleError(f"{field} must be canonical YYYY-MM-DD") from None
    if text != parsed.isoformat():
        raise QlibDoubleEnsembleError(f"{field} must be canonical YYYY-MM-DD")
    return parsed


def _strict_float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise QlibDoubleEnsembleError(f"{field} must be finite") from None
    if not math.isfinite(number):
        raise QlibDoubleEnsembleError(f"{field} must be finite")
    return number


def _qlib_code(value: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) == 9 and text[:3] in {"sh.", "sz."} and text[3:].isdigit():
        return text[:2].upper() + text[3:]
    if len(text) == 6 and text.isdigit():
        exchange = "SH" if text.startswith(("600", "601", "603", "605")) else "SZ"
        return exchange + text
    raise QlibDoubleEnsembleError("code must be sh./sz. plus six digits")


def _plain_code(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text[:2].upper() in {"SH", "SZ"} and text[2:].isdigit():
        return text[2:]
    if len(text) == 9 and text[:3].lower() in {"sh.", "sz."} and text[3:].isdigit():
        return text[3:]
    if len(text) == 6 and text.isdigit():
        return text
    raise QlibDoubleEnsembleError("instrument must identify one six-digit A-share code")


def model_config_payload() -> dict[str, Any]:
    return {
        "alpha158_feature_count": EXPECTED_ALPHA158_FEATURE_COUNT,
        "candidate_policy": {
            "min_reliable_candidates": MIN_RELIABLE_CANDIDATES,
            "top_n": TOP_N,
            "tie_break": "score_desc_stock_code_asc",
        },
        "model_class": "qlib.contrib.model.double_ensemble.DEnsembleModel",
        "model_config_version": MODEL_CONFIG_VERSION,
        "model_kwargs": dict(OFFICIAL_MODEL_KWARGS),
        "qlib_version": QLIB_PACKAGE_VERSION,
        "random_seed": RANDOM_SEED,
        "handler_class": "qlib.contrib.data.handler.Alpha158",
        "label_lookahead_sessions": LABEL_LOOKAHEAD_SESSIONS,
        "model_universe": {
            "contract_version": MODEL_UNIVERSE_CONTRACT_VERSION,
            "main_board_prefixes": list(MAIN_BOARD_PREFIXES),
            "excluded": ["ST", "*ST", "suspended"],
        },
        "v21_baseline_universe_config_hash": universe_config_hash(),
        "v21_baseline_universe_contract_version": UNIVERSE_CONTRACT_VERSION,
    }


def model_config_sha256() -> str:
    return hashlib.sha256(canonical_json_bytes(model_config_payload())).hexdigest()


def alpha158_feature_names() -> tuple[str, ...]:
    """Return names from Qlib's official Alpha158 implementation."""

    try:
        from qlib.contrib.data.handler import Alpha158
    except ImportError as exc:
        raise QlibDoubleEnsembleError("pyqlib==0.9.7 is required") from exc
    handler = object.__new__(Alpha158)
    fields, names = handler.get_feature_config()
    if len(fields) != EXPECTED_ALPHA158_FEATURE_COUNT or len(names) != len(fields):
        raise QlibDoubleEnsembleError("installed Qlib Alpha158 feature contract drifted")
    return tuple(names)


def normalize_raw_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [name for name in RAW_REQUIRED_COLUMNS if name not in frame.columns]
    if missing:
        raise QlibDoubleEnsembleError(
            "raw history is missing required fields: " + ", ".join(missing)
        )
    data = frame.loc[:, list(RAW_REQUIRED_COLUMNS)].copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    if data["date"].isna().any():
        raise QlibDoubleEnsembleError("raw history contains invalid date")
    data["code"] = data["code"].map(_qlib_code)
    if data["code"].nunique() != 1:
        raise QlibDoubleEnsembleError("one raw file must contain exactly one instrument")
    if data["date"].duplicated().any() or not data["date"].is_monotonic_increasing:
        raise QlibDoubleEnsembleError("raw history dates must be unique and increasing")
    if set(data["price_basis"].astype(str)) != {"raw_unadjusted"}:
        raise QlibDoubleEnsembleError("only raw unadjusted history is accepted")
    allowed_sources = {
        "baostock.query_history_k_data_plus.adjustflag_3",
        "akshare.stock_zh_a_hist.eastmoney.raw",
        "pytdx.get_security_bars.raw",
    }
    if not set(data["source_id"].astype(str)).issubset(allowed_sources):
        raise QlibDoubleEnsembleError("unexpected raw-history source_id")
    for column in (
        "open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"
    ):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["tradestatus"] = data["tradestatus"].astype(str)
    data["isST"] = data["isST"].astype(str)
    if not set(data["tradestatus"]).issubset({"0", "1"}):
        raise QlibDoubleEnsembleError("tradestatus must contain only 0/1")
    if not set(data["isST"]).issubset({"0", "1"}):
        raise QlibDoubleEnsembleError("isST must contain only 0/1")
    active = data["tradestatus"].eq("1")
    if data.loc[active, ["open", "high", "low", "close", "volume", "amount"]].isna().any().any():
        raise QlibDoubleEnsembleError("active rows require complete OHLCVA")
    if (data.loc[active, ["open", "high", "low", "close"]] <= 0).any().any():
        raise QlibDoubleEnsembleError("active OHLC must be positive")
    if (data.loc[active, ["volume", "amount"]] < 0).any().any():
        raise QlibDoubleEnsembleError("active volume and amount cannot be negative")
    data["vwap"] = np.where(
        data["volume"] > 0,
        data["amount"] / data["volume"],
        np.nan,
    )
    data["factor"] = 1.0
    return data


def _eligible_rows(data: pd.DataFrame) -> pd.DataFrame:
    plain_code = _plain_code(str(data["code"].iloc[0]))
    if not is_main_board_code(plain_code):
        return data.iloc[0:0].copy()
    mask = (
        data["tradestatus"].eq("1")
        & data["isST"].eq("0")
        & ~data["name"].map(is_excluded_name)
    )
    return data.loc[mask].copy()


def _calendar_positions(values: Sequence[pd.Timestamp]) -> dict[pd.Timestamp, int]:
    return {pd.Timestamp(value).normalize(): index for index, value in enumerate(values)}


def _instrument_spans(
    eligible_dates: Iterable[pd.Timestamp],
    calendar: Sequence[pd.Timestamp],
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    positions = _calendar_positions(calendar)
    indices = sorted(
        positions[pd.Timestamp(value).normalize()]
        for value in eligible_dates
        if pd.Timestamp(value).normalize() in positions
    )
    if not indices:
        return []
    spans = []
    start = previous = indices[0]
    for current in indices[1:]:
        if current != previous + 1:
            spans.append((calendar[start], calendar[previous]))
            start = current
        previous = current
    spans.append((calendar[start], calendar[previous]))
    return spans


def _write_qlib_feature(
    *,
    provider_uri: Path,
    instrument: str,
    field: str,
    values: np.ndarray,
    start_index: int,
) -> None:
    feature_dir = provider_uri / "features" / instrument.lower()
    feature_dir.mkdir(parents=True, exist_ok=True)
    payload = np.hstack([start_index, values]).astype("<f4")
    payload.tofile(feature_dir / f"{field.lower()}.day.bin")


def build_qlib_provider(
    *,
    source_dir: Path,
    provider_uri: Path,
    replace: bool = False,
    start_date: str | None = None,
) -> dict[str, Any]:
    """Convert validated private raw daily CSV files to Qlib file storage."""

    source_dir = Path(source_dir).resolve()
    provider_uri = Path(provider_uri).resolve()
    symbol_dir = source_dir / "symbols"
    calendar_file = source_dir / "calendar.csv"
    if not symbol_dir.is_dir() or not calendar_file.is_file():
        raise QlibDoubleEnsembleError("source_dir requires symbols/ and calendar.csv")
    if provider_uri.exists():
        if not replace:
            raise QlibDoubleEnsembleError("provider_uri already exists; use replace explicitly")
        shutil.rmtree(provider_uri)
    (provider_uri / "calendars").mkdir(parents=True)
    (provider_uri / "instruments").mkdir(parents=True)
    (provider_uri / "features").mkdir(parents=True)
    (provider_uri / "metadata").mkdir(parents=True)

    import qlib
    from qlib.constant import REG_CN

    qlib.init(provider_uri=str(provider_uri), region=REG_CN)

    calendar_frame = pd.read_csv(calendar_file, dtype=str)
    if set(calendar_frame.columns) != {"calendar_date", "is_trading_day"}:
        raise QlibDoubleEnsembleError("calendar.csv schema mismatch")
    trading = calendar_frame.loc[
        calendar_frame["is_trading_day"].eq("1"), "calendar_date"
    ]
    calendar = tuple(pd.to_datetime(trading, errors="raise").sort_values().drop_duplicates())
    if start_date is not None:
        provider_start = pd.Timestamp(_canonical_date(start_date, field="start_date"))
        calendar = tuple(value for value in calendar if value >= provider_start)
    if not calendar:
        raise QlibDoubleEnsembleError("verified trading calendar is empty")

    (provider_uri / "calendars" / "day.txt").write_text(
        "".join(item.strftime("%Y-%m-%d\n") for item in calendar),
        encoding="utf-8",
    )
    positions = _calendar_positions(calendar)
    instruments: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    eligible_parts = []
    names: dict[str, str] = {}
    source_ids: set[str] = set()
    row_count = 0

    files = sorted(symbol_dir.glob("*.csv"))
    if not files:
        raise QlibDoubleEnsembleError("symbols directory is empty")
    for path in files:
        data = normalize_raw_frame(pd.read_csv(path, dtype=str))
        instrument = str(data["code"].iloc[0])
        source_ids.update(data["source_id"].astype(str).unique())
        names[_plain_code(instrument)] = str(data["name"].dropna().iloc[-1]).strip()
        if start_date is not None:
            data = data.loc[data["date"].ge(calendar[0])].copy()
        if data.empty:
            continue
        row_count += len(data)
        normalized_dates = data["date"].dt.normalize()
        known = normalized_dates.isin(positions)
        if not known.all():
            raise QlibDoubleEnsembleError(f"{path.name} contains date outside calendar")
        start_index = min(positions[value] for value in normalized_dates)
        end_index = max(positions[value] for value in normalized_dates)
        aligned_index = pd.DatetimeIndex(calendar[start_index : end_index + 1])
        indexed = data.set_index("date").reindex(aligned_index)
        for field in QLIB_FIELDS:
            _write_qlib_feature(
                provider_uri=provider_uri,
                instrument=instrument,
                field=field,
                values=indexed[field].to_numpy(dtype=float),
                start_index=start_index,
            )
        eligible = _eligible_rows(data)
        spans = _instrument_spans(eligible["date"], calendar)
        if spans:
            instruments[instrument] = spans
            rolling = data.set_index("date").copy()
            rolling["ma5"] = rolling["close"].rolling(5).mean()
            rolling["ma10"] = rolling["close"].rolling(10).mean()
            rolling["ma20"] = rolling["close"].rolling(20).mean()
            rolling["five_day_pct"] = (
                rolling["close"] / rolling["close"].shift(5) - 1.0
            ) * 100.0
            rolling["volume_ratio_5d"] = rolling["volume"] / (
                rolling["volume"].shift(1).rolling(5).mean() + 1e-9
            )
            rolling["avg_amount_20d"] = rolling["amount"].rolling(20).mean()
            required_metrics = (
                "ma5", "ma10", "ma20", "five_day_pct",
                "volume_ratio_5d", "avg_amount_20d",
            )
            selected = rolling.loc[pd.DatetimeIndex(eligible["date"])].dropna(
                subset=list(required_metrics)
            )
            if not selected.empty:
                eligible_parts.append(
                    pd.DataFrame(
                        {
                            "datetime": selected.index,
                            "instrument": instrument,
                            "stock_code": _plain_code(instrument),
                            "stock_name": selected["name"].astype(str).to_numpy(),
                            "close": selected["close"].to_numpy(dtype=float),
                            "preclose": selected["preclose"].to_numpy(dtype=float),
                            "pctChg": selected["pctChg"].to_numpy(dtype=float),
                            "volume": selected["volume"].to_numpy(dtype=float),
                            "amount": selected["amount"].to_numpy(dtype=float),
                            "turn": selected["turn"].to_numpy(dtype=float),
                            "history_close": selected["close"].to_numpy(dtype=float),
                            "five_day_pct": selected["five_day_pct"].to_numpy(dtype=float),
                            "ma5": selected["ma5"].to_numpy(dtype=float),
                            "ma10": selected["ma10"].to_numpy(dtype=float),
                            "ma20": selected["ma20"].to_numpy(dtype=float),
                            "volume_ratio_5d": selected["volume_ratio_5d"].to_numpy(dtype=float),
                            "avg_amount_20d": selected["avg_amount_20d"].to_numpy(dtype=float),
                            "is_intraday": False,
                            "history_data_through": selected.index.strftime("%Y-%m-%d"),
                        }
                    )
                )

    if not instruments:
        raise QlibDoubleEnsembleError("no eligible main-board observations")
    instrument_lines = []
    for instrument in sorted(instruments):
        for start, end in instruments[instrument]:
            instrument_lines.append(
                f"{instrument}\t{start:%Y-%m-%d}\t{end:%Y-%m-%d}\n"
            )
    (provider_uri / "instruments" / "all.txt").write_text(
        "".join(instrument_lines), encoding="utf-8"
    )
    eligible_frame = pd.concat(eligible_parts, ignore_index=True) if eligible_parts else pd.DataFrame()
    eligible_frame.to_parquet(provider_uri / "metadata" / "eligible_rows.parquet", index=False)
    (provider_uri / "metadata" / "names.json").write_text(
        json.dumps(names, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "alpha158_input_fields": list(QLIB_FIELDS),
        "calendar_end": calendar[-1].strftime("%Y-%m-%d"),
        "calendar_start": calendar[0].strftime("%Y-%m-%d"),
        "evidence_status": EVIDENCE_STATUS,
        "evidence_status_reasons": list(EVIDENCE_STATUS_REASONS),
        "instrument_count": len(instruments),
        "model_config_sha256": model_config_sha256(),
        "price_basis": "raw_unadjusted",
        "provider_start": calendar[0].strftime("%Y-%m-%d"),
        "qlib_provider_format": "file_storage_day",
        "raw_row_count": row_count,
        "source_ids": sorted(source_ids),
        "source_symbol_file_count": len(files),
        "model_universe_contract_version": MODEL_UNIVERSE_CONTRACT_VERSION,
        "v21_baseline_universe_config_hash": universe_config_hash(),
        "v21_baseline_universe_contract_version": UNIVERSE_CONTRACT_VERSION,
        "vwap_derivation": "amount_cny / volume_shares",
    }
    manifest["manifest_sha256"] = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    (provider_uri / "metadata" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def fit_model_and_predict(
    *,
    provider_uri: Path,
    splits: TimeSplits,
) -> tuple[Any, pd.Series, dict[str, Any]]:
    """Fit the official model once and return it with test predictions."""

    try:
        import qlib
        from qlib.constant import REG_CN
        from qlib.contrib.data.handler import Alpha158
        from qlib.contrib.model.double_ensemble import DEnsembleModel
        from qlib.data.dataset import DatasetH
        from qlib.data.dataset.handler import DataHandlerLP
    except ImportError as exc:
        raise QlibDoubleEnsembleError("pyqlib==0.9.7 is required") from exc
    if qlib.__version__ != QLIB_PACKAGE_VERSION:
        raise QlibDoubleEnsembleError(
            f"Qlib version must be {QLIB_PACKAGE_VERSION}, got {qlib.__version__}"
        )
    provider_uri = Path(provider_uri).resolve()
    manifest_path = provider_uri / "metadata" / "manifest.json"
    if not manifest_path.is_file():
        raise QlibDoubleEnsembleError("Qlib provider manifest is missing")
    provider_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if provider_manifest.get("model_config_sha256") != model_config_sha256():
        raise QlibDoubleEnsembleError("Qlib provider model config hash mismatch")
    calendar = pd.to_datetime(
        (provider_uri / "calendars" / "day.txt").read_text(encoding="utf-8").splitlines()
    )
    positions = {value.date(): index for index, value in enumerate(calendar)}
    for earlier, later in (
        (splits.train_end, splits.valid_start),
        (splits.valid_end, splits.test_start),
    ):
        if earlier not in positions or later not in positions:
            raise QlibDoubleEnsembleError("split boundary is absent from trading calendar")
        if positions[later] - positions[earlier] <= LABEL_LOOKAHEAD_SESSIONS:
            raise QlibDoubleEnsembleError(
                "Alpha158 label requires a two-session embargo between segments"
            )
    qlib.init(provider_uri=str(provider_uri), region=REG_CN)
    random.seed(RANDOM_SEED)
    np.random.seed(RANDOM_SEED)
    handler = Alpha158(
        instruments="all",
        start_time=splits.train_start.isoformat(),
        end_time=splits.test_end.isoformat(),
        fit_start_time=splits.train_start.isoformat(),
        fit_end_time=splits.train_end.isoformat(),
    )
    dataset = DatasetH(
        handler=handler,
        segments={
            "train": (splits.train_start.isoformat(), splits.train_end.isoformat()),
            "valid": (splits.valid_start.isoformat(), splits.valid_end.isoformat()),
            "test": (splits.test_start.isoformat(), splits.test_end.isoformat()),
        },
    )
    test_features = dataset.prepare(
        "test", col_set="feature", data_key=DataHandlerLP.DK_I
    )
    if test_features.shape[1] != EXPECTED_ALPHA158_FEATURE_COUNT:
        raise QlibDoubleEnsembleError(
            f"Alpha158 generated {test_features.shape[1]} features, expected 158"
        )
    model = DEnsembleModel(**dict(OFFICIAL_MODEL_KWARGS))
    model.fit(dataset)
    predictions = model.predict(dataset, segment="test")
    predictions.name = "doubleensemble_score"
    finite = pd.to_numeric(predictions, errors="coerce").map(math.isfinite)
    if not finite.all():
        raise QlibDoubleEnsembleError("DoubleEnsemble produced non-finite scores")
    run_manifest = {
        "alpha158_feature_count": int(test_features.shape[1]),
        "alpha158_complete": test_features.shape[1] == EXPECTED_ALPHA158_FEATURE_COUNT,
        "model_config_sha256": model_config_sha256(),
        "label_embargo_sessions": LABEL_LOOKAHEAD_SESSIONS,
        "prediction_count": int(len(predictions)),
        "qlib_version": qlib.__version__,
        "segments": splits.to_dict(),
        "test_feature_non_null_rate": round(float(test_features.notna().mean().mean()), 8),
    }
    return model, predictions, run_manifest


def fit_and_predict(
    *,
    provider_uri: Path,
    splits: TimeSplits,
) -> tuple[pd.Series, dict[str, Any]]:
    """Fit official Alpha158 + DoubleEnsemble and predict only the test segment."""

    _, predictions, run_manifest = fit_model_and_predict(
        provider_uri=provider_uri,
        splits=splits,
    )
    return predictions, run_manifest


def predict_with_frozen_model(
    *,
    model: Any,
    provider_uri: Path,
    data_as_of: date,
) -> tuple[pd.Series, dict[str, Any]]:
    """Run one-day Alpha158 inference without fitting or changing the model."""

    try:
        import qlib
        from qlib.constant import REG_CN
        from qlib.contrib.data.handler import Alpha158
        from qlib.data.dataset import DatasetH
        from qlib.data.dataset.handler import DataHandlerLP
    except ImportError as exc:
        raise QlibDoubleEnsembleError("pyqlib==0.9.7 is required") from exc
    if qlib.__version__ != QLIB_PACKAGE_VERSION:
        raise QlibDoubleEnsembleError(
            f"Qlib version must be {QLIB_PACKAGE_VERSION}, got {qlib.__version__}"
        )
    provider_uri = Path(provider_uri).resolve()
    manifest_path = provider_uri / "metadata" / "manifest.json"
    if not manifest_path.is_file():
        raise QlibDoubleEnsembleError("Qlib provider manifest is missing")
    provider_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if provider_manifest.get("model_config_sha256") != model_config_sha256():
        raise QlibDoubleEnsembleError("Qlib provider model config hash mismatch")
    cutoff = data_as_of.isoformat()
    calendar = (provider_uri / "calendars" / "day.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    if cutoff not in calendar:
        raise QlibDoubleEnsembleError("data_as_of is absent from trading calendar")

    qlib.init(provider_uri=str(provider_uri), region=REG_CN)
    handler = Alpha158(
        instruments="all",
        start_time=cutoff,
        end_time=cutoff,
    )
    dataset = DatasetH(handler=handler, segments={"infer": (cutoff, cutoff)})
    features = dataset.prepare(
        "infer", col_set="feature", data_key=DataHandlerLP.DK_I
    )
    if features.shape[1] != EXPECTED_ALPHA158_FEATURE_COUNT:
        raise QlibDoubleEnsembleError(
            f"Alpha158 generated {features.shape[1]} features, expected 158"
        )
    predictions = model.predict(dataset, segment="infer")
    predictions.name = "doubleensemble_score"
    finite = pd.to_numeric(predictions, errors="coerce").map(math.isfinite)
    if predictions.empty or not finite.all():
        raise QlibDoubleEnsembleError(
            "frozen DoubleEnsemble produced empty or non-finite scores"
        )
    return predictions, {
        "alpha158_complete": True,
        "alpha158_feature_count": int(features.shape[1]),
        "data_as_of": cutoff,
        "feature_non_null_rate": round(float(features.notna().mean().mean()), 8),
        "prediction_count": int(len(predictions)),
        "qlib_version": qlib.__version__,
    }


def build_candidate_batches(
    *,
    predictions: pd.Series,
    provider_uri: Path,
    calendar: Sequence[str],
) -> list[dict[str, Any]]:
    """Shift T-1 scores to T and return deterministic daily 0-or-Top-5 batches."""

    if not isinstance(predictions.index, pd.MultiIndex):
        raise QlibDoubleEnsembleError("Qlib predictions must use a MultiIndex")
    names = json.loads(
        (Path(provider_uri) / "metadata" / "names.json").read_text(encoding="utf-8")
    )
    dates = tuple(pd.Timestamp(value).normalize() for value in calendar)
    next_date = {dates[index]: dates[index + 1] for index in range(len(dates) - 1)}
    frame = predictions.rename("score").reset_index()
    if not {"datetime", "instrument", "score"}.issubset(frame.columns):
        raise QlibDoubleEnsembleError("Qlib prediction index levels must be datetime/instrument")
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.normalize()
    batches = []
    for cutoff, group in frame.groupby("datetime", sort=True):
        signal_date = next_date.get(cutoff)
        if signal_date is None:
            continue
        ranked = group.assign(
            stock_code=group["instrument"].map(_plain_code)
        ).sort_values(["score", "stock_code"], ascending=[False, True], kind="stable")
        if len(ranked) < MIN_RELIABLE_CANDIDATES:
            selected = ranked.iloc[0:0]
            status = "insufficient_reliable_candidates"
        else:
            selected = ranked.head(TOP_N)
            status = "ok"
        candidates = []
        for rank, row in enumerate(selected.itertuples(index=False), start=1):
            candidates.append(
                {
                    "trade_date": signal_date.strftime("%Y-%m-%d"),
                    "stock_code": row.stock_code,
                    "stock_name": names.get(row.stock_code),
                    "model_rank": rank,
                    "doubleensemble_score": float(row.score),
                    "data_cutoff_date": cutoff.strftime("%Y-%m-%d"),
                    "model_config_version": MODEL_CONFIG_VERSION,
                    "data_status": status,
                    "evidence_status": EVIDENCE_STATUS,
                }
            )
        batches.append(
            {
                "trade_date": signal_date.strftime("%Y-%m-%d"),
                "data_cutoff_date": cutoff.strftime("%Y-%m-%d"),
                "candidate_count": len(candidates),
                "data_status": status,
                "evidence_status": EVIDENCE_STATUS,
                "candidates": candidates,
            }
        )
    return batches


def baseline_batches(*, provider_uri: Path, candidate_batches: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rank the frozen V2.1 scores already produced during data adaptation."""

    eligible = pd.read_parquet(Path(provider_uri) / "metadata" / "eligible_rows.parquet")
    eligible["datetime"] = pd.to_datetime(eligible["datetime"]).dt.normalize()
    wanted = {pd.Timestamp(item["data_cutoff_date"]) for item in candidate_batches}
    eligible = eligible.loc[eligible["datetime"].isin(wanted)].copy()
    eligible_indices = []
    for _, group in eligible.groupby("datetime", sort=True):
        group = group.loc[pd.to_numeric(group["turn"], errors="coerce").notna()]
        if group.empty:
            continue
        spot = pd.DataFrame(
            {
                "code": group["stock_code"],
                "name": group["stock_name"],
                "close": group["close"],
                "prev_close": group["preclose"],
                "pct_change": group["pctChg"],
                "volume": group["volume"],
                "amount": group["amount"],
                "turnover": group["turn"],
            },
            index=group.index,
        )
        config = ScreeningConfig(
            preselect_limit=max(5, len(spot)),
            enrichment_limit=max(8, len(spot)),
        )
        eligible_indices.extend(apply_spot_filters(spot, config).index)
    eligible = eligible.loc[eligible_indices]
    scores = []
    config = ScreeningConfig()
    for row in eligible.itertuples(index=False):
        spot = {
            "code": row.stock_code,
            "name": row.stock_name,
            "close": row.close,
            "prev_close": row.preclose,
            "pct_change": row.pctChg,
            "volume": row.volume,
            "amount": row.amount,
            "turnover": row.turn,
            "volume_ratio": None,
            "pe_ratio": None,
            "pb_ratio": None,
        }
        metrics = {
            "history_close": row.history_close,
            "five_day_pct": row.five_day_pct,
            "ma5": row.ma5,
            "ma10": row.ma10,
            "ma20": row.ma20,
            "volume_ratio_5d": row.volume_ratio_5d,
            "avg_amount_20d": row.avg_amount_20d,
            "is_intraday": False,
            "history_data_through": row.history_data_through,
        }
        candidate = build_candidate(spot, metrics, config)
        if candidate is not None:
            scores.append(
                {
                    "datetime": row.datetime,
                    "stock_code": row.stock_code,
                    "stock_name": row.stock_name,
                    "v21_score": candidate.score,
                }
            )
    scored = pd.DataFrame(scores)
    result = []
    signal_by_cutoff = {
        pd.Timestamp(item["data_cutoff_date"]): item["trade_date"]
        for item in candidate_batches
    }
    for cutoff in sorted(wanted):
        group = (
            scored.loc[scored["datetime"].eq(cutoff)]
            if not scored.empty
            else scored
        )
        ranked = group.sort_values(
            ["v21_score", "stock_code"], ascending=[False, True], kind="stable"
        ).head(TOP_N)
        result.append(
            {
                "trade_date": signal_by_cutoff[cutoff],
                "data_cutoff_date": cutoff.strftime("%Y-%m-%d"),
                "candidates": [
                    {
                        "stock_code": row.stock_code,
                        "stock_name": row.stock_name,
                        "model_rank": rank,
                        "v21_score": float(row.v21_score),
                    }
                    for rank, row in enumerate(ranked.itertuples(index=False), start=1)
                ],
            }
        )
    return result


def evaluate_out_of_sample(
    *,
    source_dir: Path,
    doubleensemble_batches: Sequence[Mapping[str, Any]],
    v21_batches: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate both rankings with the existing unified-race return engine."""

    from research.benchmarks.unified_race import (
        ENGINE_VERSION,
        SUPPORTED_HORIZONS,
        ForwardBar,
        _Outcome,
        _aggregate,
    )
    from src.core.backtest_engine import BacktestEngine, EvaluationConfig

    source_dir = Path(source_dir)
    calendar_frame = pd.read_csv(source_dir / "calendar.csv", dtype=str)
    calendar = tuple(
        pd.to_datetime(
            calendar_frame.loc[
                calendar_frame["is_trading_day"].eq("1"), "calendar_date"
            ]
        ).dt.date
    )
    calendar_position = {value: index for index, value in enumerate(calendar)}
    batches_by_model = {
        "doubleensemble": doubleensemble_batches,
        "v2_1_baseline": v21_batches,
    }
    needed_codes = {
        str(candidate["stock_code"])
        for batches in batches_by_model.values()
        for batch in batches
        for candidate in batch.get("candidates", [])
    }
    histories: dict[str, pd.DataFrame] = {}
    for code in sorted(needed_codes):
        path = source_dir / "symbols" / f"{code}.csv"
        if path.is_file():
            data = normalize_raw_frame(pd.read_csv(path, dtype=str))
            histories[code] = data.set_index(data["date"].dt.date)
    benchmark_path = source_dir / "benchmark_sh000300.csv"
    if not benchmark_path.is_file():
        raise QlibDoubleEnsembleError("HS300 benchmark history is missing")
    benchmark = normalize_raw_frame(pd.read_csv(benchmark_path, dtype=str))
    benchmark = benchmark.set_index(benchmark["date"].dt.date)

    outcomes: dict[tuple[str, str], list[Any]] = {
        (model, horizon): []
        for model in batches_by_model
        for horizon in SUPPORTED_HORIZONS
    }
    exclusions: dict[str, int] = {}
    for model, batches in batches_by_model.items():
        for batch in batches:
            signal_date = _canonical_date(batch["trade_date"], field="trade_date")
            start_position = calendar_position.get(signal_date)
            if start_position is None or signal_date not in benchmark.index:
                exclusions["signal_or_benchmark_reference_missing"] = (
                    exclusions.get("signal_or_benchmark_reference_missing", 0) + 1
                )
                continue
            benchmark_reference = _strict_float(
                benchmark.loc[signal_date, "close"], field="hs300_reference_close"
            )
            for horizon, days in SUPPORTED_HORIZONS.items():
                forward_dates = calendar[start_position + 1 : start_position + days + 1]
                if len(forward_dates) != days or any(
                    value not in benchmark.index for value in forward_dates
                ):
                    exclusions["incomplete_forward_calendar"] = (
                        exclusions.get("incomplete_forward_calendar", 0) + 1
                    )
                    continue
                hs300_return = (
                    _strict_float(
                        benchmark.loc[forward_dates[-1], "close"],
                        field="hs300_forward_close",
                    )
                    / benchmark_reference
                    - 1.0
                ) * 100.0
                for candidate in batch.get("candidates", []):
                    code = str(candidate["stock_code"])
                    history = histories.get(code)
                    if history is None or signal_date not in history.index or any(
                        value not in history.index for value in forward_dates
                    ):
                        exclusions["candidate_forward_history_missing"] = (
                            exclusions.get("candidate_forward_history_missing", 0) + 1
                        )
                        continue
                    reference = _strict_float(
                        history.loc[signal_date, "close"], field="reference_close"
                    )
                    bars = [
                        ForwardBar(
                            date=value,
                            high=_strict_float(history.loc[value, "high"], field="forward_high"),
                            low=_strict_float(history.loc[value, "low"], field="forward_low"),
                            close=_strict_float(history.loc[value, "close"], field="forward_close"),
                        )
                        for value in forward_dates
                    ]
                    evaluation = BacktestEngine.evaluate_decision_signal(
                        direction_expected="up",
                        anchor_date=signal_date,
                        start_price=reference,
                        forward_bars=bars,
                        config=EvaluationConfig(
                            eval_window_days=days,
                            neutral_band_pct=0.0,
                            engine_version=ENGINE_VERSION,
                        ),
                    )
                    if evaluation.get("eval_status") != "completed":
                        raise QlibDoubleEnsembleError("validated outcome did not complete")
                    gross = _strict_float(
                        evaluation["stock_return_pct"], field="stock_return_pct"
                    )
                    max_high = _strict_float(evaluation["max_high"], field="max_high")
                    min_low = _strict_float(evaluation["min_low"], field="min_low")
                    net = gross - ROUND_TRIP_COST_BPS / 100.0
                    outcomes[(model, horizon)].append(
                        _Outcome(
                            model,
                            code,
                            signal_date,
                            horizon,
                            gross,
                            net,
                            max(0.0, (max_high / reference - 1.0) * 100.0),
                            max(0.0, (reference - min_low) / reference * 100.0),
                            net - hs300_return,
                        )
                    )
    models = {
        model: {
            horizon: _aggregate(outcomes[(model, horizon)])
            for horizon in SUPPORTED_HORIZONS
        }
        for model in batches_by_model
    }
    comparison = {}
    for horizon in SUPPORTED_HORIZONS:
        double = models["doubleensemble"][horizon]
        baseline = models["v2_1_baseline"][horizon]
        comparison[horizon] = {
            "doubleensemble_minus_v2_1_net_mean_pct": (
                round(
                    double["net_return_mean_pct"] - baseline["net_return_mean_pct"],
                    8,
                )
                if double["net_return_mean_pct"] is not None
                and baseline["net_return_mean_pct"] is not None
                else None
            ),
            "doubleensemble_minus_v2_1_hs300_excess_mean_pct": (
                round(
                    double["hs300_excess_mean_pct"]
                    - baseline["hs300_excess_mean_pct"],
                    8,
                )
                if double["hs300_excess_mean_pct"] is not None
                and baseline["hs300_excess_mean_pct"] is not None
                else None
            ),
        }
    summary = {
        "cost_bps": ROUND_TRIP_COST_BPS,
        "engine_version": ENGINE_VERSION,
        "evidence_status": EVIDENCE_STATUS,
        "evidence_status_reasons": list(EVIDENCE_STATUS_REASONS),
        "metric_definitions": {
            "reference_price": "signal-date close; evaluation only, not a candidate buy price",
            "gross_return": "forward close / signal-date close - 1",
            "net_return": "gross return - 30bps",
            "mfe": "max(0, max forward high / reference - 1)",
            "mae": "max(0, (reference - min forward low) / reference)",
            "max_drawdown": "existing unified-race date-level equal-weight net-return drawdown",
            "hs300_excess": "model net return - HS300 gross return",
        },
        "models": models,
        "comparison": comparison,
        "exclusions": dict(sorted(exclusions.items())),
    }
    summary["summary_sha256"] = hashlib.sha256(canonical_json_bytes(summary)).hexdigest()
    return summary
