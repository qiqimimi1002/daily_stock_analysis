"""Immutable forward outcome calculation for archived V2.1 observation signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid5

from research.archive import (
    SHANGHAI_TZ,
    _aware_shanghai_datetime,
    _canonical_json_bytes,
    _clean_json_value,
    _normalize_stock_code,
    _sha256_bytes,
    _strict_json_bytes,
)


OUTCOME_SCHEMA_VERSION = "V2.2.2"
DEFAULT_CALCULATION_VERSION = "V2.2-OUTCOME-2"
SUPPORTED_PRICE_BASIS = "raw_unadjusted"
DEFAULT_ROUND_TRIP_COST_BPS = 30.0
BENCHMARK_CODE = "000300"
OUTCOME_NAMESPACE = UUID("4d8f48ba-c609-4ef2-9b50-93d623a60e4f")
HORIZONS = (1, 3, 5, 10, 20)
SESSION_CLOSE = time(15, 0)


class OutcomeValidationError(ValueError):
    """Raised when a signal archive or price artifact is not auditable."""


class OutcomeConflictError(RuntimeError):
    """Raised when an immutable outcome batch cannot be verified."""


@dataclass(frozen=True)
class LoadedPriceArtifact:
    source: Mapping[str, Any]
    source_file_sha256: str


@dataclass(frozen=True)
class OutcomeResult:
    status: str
    archive_dir: Path
    outcome_count: int
    outcome_ids: Tuple[str, ...]
    content_hash: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "archive_dir": str(self.archive_dir),
            "outcome_count": self.outcome_count,
            "outcome_ids": list(self.outcome_ids),
            "content_hash": self.content_hash,
        }


def _mapping(value: Any, *, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OutcomeValidationError(f"{field} must be an object")
    return dict(value)


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeValidationError(f"{field} is required")
    return value.strip()


def _iso_date(value: Any, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise OutcomeValidationError(f"{field} must be an ISO-8601 date") from None


def _number(value: Any, *, field: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise OutcomeValidationError(f"{field} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise OutcomeValidationError(f"{field} must be a finite number") from None
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "positive finite" if positive else "finite"
        raise OutcomeValidationError(f"{field} must be a {qualifier} number")
    return result


def _optional_number(value: Any, *, field: str, positive: bool = False) -> Optional[float]:
    if value in (None, ""):
        return None
    return _number(value, field=field, positive=positive)


def _boolean(value: Any, *, field: str, default: Optional[bool] = None) -> Optional[bool]:
    if value is None and default is not None:
        return default
    if value is None:
        return None
    if not isinstance(value, bool):
        raise OutcomeValidationError(f"{field} must be a boolean")
    return value


def _shanghai_datetime(value: Any, *, field: str) -> datetime:
    try:
        return _aware_shanghai_datetime(value, field=field)
    except ValueError as exc:
        raise OutcomeValidationError(str(exc)) from exc


def _digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise OutcomeValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _session_is_closed(day: date, *, as_of: datetime) -> bool:
    return day < as_of.date() or (day == as_of.date() and as_of.timetz().replace(tzinfo=None) >= SESSION_CLOSE)


def _round_pct(value: float) -> float:
    return round(value * 100.0, 6)


def build_outcome_id(signal_id: str, horizon_days: int, calculation_version: str) -> str:
    """Return a stable result identity independent of price-data revisions."""
    identity = f"{_text(signal_id, field='signal_id')}|{int(horizon_days)}|{_text(calculation_version, field='calculation_version')}"
    return str(uuid5(OUTCOME_NAMESPACE, identity))


def _write_outcomes_parquet(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "Parquet output requires the isolated research dependencies: "
            "pip install -r requirements-research.txt"
        ) from exc
    rows = []
    for record in records:
        row = {}
        for key, value in record.items():
            row[key] = (
                _canonical_json_bytes(value).decode("utf-8")
                if isinstance(value, (Mapping, list, tuple))
                else value
            )
        rows.append(row)
    table = pa.Table.from_pylist(rows) if rows else pa.table({"outcome_id": pa.array([], type=pa.string())})
    pq.write_table(table, path)


def load_price_artifact(path: Path | str) -> LoadedPriceArtifact:
    """Hash exact price-artifact bytes before parsing the same bytes as JSON."""
    source_path = Path(path)
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise OutcomeValidationError(f"cannot read price JSON: {source_path}") from exc
    try:
        source = json.loads(source_bytes.decode("utf-8"), parse_constant=lambda token: float(token))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutcomeValidationError(f"cannot read price JSON: {source_path}") from exc
    return LoadedPriceArtifact(
        source=_mapping(source, field="prices"),
        source_file_sha256=_sha256_bytes(source_bytes),
    )


def _load_signal_archives(root: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    manifest_paths = [root / "manifest.json"] if (root / "manifest.json").is_file() else sorted(root.rglob("manifest.json"))
    if not manifest_paths:
        raise OutcomeValidationError(f"no phase-1 signal manifests found under {root}")

    signals: List[Dict[str, Any]] = []
    inputs: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    for manifest_path in manifest_paths:
        batch_dir = manifest_path.parent
        json_path = batch_dir / "signals.json"
        parquet_path = batch_dir / "signals.parquet"
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            payload_bytes = json_path.read_bytes()
            payload = json.loads(payload_bytes.decode("utf-8"))
            parquet_bytes = parquet_path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OutcomeValidationError(f"invalid phase-1 archive: {batch_dir}") from exc

        files = manifest.get("files")
        if not isinstance(files, Mapping):
            raise OutcomeValidationError(f"phase-1 manifest has no file hashes: {manifest_path}")
        actual_json_hash = _sha256_bytes(payload_bytes)
        actual_parquet_hash = _sha256_bytes(parquet_bytes)
        if files.get("signals.json") != actual_json_hash or files.get("signals.parquet") != actual_parquet_hash:
            raise OutcomeValidationError(f"phase-1 archive file hash mismatch: {batch_dir}")
        records = payload.get("signals")
        if not isinstance(records, list) or any(not isinstance(record, Mapping) for record in records):
            raise OutcomeValidationError(f"phase-1 signals.json has invalid records: {json_path}")
        record_ids = [record.get("signal_id") for record in records]
        if manifest.get("signal_count") != len(records) or manifest.get("signal_ids") != record_ids:
            raise OutcomeValidationError(f"phase-1 manifest does not match signals.json: {batch_dir}")

        relative = batch_dir.name if root == batch_dir else batch_dir.relative_to(root).as_posix()
        inputs.append(
            {
                "archive": relative,
                "manifest_sha256": _sha256_bytes(manifest_bytes),
                "signals_json_sha256": actual_json_hash,
                "signals_parquet_sha256": actual_parquet_hash,
            }
        )
        for record in records:
            normalized = dict(record)
            signal_id = _text(normalized.get("signal_id"), field="signal_id")
            if signal_id in seen_ids:
                raise OutcomeValidationError(f"duplicate signal_id across phase-1 archives: {signal_id}")
            seen_ids.add(signal_id)
            normalized["source_signal_archive"] = relative
            signals.append(normalized)

    inputs.sort(key=lambda item: item["archive"])
    input_hash = _sha256_bytes(_canonical_json_bytes(inputs))
    return signals, inputs, input_hash


def _normalize_price_artifact(
    source: Mapping[str, Any],
    *,
    source_file_sha256: str,
    evaluation_as_of: datetime,
) -> Dict[str, Any]:
    root = _mapping(source, field="prices")
    file_hash = _digest(source_file_sha256, field="price_file_sha256")
    content_hash = _sha256_bytes(_canonical_json_bytes(_clean_json_value(root)))
    price_basis = _text(root.get("price_basis"), field="price_basis")
    if price_basis != SUPPORTED_PRICE_BASIS:
        raise OutcomeValidationError(
            f"price_basis must be {SUPPORTED_PRICE_BASIS}; adjusted prices are not comparable to signal snapshots"
        )
    price_source = _text(root.get("price_source"), field="price_source")
    calendar_source = _text(root.get("calendar_source"), field="calendar_source")
    price_data_as_of = _shanghai_datetime(root.get("price_data_as_of"), field="price_data_as_of")
    if price_data_as_of > evaluation_as_of:
        raise OutcomeValidationError("price_data_as_of cannot be later than --as-of")

    raw_dates = root.get("market_trade_dates")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise OutcomeValidationError("market_trade_dates must be a non-empty array")
    calendar = [_iso_date(value, field="market_trade_dates[]") for value in raw_dates]
    if calendar != sorted(set(calendar)):
        raise OutcomeValidationError("market_trade_dates must be sorted and unique")
    if any(day.weekday() >= 5 for day in calendar):
        raise OutcomeValidationError("market_trade_dates cannot contain weekends")

    raw_stocks = _mapping(root.get("stocks"), field="stocks")
    stocks: Dict[str, Dict[str, Any]] = {}
    for raw_code, raw_stock in raw_stocks.items():
        try:
            code = _normalize_stock_code(raw_code)
        except ValueError as exc:
            raise OutcomeValidationError(str(exc)) from exc
        stock = _mapping(raw_stock, field=f"stocks.{code}")
        bars: Dict[date, Dict[str, Any]] = {}
        raw_prices = stock.get("prices")
        if not isinstance(raw_prices, list):
            raise OutcomeValidationError(f"stocks.{code}.prices must be an array")
        for index, raw_bar in enumerate(raw_prices):
            bar = _mapping(raw_bar, field=f"stocks.{code}.prices[{index}]")
            trade_date = _iso_date(bar.get("trade_date"), field=f"stocks.{code}.prices[{index}].trade_date")
            if trade_date not in calendar:
                raise OutcomeValidationError(f"price date is not in market_trade_dates: {code} {trade_date}")
            if trade_date > price_data_as_of.date():
                raise OutcomeValidationError(f"price row is later than price_data_as_of: {code} {trade_date}")
            if trade_date in bars:
                raise OutcomeValidationError(f"duplicate price row: {code} {trade_date}")
            volume = _optional_number(bar.get("volume"), field=f"{code}.{trade_date}.volume")
            suspended = bool(_boolean(bar.get("is_suspended"), field=f"{code}.{trade_date}.is_suspended", default=False))
            if volume == 0:
                suspended = True
            normalized_bar: Dict[str, Any] = {
                "trade_date": trade_date,
                "is_suspended": suspended,
                "volume": volume,
                "is_limit_up": _boolean(bar.get("is_limit_up"), field=f"{code}.{trade_date}.is_limit_up"),
                "is_limit_down": _boolean(bar.get("is_limit_down"), field=f"{code}.{trade_date}.is_limit_down"),
                "limit_up_price": _optional_number(
                    bar.get("limit_up_price"), field=f"{code}.{trade_date}.limit_up_price", positive=True
                ),
            }
            if suspended:
                normalized_bar.update({"open": None, "high": None, "low": None, "close": None})
            else:
                prices = {
                    key: _number(bar.get(key), field=f"{code}.{trade_date}.{key}", positive=True)
                    for key in ("open", "high", "low", "close")
                }
                if prices["high"] < max(prices["open"], prices["low"], prices["close"]):
                    raise OutcomeValidationError(f"invalid OHLC high: {code} {trade_date}")
                if prices["low"] > min(prices["open"], prices["high"], prices["close"]):
                    raise OutcomeValidationError(f"invalid OHLC low: {code} {trade_date}")
                normalized_bar.update(prices)
            bars[trade_date] = normalized_bar

        actions: Dict[date, List[str]] = {}
        for index, raw_action in enumerate(stock.get("corporate_actions") or []):
            action = _mapping(raw_action, field=f"stocks.{code}.corporate_actions[{index}]")
            action_date = _iso_date(action.get("trade_date"), field=f"stocks.{code}.corporate_actions[{index}].trade_date")
            if action_date not in calendar:
                raise OutcomeValidationError(f"corporate action date is not a market trade date: {code} {action_date}")
            if action_date > price_data_as_of.date():
                raise OutcomeValidationError(f"future corporate action is not allowed: {code} {action_date}")
            actions.setdefault(action_date, []).append(_text(action.get("action_type"), field="action_type"))

        conflicts: Dict[date, List[str]] = {}
        for index, raw_conflict in enumerate(stock.get("data_conflicts") or []):
            conflict = _mapping(raw_conflict, field=f"stocks.{code}.data_conflicts[{index}]")
            conflict_date = _iso_date(conflict.get("trade_date"), field=f"stocks.{code}.data_conflicts[{index}].trade_date")
            if conflict_date not in calendar:
                raise OutcomeValidationError(f"data conflict date is not a market trade date: {code} {conflict_date}")
            if conflict_date > price_data_as_of.date():
                raise OutcomeValidationError(f"future data conflict is not allowed: {code} {conflict_date}")
            conflicts.setdefault(conflict_date, []).append(_text(conflict.get("reason"), field="reason"))
        stocks[code] = {"bars": bars, "corporate_actions": actions, "data_conflicts": conflicts}

    benchmark_snapshots: List[Dict[str, Any]] = []
    for index, raw_snapshot in enumerate(root.get("benchmark_reference_snapshots") or []):
        snapshot = _mapping(raw_snapshot, field=f"benchmark_reference_snapshots[{index}]")
        captured_at = _shanghai_datetime(
            snapshot.get("captured_at"), field=f"benchmark_reference_snapshots[{index}].captured_at"
        )
        if captured_at > price_data_as_of:
            raise OutcomeValidationError("benchmark reference snapshot is later than price_data_as_of")
        benchmark_snapshots.append(
            {
                "captured_at": captured_at,
                "price": _number(snapshot.get("price"), field="benchmark reference price", positive=True),
            }
        )
    benchmark_snapshots.sort(key=lambda item: item["captured_at"])

    signal_snapshots: Dict[str, Dict[str, Any]] = {}
    for signal_id, raw_snapshot in _mapping(root.get("signal_snapshots") or {}, field="signal_snapshots").items():
        snapshot = _mapping(raw_snapshot, field=f"signal_snapshots.{signal_id}")
        captured_at = _shanghai_datetime(snapshot.get("captured_at"), field=f"signal_snapshots.{signal_id}.captured_at")
        if captured_at > price_data_as_of:
            raise OutcomeValidationError("signal execution snapshot is later than price_data_as_of")
        signal_snapshots[str(signal_id)] = {
            "captured_at": captured_at,
            "limit_up_price": _optional_number(
                snapshot.get("limit_up_price"), field=f"signal_snapshots.{signal_id}.limit_up_price", positive=True
            ),
        }

    return {
        "price_basis": price_basis,
        "price_source": price_source,
        "calendar_source": calendar_source,
        "price_data_as_of": price_data_as_of,
        "calendar": calendar,
        "stocks": stocks,
        "benchmark_reference_snapshots": benchmark_snapshots,
        "signal_snapshots": signal_snapshots,
        "price_file_sha256": file_hash,
        "price_content_sha256": content_hash,
        "coverage_start": calendar[0],
        "coverage_end": calendar[-1],
    }


def _window_metrics(reference_price: float, bars: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    highest = max(bars, key=lambda bar: float(bar["high"]))
    lowest = min(bars, key=lambda bar: float(bar["low"]))
    max_upside = float(highest["high"]) / reference_price - 1.0
    max_adverse = min(0.0, float(lowest["low"]) / reference_price - 1.0)
    peak = reference_price
    max_drawdown = 0.0
    for bar in bars:
        close = float(bar["close"])
        peak = max(peak, close)
        max_drawdown = min(max_drawdown, close / peak - 1.0)
    return {
        "horizon_high": float(highest["high"]),
        "horizon_high_date": highest["trade_date"].isoformat(),
        "horizon_low": float(lowest["low"]),
        "horizon_low_date": lowest["trade_date"].isoformat(),
        "mfe": round(max_upside, 10),
        "mae": round(max_adverse, 10),
        "max_drawdown": round(max_drawdown, 10),
    }


def _benchmark_reference(prices: Mapping[str, Any], signal_time: datetime) -> Optional[Dict[str, Any]]:
    eligible = [
        snapshot
        for snapshot in prices["benchmark_reference_snapshots"]
        if snapshot["captured_at"] <= signal_time
    ]
    return eligible[-1] if eligible else None


def _calculate_signal(
    signal: Mapping[str, Any],
    *,
    prices: Mapping[str, Any],
    evaluation_as_of: datetime,
    calculated_at: datetime,
    calculation_version: str,
    round_trip_cost_bps: float,
) -> List[Dict[str, Any]]:
    signal_id = _text(signal.get("signal_id"), field="signal_id")
    signal_date = _iso_date(signal.get("signal_date"), field=f"{signal_id}.signal_date")
    try:
        stock_code = _normalize_stock_code(signal.get("stock_code"))
    except ValueError as exc:
        raise OutcomeValidationError(str(exc)) from exc
    stock_name = _text(signal.get("stock_name"), field=f"{signal_id}.stock_name")
    reference_price = _number(signal.get("reference_price"), field=f"{signal_id}.reference_price", positive=True)
    signal_market_time = _shanghai_datetime(signal.get("market_data_at"), field=f"{signal_id}.market_data_at")
    calendar: List[date] = prices["calendar"]
    future_dates = [day for day in calendar if day > signal_date]
    stock = prices["stocks"].get(stock_code, {"bars": {}, "corporate_actions": {}, "data_conflicts": {}})
    signal_near_limit_up: Optional[bool] = None
    signal_snapshot = prices["signal_snapshots"].get(signal_id) or prices["signal_snapshots"].get(stock_code)
    if signal_snapshot and signal_snapshot["captured_at"] <= signal_market_time:
        if signal_snapshot.get("limit_up_price") is not None:
            signal_near_limit_up = reference_price >= float(signal_snapshot["limit_up_price"]) * 0.99
    benchmark_reference = _benchmark_reference(prices, signal_market_time)
    benchmark = prices["stocks"].get(
        BENCHMARK_CODE, {"bars": {}, "corporate_actions": {}, "data_conflicts": {}}
    )

    results: List[Dict[str, Any]] = []
    for horizon in HORIZONS:
        target_date = future_dates[horizon - 1] if len(future_dates) >= horizon else None
        mature = target_date is not None and _session_is_closed(target_date, as_of=evaluation_as_of)
        intended_dates = future_dates[:horizon]
        elapsed_dates = [day for day in intended_dates if _session_is_closed(day, as_of=evaluation_as_of)]
        available_dates = [day for day in elapsed_dates if _session_is_closed(day, as_of=prices["price_data_as_of"])]
        valid_bars = [
            stock["bars"][day]
            for day in available_dates
            if day in stock["bars"] and not stock["bars"][day]["is_suspended"]
        ]
        missing_days = len(elapsed_dates) - len(valid_bars)
        suspended_days = sum(
            1 for day in available_dates if day in stock["bars"] and stock["bars"][day]["is_suspended"]
        )
        target_bar = stock["bars"].get(target_date) if target_date is not None else None
        target_is_available = target_date in available_dates if target_date is not None else False
        window_conflicts = [
            reason
            for day in intended_dates
            for reason in stock["data_conflicts"].get(day, [])
            if day in available_dates
        ]
        window_actions = [
            {"trade_date": day.isoformat(), "action_type": action_type}
            for day in intended_dates
            for action_type in stock["corporate_actions"].get(day, [])
            if day in available_dates
        ]

        status = "complete"
        reason: Optional[str] = None
        if not mature:
            status = "pending"
            reason = "calendar_not_covered_to_horizon" if target_date is None else "target_session_not_closed"
        elif window_conflicts:
            status = "data_conflict"
            reason = "; ".join(window_conflicts)
        elif window_actions:
            status = "corporate_action_review"
            reason = "unadjusted OHLC window contains a corporate action"
        elif not target_is_available or target_bar is None:
            status = "missing_price"
            reason = "target trading date has no finalized price row"
        elif target_bar["is_suspended"]:
            status = "suspended"
            reason = "target trading date has no transaction"
        elif missing_days:
            status = "missing_price"
            reason = "evaluation window has missing or suspended price days"

        horizon_close: Optional[float] = None
        gross_return: Optional[float] = None
        net_return: Optional[float] = None
        target_limit_up: Optional[bool] = None
        target_limit_down: Optional[bool] = None
        if mature and target_is_available and target_bar is not None and not target_bar["is_suspended"] and not window_conflicts:
            horizon_close = float(target_bar["close"])
            gross_return = round(horizon_close / reference_price - 1.0, 10)
            net_return = round(
                (horizon_close / reference_price) * (1.0 - round_trip_cost_bps / 10000.0) - 1.0,
                10,
            )
            target_limit_up = target_bar.get("is_limit_up")
            target_limit_down = target_bar.get("is_limit_down")

        path_metrics: Dict[str, Any] = {
            "horizon_high": None,
            "horizon_high_date": None,
            "horizon_low": None,
            "horizon_low_date": None,
            "mfe": None,
            "mae": None,
            "max_drawdown": None,
        }
        if mature and valid_bars and not window_conflicts:
            path_metrics = _window_metrics(reference_price, valid_bars)

        benchmark_status = "unavailable"
        benchmark_reason = "no benchmark snapshot at or before signal market-data time"
        benchmark_reference_price: Optional[float] = None
        benchmark_reference_at: Optional[str] = None
        benchmark_close: Optional[float] = None
        benchmark_return: Optional[float] = None
        excess_return: Optional[float] = None
        if benchmark_reference is not None:
            benchmark_reference_price = float(benchmark_reference["price"])
            benchmark_reference_at = benchmark_reference["captured_at"].isoformat(timespec="seconds")
            benchmark_bar = benchmark["bars"].get(target_date) if target_date else None
            if not mature:
                benchmark_status = "pending"
                benchmark_reason = "target session not closed"
            elif target_date not in available_dates or benchmark_bar is None or benchmark_bar.get("is_suspended"):
                benchmark_reason = "benchmark target close unavailable"
            elif benchmark["data_conflicts"].get(target_date, []):
                benchmark_status = "data_conflict"
                benchmark_reason = "benchmark source conflict"
            else:
                benchmark_close = float(benchmark_bar["close"])
                benchmark_return = round(benchmark_close / benchmark_reference_price - 1.0, 10)
                benchmark_status = "complete"
                benchmark_reason = None
                if gross_return is not None:
                    excess_return = round(gross_return - benchmark_return, 10)

        execution_risks: List[str] = []
        if signal_near_limit_up:
            execution_risks.append("signal_price_near_limit_up")
        if target_limit_up:
            execution_risks.append("target_closed_at_limit_up")
        if target_limit_down:
            execution_risks.append("target_closed_at_limit_down")
        if status == "suspended":
            execution_risks.append("target_suspended")
        if status == "missing_price":
            execution_risks.append("incomplete_price_window")

        results.append(
            {
                "outcome_id": build_outcome_id(signal_id, horizon, calculation_version),
                "signal_id": signal_id,
                "signal_date": signal_date.isoformat(),
                "stock_code": stock_code,
                "stock_name": stock_name,
                "reference_price": reference_price,
                "horizon_days": horizon,
                "horizon_trade_date": target_date.isoformat() if target_date else None,
                "horizon_close": horizon_close,
                "gross_return": gross_return,
                "net_return": net_return,
                "round_trip_cost_bps": round_trip_cost_bps,
                **path_metrics,
                "valid_market_days": len(valid_bars),
                "missing_price_days": missing_days,
                "suspended_market_days": suspended_days,
                "outcome_status": status,
                "outcome_status_reason": reason,
                "target_is_limit_up": target_limit_up,
                "target_is_limit_down": target_limit_down,
                "signal_near_limit_up": signal_near_limit_up,
                "execution_risks": execution_risks,
                "corporate_actions": window_actions,
                "benchmark_code": BENCHMARK_CODE,
                "benchmark_status": benchmark_status,
                "benchmark_status_reason": benchmark_reason,
                "benchmark_reference_price": benchmark_reference_price,
                "benchmark_reference_at": benchmark_reference_at,
                "benchmark_close": benchmark_close,
                "benchmark_return": benchmark_return,
                "excess_return": excess_return,
                "calculated_at": calculated_at.isoformat(timespec="seconds"),
                "price_data_as_of": prices["price_data_as_of"].isoformat(timespec="seconds"),
                "price_source": prices["price_source"],
                "price_basis": prices["price_basis"],
                "price_file_sha256": prices["price_file_sha256"],
                "calculation_version": calculation_version,
                "source_signal_archive": signal.get("source_signal_archive"),
            }
        )
    return results


def _outcome_content(
    records: Sequence[Mapping[str, Any]],
    *,
    evaluation_as_of: datetime,
    calculation_version: str,
    signal_input_sha256: str,
    price_file_sha256: str,
    round_trip_cost_bps: float,
) -> Dict[str, Any]:
    return {
        "archive_schema_version": OUTCOME_SCHEMA_VERSION,
        "evaluation_as_of": evaluation_as_of.isoformat(timespec="seconds"),
        "calculation_version": calculation_version,
        "signal_input_sha256": signal_input_sha256,
        "price_file_sha256": price_file_sha256,
        "round_trip_cost_bps": round_trip_cost_bps,
        "outcomes": [{key: value for key, value in record.items() if key != "calculated_at"} for record in records],
    }


def _target_directory(
    output_root: Path,
    *,
    evaluation_as_of: datetime,
    calculation_version: str,
    signal_input_sha256: str,
    price_file_sha256: str,
    round_trip_cost_bps: float,
) -> Path:
    identity = _canonical_json_bytes(
        {
            "evaluation_as_of": evaluation_as_of.isoformat(timespec="seconds"),
            "calculation_version": calculation_version,
            "signal_input_sha256": signal_input_sha256,
            "price_file_sha256": price_file_sha256,
            "round_trip_cost_bps": round_trip_cost_bps,
        }
    )
    batch_key = _sha256_bytes(identity)[:16]
    return (
        output_root
        / f"{evaluation_as_of.year:04d}"
        / f"{evaluation_as_of.month:02d}"
        / f"{evaluation_as_of.day:02d}"
        / f"batch-{batch_key}"
    )


def _existing_result(
    target: Path,
    *,
    content_hash: str,
    outcome_ids: Sequence[str],
    evaluation_as_of: datetime,
    calculation_version: str,
    signal_input_sha256: str,
    price_file_sha256: str,
    round_trip_cost_bps: float,
) -> OutcomeResult:
    manifest_path = target / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutcomeConflictError(f"existing outcome manifest cannot be verified: {target}") from exc
    if manifest.get("content_hash") != content_hash or manifest.get("outcome_ids") != list(outcome_ids):
        raise OutcomeConflictError(f"immutable outcome conflict; original preserved at {target}")
    files = manifest.get("files")
    if not isinstance(files, Mapping) or set(files) != {"outcomes.json", "outcomes.parquet"}:
        raise OutcomeConflictError(f"existing outcome file hashes are invalid: {target}")
    for name, expected_hash in files.items():
        path = target / name
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected_hash:
            raise OutcomeConflictError(f"existing outcome file hash mismatch; original preserved: {path}")
    try:
        payload = json.loads((target / "outcomes.json").read_text(encoding="utf-8"))
        records = payload["outcomes"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OutcomeConflictError(f"existing outcome payload cannot be verified: {target}") from exc
    if not isinstance(records, list) or any(not isinstance(record, Mapping) for record in records):
        raise OutcomeConflictError(f"existing outcome payload has invalid records: {target}")
    recomputed_hash = _sha256_bytes(
        _canonical_json_bytes(
            _outcome_content(
                records,
                evaluation_as_of=evaluation_as_of,
                calculation_version=calculation_version,
                signal_input_sha256=signal_input_sha256,
                price_file_sha256=price_file_sha256,
                round_trip_cost_bps=round_trip_cost_bps,
            )
        )
    )
    if (
        recomputed_hash != content_hash
        or manifest.get("outcome_count") != len(records)
        or [record.get("outcome_id") for record in records] != list(outcome_ids)
    ):
        raise OutcomeConflictError(f"existing outcome manifest is inconsistent; original preserved at {target}")
    return OutcomeResult(
        status="exists",
        archive_dir=target,
        outcome_count=int(manifest.get("outcome_count", 0)),
        outcome_ids=tuple(outcome_ids),
        content_hash=content_hash,
    )


def calculate_outcomes(
    signals_root: Path | str,
    price_source: Mapping[str, Any],
    *,
    price_file_sha256: str,
    output_root: Path | str,
    as_of: str | datetime,
    calculation_version: str = DEFAULT_CALCULATION_VERSION,
    round_trip_cost_bps: float = DEFAULT_ROUND_TRIP_COST_BPS,
    calculated_at: Optional[datetime] = None,
    parquet_writer: Callable[[Path, Sequence[Mapping[str, Any]]], None] = _write_outcomes_parquet,
) -> OutcomeResult:
    """Calculate immutable outcomes without mutating the phase-1 signal archive."""
    evaluation_as_of = _shanghai_datetime(as_of, field="as_of")
    version = _text(calculation_version, field="calculation_version")
    cost_bps = _number(round_trip_cost_bps, field="round_trip_cost_bps")
    if cost_bps < 0 or cost_bps >= 10000:
        raise OutcomeValidationError("round_trip_cost_bps must be between 0 and 10000")
    calculation_time = calculated_at or datetime.now(SHANGHAI_TZ)
    if calculation_time.tzinfo is None or calculation_time.utcoffset() is None:
        raise OutcomeValidationError("calculated_at must include a timezone offset")
    calculation_time = calculation_time.astimezone(SHANGHAI_TZ)
    if calculation_time < evaluation_as_of:
        raise OutcomeValidationError("calculated_at cannot be earlier than as_of")

    signals, signal_inputs, signal_input_hash = _load_signal_archives(Path(signals_root))
    prices = _normalize_price_artifact(
        price_source,
        source_file_sha256=price_file_sha256,
        evaluation_as_of=evaluation_as_of,
    )
    records = [
        record
        for signal in signals
        for record in _calculate_signal(
            signal,
            prices=prices,
            evaluation_as_of=evaluation_as_of,
            calculated_at=calculation_time,
            calculation_version=version,
            round_trip_cost_bps=cost_bps,
        )
    ]
    outcome_ids = [record["outcome_id"] for record in records]
    if len(outcome_ids) != len(set(outcome_ids)):
        raise OutcomeValidationError("duplicate outcome identity")

    content = _outcome_content(
        records,
        evaluation_as_of=evaluation_as_of,
        calculation_version=version,
        signal_input_sha256=signal_input_hash,
        price_file_sha256=prices["price_file_sha256"],
        round_trip_cost_bps=cost_bps,
    )
    content_hash = _sha256_bytes(_canonical_json_bytes(content))
    target = _target_directory(
        Path(output_root),
        evaluation_as_of=evaluation_as_of,
        calculation_version=version,
        signal_input_sha256=signal_input_hash,
        price_file_sha256=prices["price_file_sha256"],
        round_trip_cost_bps=cost_bps,
    )
    if target.exists():
        return _existing_result(
            target,
            content_hash=content_hash,
            outcome_ids=outcome_ids,
            evaluation_as_of=evaluation_as_of,
            calculation_version=version,
            signal_input_sha256=signal_input_hash,
            price_file_sha256=prices["price_file_sha256"],
            round_trip_cost_bps=cost_bps,
        )

    payload = {
        "archive_schema_version": OUTCOME_SCHEMA_VERSION,
        "evaluation_as_of": evaluation_as_of.isoformat(timespec="seconds"),
        "calculated_at": calculation_time.isoformat(timespec="seconds"),
        "calculation_version": version,
        "round_trip_cost_bps": cost_bps,
        "cost_scenario_note": "Scenario estimate only; it does not represent executable profit.",
        "signal_inputs": signal_inputs,
        "signal_input_sha256": signal_input_hash,
        "price_metadata": {
            "price_source": prices["price_source"],
            "price_basis": prices["price_basis"],
            "calendar_source": prices["calendar_source"],
            "price_data_as_of": prices["price_data_as_of"].isoformat(timespec="seconds"),
            "coverage_start": prices["coverage_start"].isoformat(),
            "coverage_end": prices["coverage_end"].isoformat(),
            "price_file_sha256": prices["price_file_sha256"],
            "price_content_sha256": prices["price_content_sha256"],
        },
        "outcomes": records,
    }
    json_bytes = _strict_json_bytes(payload)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        json_path = temp_dir / "outcomes.json"
        parquet_path = temp_dir / "outcomes.parquet"
        json_path.write_bytes(json_bytes)
        parquet_writer(parquet_path, records)
        if not parquet_path.is_file():
            raise RuntimeError("parquet writer did not create outcomes.parquet")
        manifest = {
            "archive_schema_version": OUTCOME_SCHEMA_VERSION,
            "evaluation_as_of": evaluation_as_of.isoformat(timespec="seconds"),
            "calculated_at": calculation_time.isoformat(timespec="seconds"),
            "calculation_version": version,
            "round_trip_cost_bps": cost_bps,
            "cost_scenario_note": "Scenario estimate only; it does not represent executable profit.",
            "outcome_count": len(records),
            "outcome_ids": outcome_ids,
            "content_hash": content_hash,
            "signal_inputs": signal_inputs,
            "signal_input_sha256": signal_input_hash,
            "price_source": prices["price_source"],
            "price_basis": prices["price_basis"],
            "price_data_as_of": prices["price_data_as_of"].isoformat(timespec="seconds"),
            "price_coverage_start": prices["coverage_start"].isoformat(),
            "price_coverage_end": prices["coverage_end"].isoformat(),
            "price_file_sha256": prices["price_file_sha256"],
            "price_content_sha256": prices["price_content_sha256"],
            "files": {
                "outcomes.json": _sha256_bytes(json_path.read_bytes()),
                "outcomes.parquet": _sha256_bytes(parquet_path.read_bytes()),
            },
        }
        (temp_dir / "manifest.json").write_bytes(_strict_json_bytes(manifest))
        try:
            os.rename(temp_dir, target)
        except OSError:
            if target.exists():
                existing = _existing_result(
                    target,
                    content_hash=content_hash,
                    outcome_ids=outcome_ids,
                    evaluation_as_of=evaluation_as_of,
                    calculation_version=version,
                    signal_input_sha256=signal_input_hash,
                    price_file_sha256=prices["price_file_sha256"],
                    round_trip_cost_bps=cost_bps,
                )
                shutil.rmtree(temp_dir, ignore_errors=True)
                return existing
            raise
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return OutcomeResult(
        status="created",
        archive_dir=target,
        outcome_count=len(records),
        outcome_ids=tuple(outcome_ids),
        content_hash=content_hash,
    )
