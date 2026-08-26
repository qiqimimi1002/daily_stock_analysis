"""Manual after-close preparation and next-morning read-only quote handoff."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time as clock
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from research.benchmarks.qlib_doubleensemble import build_qlib_provider
from research.benchmarks.qlib_shadow import (
    FROZEN_MODEL_VERSION,
    QlibShadowConflictError,
    QlibShadowError,
    provider_tree_sha256,
)
from research.benchmarks.schema import canonical_json_bytes
from research.data_sources.trade_calendar import fetch_verified_trade_calendar
from src.services.market_screener import is_excluded_name, is_main_board_code


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
AFTER_CLOSE_READY_TIME = time(16, 30)
RAW_SOURCE_ID = "baostock.query_history_k_data_plus.adjustflag_3"
RAW_FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "pctChg",
    "tradestatus",
    "isST",
)
DAILY_READY_SCHEMA_VERSION = "qlib-doubleensemble-nightly-ready-v1"
PRIVATE_REPOSITORY = "qiqimimi1002/daily-stock-tushare-private"
PRIVATE_WORKFLOW = "tushare-readonly-acceptance.yml"
PRIVATE_CONFIRMATION = "PERSONAL_PRIVATE_READ_ONLY"
REFRESH_STATE_SCHEMA_VERSION = "qlib-raw-refresh-state-v1"
BAOSTOCK_REQUEST_TIMEOUT_SECONDS = 15.0
BAOSTOCK_MAX_ATTEMPTS = 2
BAOSTOCK_MAX_FAILURES = 5
STOCK_PROGRESS_INTERVAL = 50


class QlibDailyError(QlibShadowError):
    """Raised when the manual daily preparation contract is not satisfied."""


class _BaostockSocketGuard:
    """Keep Baostock's receive loop bounded when the peer closes the socket."""

    def __init__(self, wrapped: Any):
        self.wrapped = wrapped

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
        value = self.wrapped.recv(*args, **kwargs)
        if value == b"":
            raise ConnectionError("Baostock socket closed before response terminator")
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)


def _canonical_date(value: date | str, *, field: str) -> date:
    if isinstance(value, datetime):
        raise QlibDailyError(f"{field} must be a date")
    text = value.isoformat() if isinstance(value, date) else str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise QlibDailyError(f"{field} must be canonical YYYY-MM-DD") from None
    if text != parsed.isoformat():
        raise QlibDailyError(f"{field} must be canonical YYYY-MM-DD")
    return parsed


def _now(value: datetime | None = None) -> datetime:
    observed = value or datetime.now(SHANGHAI_TZ)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise QlibDailyError("now must include a timezone")
    return observed.astimezone(SHANGHAI_TZ)


def _strict_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _manifest_with_hash(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["manifest_sha256"] = _content_sha256(result)
    return result


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QlibDailyError(f"{label} cannot be read") from exc
    if not isinstance(value, dict):
        raise QlibDailyError(f"{label} must be an object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_refresh_state(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, _strict_json_bytes(value))


def _append_refresh_event(
    staging: Path,
    event: Mapping[str, Any],
    event_sink: Callable[[Mapping[str, Any]], None] | None,
) -> None:
    payload = {
        "observed_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="milliseconds"),
        "pid": os.getpid(),
        **dict(event),
    }
    with (staging / "refresh-events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
        handle.flush()
    if event_sink is not None:
        event_sink(payload)


def _source_target(runtime_root: Path, completed_date: date) -> Path:
    return runtime_root / f"raw-through-{completed_date:%Y%m%d}"


def _ready_target(ready_root: Path, trade_date: date) -> Path:
    return (
        ready_root
        / f"{trade_date.year:04d}"
        / f"{trade_date.month:02d}"
        / f"{trade_date.day:02d}"
        / "doubleensemble-nightly-ready-v1"
    )


def _read_raw_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str)
    if frame.empty:
        raise QlibDailyError(f"raw symbol file is empty: {path.name}")
    return frame


def _read_last_raw_row(path: Path) -> pd.Series:
    return _read_raw_frame(path).iloc[-1]


def _number_or_none(value: Any) -> float | None:
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return None if pd.isna(parsed) else float(parsed)


def _eligible_target_record(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_code = row.get("code")
    if raw_code is None:
        raw_code = row.get("stock_code")
    raw_name = row.get("name")
    if raw_name is None:
        raw_name = row.get("stock_name")
    code = str(raw_code or "").split(".")[-1]
    return {
        "amount": _number_or_none(row.get("amount")),
        "close": _number_or_none(row.get("close")),
        "pctChg": _number_or_none(row.get("pctChg")),
        "preclose": _number_or_none(row.get("preclose")),
        "stock_code": code,
        "stock_name": str(raw_name or "").strip(),
        "turn": _number_or_none(row.get("turn")),
        "volume": _number_or_none(row.get("volume")),
    }


def inspect_completed_source(
    source_dir: Path | str,
    completed_date: date | str,
) -> dict[str, Any]:
    """Prove exact file/universe coverage for one completed daily snapshot."""

    target = _canonical_date(completed_date, field="completed_date")
    source = Path(source_dir).resolve()
    manifest = _load_json(source / "manifest.json", label="source manifest")
    if manifest.get("status") != "complete":
        raise QlibDailyError("source manifest status is not complete")
    if manifest.get("collection_end") != target.isoformat():
        raise QlibDailyError("source collection_end must equal the completed date")
    if manifest.get("failure_count") != 0 or manifest.get("failures") not in ([], None):
        raise QlibDailyError("source refresh failure_count must be zero")

    calendar = pd.read_csv(source / "calendar.csv", dtype=str)
    expected_calendar_columns = {"calendar_date", "is_trading_day"}
    if set(calendar.columns) != expected_calendar_columns:
        raise QlibDailyError("source calendar schema differs")
    target_rows = calendar.loc[calendar["calendar_date"].eq(target.isoformat())]
    if len(target_rows) != 1 or target_rows.iloc[0]["is_trading_day"] != "1":
        raise QlibDailyError("completed date is not one verified trading session")

    files = sorted((source / "symbols").glob("*.csv"))
    expected_files = manifest.get("completed_symbol_file_count")
    if not files or expected_files != len(files):
        raise QlibDailyError("source universe/file coverage differs from manifest")
    active = 0
    inactive = 0
    eligible_target_records = []
    required_active = ("open", "high", "low", "close", "volume", "amount")
    for path in files:
        frame = _read_raw_frame(path)
        row = frame.iloc[-1]
        if str(row.get("date") or "") != target.isoformat():
            raise QlibDailyError(f"source file does not end at completed date: {path.name}")
        status = str(row.get("tradestatus") or "")
        if status not in {"0", "1"}:
            raise QlibDailyError(f"source trading status differs: {path.name}")
        if status == "1":
            numeric = pd.to_numeric(row.loc[list(required_active)], errors="coerce")
            if numeric.isna().any() or (numeric.loc[["open", "high", "low", "close"]] <= 0).any():
                raise QlibDailyError(f"active completed row is incomplete: {path.name}")
            active += 1
            code = str(row.get("code") or "").split(".")[-1]
            if (
                is_main_board_code(code)
                and str(row.get("isST") or "") == "0"
                and not is_excluded_name(row.get("name"))
            ):
                for column in ("close", "volume", "amount"):
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                complete_metrics = pd.DataFrame(
                    {
                        "ma5": frame["close"].rolling(5).mean(),
                        "ma10": frame["close"].rolling(10).mean(),
                        "ma20": frame["close"].rolling(20).mean(),
                        "five_day_pct": frame["close"] / frame["close"].shift(5),
                        "volume_ratio_5d": frame["volume"]
                        / (frame["volume"].shift(1).rolling(5).mean() + 1e-9),
                        "avg_amount_20d": frame["amount"].rolling(20).mean(),
                    }
                ).iloc[-1]
                if complete_metrics.notna().all():
                    eligible_target_records.append(_eligible_target_record(row))
        else:
            inactive += 1
    eligible_target_records.sort(key=lambda item: item["stock_code"])
    return {
        "active_file_count": active,
        "completed_date": target.isoformat(),
        "eligible_target_count": len(eligible_target_records),
        "eligible_target_sha256": _content_sha256(eligible_target_records),
        "failure_count": 0,
        "inactive_file_count": inactive,
        "source_dir": str(source),
        "source_manifest_file_sha256": _file_sha256(source / "manifest.json"),
        "symbol_file_count": len(files),
        "target_row_coverage_count": len(files),
    }


def _latest_prior_source(runtime_root: Path, target: date) -> Path:
    candidates: list[tuple[date, Path]] = []
    for path in runtime_root.glob("raw-through-????????"):
        if not path.is_dir():
            continue
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _load_json(manifest_path, label="source manifest")
        try:
            collection_end = _canonical_date(
                manifest.get("collection_end"), field="collection_end"
            )
        except QlibDailyError:
            continue
        if manifest.get("status") == "complete" and collection_end < target:
            candidates.append((collection_end, path.resolve()))
    if not candidates:
        raise QlibDailyError("no earlier complete raw-through snapshot is available")
    return max(candidates, key=lambda item: item[0])[1]


def _verified_calendar_rows(target: date) -> tuple[pd.DataFrame, dict[str, Any]]:
    query_end = target + timedelta(days=14)
    verified = fetch_verified_trade_calendar(target, query_end, allow_network=True)
    trading_dates = {item.isoformat() for item in verified.trading_dates}
    if target.isoformat() not in trading_dates:
        raise QlibDailyError("requested completed date is not a verified trading day")
    rows = []
    current = target
    while current <= query_end:
        rows.append(
            {
                "calendar_date": current.isoformat(),
                "is_trading_day": "1" if current.isoformat() in trading_dates else "0",
            }
        )
        current += timedelta(days=1)
    return pd.DataFrame(rows), verified.to_dict()


def _baostock_login(bs: Any, bs_context: Any, timeout_seconds: float) -> None:
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_seconds)
    try:
        login = bs.login()
    except Exception as exc:
        raise QlibDailyError(f"Baostock login failed: {type(exc).__name__}") from None
    finally:
        socket.setdefaulttimeout(previous_timeout)
    if getattr(login, "error_code", None) != "0":
        raise QlibDailyError("Baostock login failed")
    active_socket = getattr(bs_context, "default_socket", None)
    if active_socket is None:
        raise QlibDailyError("Baostock login did not create a socket")
    active_socket.settimeout(timeout_seconds)
    setattr(bs_context, "default_socket", _BaostockSocketGuard(active_socket))


def _baostock_disconnect(bs: Any, bs_context: Any, *, logout: bool) -> None:
    active_socket = getattr(bs_context, "default_socket", None)
    if logout:
        try:
            bs.logout()
        except Exception:
            pass
    if active_socket is not None:
        try:
            active_socket.close()
        except Exception:
            pass
    setattr(bs_context, "default_socket", None)


def _validate_baostock_result(result: Any, symbol: str, target: date) -> dict[str, str]:
    error_code = getattr(result, "error_code", None)
    if result is None or error_code != "0":
        raise QlibDailyError(f"query_error_code={error_code or 'missing'}")
    actual_fields = tuple(getattr(result, "fields", ()) or ())
    if actual_fields != RAW_FIELDS:
        raise QlibDailyError("query_fields_mismatch")
    values = []
    while result.next():
        values.append(dict(zip(actual_fields, result.get_row_data())))
    terminal_error_code = getattr(result, "error_code", None)
    if terminal_error_code != "0":
        raise QlibDailyError(
            f"query_terminal_error_code={terminal_error_code or 'missing'}"
        )
    if len(values) != 1:
        raise QlibDailyError(f"query_row_count={len(values)}")
    row = values[0]
    if (
        row.get("date") != target.isoformat()
        or row.get("code") != symbol
        or row.get("adjustflag") != "3"
        or row.get("tradestatus") not in {"0", "1"}
    ):
        raise QlibDailyError("query_row_contract_mismatch")
    return row


def _baostock_rows(
    symbols: Sequence[str],
    target: date,
    *,
    on_success: Callable[[str, Mapping[str, str], int, float], None],
    event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    request_timeout_seconds: float = BAOSTOCK_REQUEST_TIMEOUT_SECONDS,
    max_attempts: int = BAOSTOCK_MAX_ATTEMPTS,
    max_failures: int = BAOSTOCK_MAX_FAILURES,
    baostock_module: Any | None = None,
    baostock_context: Any | None = None,
) -> dict[str, Any]:
    if request_timeout_seconds <= 0:
        raise QlibDailyError("Baostock request timeout must be positive")
    if max_attempts < 1 or max_failures < 1:
        raise QlibDailyError("Baostock attempt and failure limits must be positive")
    if not symbols:
        return {"completed_count": 0, "failure_count": 0, "retry_count": 0}
    try:
        if baostock_module is None:
            import baostock as baostock_module
        if baostock_context is None:
            import baostock.common.context as baostock_context
    except Exception as exc:
        raise QlibDailyError(f"Baostock import failed: {type(exc).__name__}") from None

    fields = ",".join(RAW_FIELDS)
    failures: list[str] = []
    retry_count = 0
    completed_count = 0
    _baostock_login(baostock_module, baostock_context, request_timeout_seconds)
    try:
        for symbol in symbols:
            symbol_started = clock.perf_counter()
            succeeded = False
            for attempt in range(1, max_attempts + 1):
                attempt_started = clock.perf_counter()
                try:
                    result = baostock_module.query_history_k_data_plus(
                        symbol,
                        fields,
                        start_date=target.isoformat(),
                        end_date=target.isoformat(),
                        frequency="d",
                        adjustflag="3",
                    )
                    row = _validate_baostock_result(result, symbol, target)
                except Exception as exc:
                    reason = (
                        str(exc)
                        if isinstance(exc, QlibDailyError)
                        else type(exc).__name__
                    )
                    duration = round(clock.perf_counter() - attempt_started, 4)
                    if attempt < max_attempts:
                        retry_count += 1
                        if event_sink is not None:
                            event_sink(
                                {
                                    "event": "stock_retry",
                                    "symbol": symbol,
                                    "attempt": attempt,
                                    "duration_seconds": duration,
                                    "reason": reason,
                                }
                            )
                        _baostock_disconnect(
                            baostock_module, baostock_context, logout=False
                        )
                        _baostock_login(
                            baostock_module,
                            baostock_context,
                            request_timeout_seconds,
                        )
                        continue
                    failures.append(symbol)
                    if event_sink is not None:
                        event_sink(
                            {
                                "event": "stock_failed",
                                "symbol": symbol,
                                "attempts": attempt,
                                "duration_seconds": round(
                                    clock.perf_counter() - symbol_started, 4
                                ),
                                "reason": reason,
                            }
                        )
                    break
                completed_count += 1
                on_success(
                    symbol,
                    row,
                    attempt,
                    round(clock.perf_counter() - symbol_started, 4),
                )
                succeeded = True
                break
            if not succeeded:
                _baostock_disconnect(baostock_module, baostock_context, logout=False)
                if len(failures) >= max_failures:
                    break
                _baostock_login(
                    baostock_module, baostock_context, request_timeout_seconds
                )
    finally:
        _baostock_disconnect(baostock_module, baostock_context, logout=True)
    if failures:
        raise QlibDailyError(
            "daily refresh failed closed: "
            f"failure_count={len(failures)}, retry_count={retry_count}, "
            f"first_codes={failures[:5]}"
        )
    return {
        "completed_count": completed_count,
        "failure_count": 0,
        "retry_count": retry_count,
    }


def _staging_target(runtime_root: Path, target: date) -> Path:
    return runtime_root / f".raw-through-{target:%Y%m%d}-staging"


def _staged_symbol_entries(
    staging: Path,
    *,
    prior_end: date,
    target: date,
) -> tuple[dict[str, tuple[Path, str]], set[str]]:
    entries: dict[str, tuple[Path, str]] = {}
    completed: set[str] = set()
    for path in sorted((staging / "symbols").glob("*.csv")):
        frame = _read_raw_frame(path)
        last_row = frame.iloc[-1]
        symbol = str(last_row.get("code") or "").strip()
        if not symbol or symbol.split(".")[-1] != path.stem or symbol in entries:
            raise QlibDailyError(f"staging symbol identity differs: {path.name}")
        last_date = str(last_row.get("date") or "")
        if last_date == target.isoformat():
            if int(frame["date"].eq(target.isoformat()).sum()) != 1:
                raise QlibDailyError(f"staging target row is duplicated: {path.name}")
            if (
                str(last_row.get("price_basis") or "") != "raw_unadjusted"
                or str(last_row.get("source_id") or "") != RAW_SOURCE_ID
                or str(last_row.get("tradestatus") or "") not in {"0", "1"}
            ):
                raise QlibDailyError(f"staging completed row differs: {path.name}")
            completed.add(symbol)
        elif last_date != prior_end.isoformat():
            raise QlibDailyError(f"staging cutoff differs: {path.name}")
        entries[symbol] = (path, str(last_row.get("name") or "").strip())
    return entries, completed


def _append_staged_row_atomic(
    path: Path,
    *,
    symbol: str,
    name: str,
    row: Mapping[str, str],
) -> dict[str, str]:
    output = {
        "date": row["date"],
        "code": row["code"],
        "name": name,
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "preclose": row["preclose"],
        "volume": row["volume"],
        "amount": row["amount"],
        "price_basis": "raw_unadjusted",
        "source_id": RAW_SOURCE_ID,
        "turn": row["turn"],
        "pctChg": row["pctChg"],
        "tradestatus": row["tradestatus"],
        "isST": row["isST"],
    }
    frame = _read_raw_frame(path)
    if str(frame.iloc[-1].get("date") or "") >= row["date"]:
        raise QlibDailyError(f"raw history append is not monotonic: {path.name}")
    combined = pd.concat([frame, pd.DataFrame([output])], ignore_index=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        combined.to_csv(temporary, index=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if symbol != output["code"]:
        raise QlibDailyError(f"Baostock symbol identity differs: {path.name}")
    return output


def refresh_completed_source(
    *,
    runtime_root: Path | str,
    completed_date: date | str,
    observed_at: datetime | None = None,
    event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    request_timeout_seconds: float = BAOSTOCK_REQUEST_TIMEOUT_SECONDS,
    max_attempts: int = BAOSTOCK_MAX_ATTEMPTS,
    max_failures: int = BAOSTOCK_MAX_FAILURES,
) -> dict[str, Any]:
    """Create or verify one immutable raw-through-T full-market snapshot."""

    started = clock.perf_counter()
    target_date = _canonical_date(completed_date, field="completed_date")
    root = Path(runtime_root).resolve()
    now = _now(observed_at)
    if target_date > now.date():
        raise QlibDailyError("future daily data cannot be prepared")
    if target_date == now.date() and now.time() < AFTER_CLOSE_READY_TIME:
        raise QlibDailyError("today is not eligible until 16:30 Asia/Shanghai")
    target = _source_target(root, target_date)
    if target.exists():
        inspection = inspect_completed_source(target, target_date)
        return {
            **inspection,
            "operation_status": "exists",
            "resumed_symbol_count": 0,
            "retry_count": 0,
            "stage_seconds": {},
            "refresh_seconds": round(clock.perf_counter() - started, 4),
        }

    stage_seconds: dict[str, float] = {}
    stage_started = clock.perf_counter()
    prior = _latest_prior_source(root, target_date)
    prior_manifest = _load_json(prior / "manifest.json", label="source manifest")
    prior_end = _canonical_date(prior_manifest.get("collection_end"), field="collection_end")
    prior_manifest_sha256 = _file_sha256(prior / "manifest.json")
    stage_seconds["prior_source"] = round(clock.perf_counter() - stage_started, 4)

    stage_started = clock.perf_counter()
    calendar_rows, calendar_evidence = _verified_calendar_rows(target_date)
    stage_seconds["verified_calendar"] = round(clock.perf_counter() - stage_started, 4)

    temporary = _staging_target(root, target_date)
    state_path = temporary / "refresh-state.json"
    operation_status = "created"
    stage_started = clock.perf_counter()
    if temporary.exists():
        state = _load_json(state_path, label="refresh state")
        expected_state = {
            "schema_version": REFRESH_STATE_SCHEMA_VERSION,
            "target_date": target_date.isoformat(),
            "prior_manifest_sha256": prior_manifest_sha256,
        }
        if any(state.get(key) != value for key, value in expected_state.items()):
            raise QlibDailyError("existing refresh staging identity differs")
        operation_status = "resumed"
    else:
        shutil.copytree(prior, temporary)
        calendar = pd.read_csv(temporary / "calendar.csv", dtype=str)
        calendar = pd.concat([calendar, calendar_rows], ignore_index=True)
        calendar = calendar.drop_duplicates("calendar_date", keep="last").sort_values(
            "calendar_date"
        )
        calendar.to_csv(temporary / "calendar.csv", index=False)
        (temporary / "verified-calendar.json").write_bytes(
            _strict_json_bytes(calendar_evidence)
        )
        state = {
            "schema_version": REFRESH_STATE_SCHEMA_VERSION,
            "status": "incomplete",
            "target_date": target_date.isoformat(),
            "prior_manifest_sha256": prior_manifest_sha256,
            "expected_symbol_file_count": int(
                prior_manifest["completed_symbol_file_count"]
            ),
            "completed_symbol_count": 0,
            "failure_count": 0,
            "retry_count": 0,
            "last_symbol": None,
        }
        _write_refresh_state(state_path, state)
    stage_seconds["staging_prepare"] = round(clock.perf_counter() - stage_started, 4)

    def emit(event: Mapping[str, Any]) -> None:
        _append_refresh_event(temporary, event, event_sink)

    emit(
        {
            "event": "stage_complete",
            "stage": "staging_prepare",
            "duration_seconds": stage_seconds["staging_prepare"],
            "operation_status": operation_status,
        }
    )
    for stage in ("prior_source", "verified_calendar"):
        emit(
            {
                "event": "stage_complete",
                "stage": stage,
                "duration_seconds": stage_seconds[stage],
            }
        )

    try:
        symbol_files = sorted((temporary / "symbols").glob("*.csv"))
        expected_files = int(state["expected_symbol_file_count"])
        if len(symbol_files) != expected_files:
            raise QlibDailyError("refresh staging symbol coverage differs")
        entries, completed_symbols = _staged_symbol_entries(
            temporary,
            prior_end=prior_end,
            target=target_date,
        )
        resumed_symbol_count = len(completed_symbols)
        pending_symbols = [symbol for symbol in entries if symbol not in completed_symbols]
        progress = {
            "completed": resumed_symbol_count,
            "retry_count": int(state.get("retry_count") or 0),
            "failures": int(state.get("failure_count") or 0),
        }
        state.update(
            {
                "status": "incomplete",
                "completed_symbol_count": resumed_symbol_count,
                "failure_count": progress["failures"],
            }
        )
        _write_refresh_state(state_path, state)
        for symbol in sorted(completed_symbols):
            path, _ = entries[symbol]
            emit(
                {
                    "event": "stock_resume",
                    "symbol": symbol,
                    "row_sha256": _content_sha256(
                        _read_last_raw_row(path).to_dict()
                    ),
                }
            )
        emit(
            {
                "event": "stage_start",
                "stage": "baostock_refresh",
                "total_symbols": len(entries),
                "resumed_symbols": resumed_symbol_count,
                "pending_symbols": len(pending_symbols),
                "request_timeout_seconds": request_timeout_seconds,
                "max_attempts": max_attempts,
                "max_failures": max_failures,
            }
        )

        def baostock_event(event: Mapping[str, Any]) -> None:
            if event.get("event") == "stock_retry":
                progress["retry_count"] += 1
            elif event.get("event") == "stock_failed":
                progress["failures"] += 1
            state.update(
                {
                    "retry_count": progress["retry_count"],
                    "failure_count": progress["failures"],
                    "last_symbol": event.get("symbol"),
                }
            )
            _write_refresh_state(state_path, state)
            emit(event)

        def persist_success(
            symbol: str,
            row: Mapping[str, str],
            attempts: int,
            duration_seconds: float,
        ) -> None:
            path, name = entries[symbol]
            output = _append_staged_row_atomic(
                path,
                symbol=symbol,
                name=name,
                row=row,
            )
            progress["completed"] += 1
            state.update(
                {
                    "completed_symbol_count": progress["completed"],
                    "retry_count": progress["retry_count"],
                    "failure_count": progress["failures"],
                    "last_symbol": symbol,
                }
            )
            _write_refresh_state(state_path, state)
            emit(
                {
                    "event": "stock_complete",
                    "symbol": symbol,
                    "attempts": attempts,
                    "duration_seconds": duration_seconds,
                    "row_sha256": _content_sha256(output),
                }
            )
            if (
                progress["completed"] % STOCK_PROGRESS_INTERVAL == 0
                or progress["completed"] == len(entries)
            ):
                emit(
                    {
                        "event": "stock_progress",
                        "completed_symbols": progress["completed"],
                        "total_symbols": len(entries),
                        "retry_count": progress["retry_count"],
                        "failure_count": progress["failures"],
                    }
                )

        stage_started = clock.perf_counter()
        fetch_stats = _baostock_rows(
            pending_symbols,
            target_date,
            on_success=persist_success,
            event_sink=baostock_event,
            request_timeout_seconds=request_timeout_seconds,
            max_attempts=max_attempts,
            max_failures=max_failures,
        )
        stage_seconds["baostock_refresh"] = round(
            clock.perf_counter() - stage_started, 4
        )
        emit(
            {
                "event": "stage_complete",
                "stage": "baostock_refresh",
                "duration_seconds": stage_seconds["baostock_refresh"],
                "completed_symbols": progress["completed"],
                "retry_count": progress["retry_count"],
                "failure_count": progress["failures"],
            }
        )
        if progress["completed"] != len(entries):
            raise QlibDailyError("daily refresh is incomplete after bounded requests")

        manifest = _load_json(temporary / "manifest.json", label="source manifest")
        manifest.update(
            {
                "collection_end": target_date.isoformat(),
                "completed_symbol_file_count": len(symbol_files),
                "failure_count": 0,
                "failures": [],
                "status": "complete",
            }
        )
        _atomic_write_bytes(temporary / "manifest.json", _strict_json_bytes(manifest))
        state["status"] = "validation_pending"
        _write_refresh_state(state_path, state)
        stage_started = clock.perf_counter()
        inspection = inspect_completed_source(temporary, target_date)
        stage_seconds["source_validation"] = round(
            clock.perf_counter() - stage_started, 4
        )
        _atomic_write_bytes(
            temporary / "refresh-report.json",
            _strict_json_bytes(
                {
                    "status": "complete",
                    "target_date": target_date.isoformat(),
                    "symbol_file_count": len(symbol_files),
                    "completed_symbol_count": progress["completed"],
                    "resumed_symbol_count": resumed_symbol_count,
                    "failure_count": 0,
                    "request_failure_count": progress["failures"],
                    "retry_count": progress["retry_count"],
                    "stage_seconds": stage_seconds,
                }
            ),
        )
        state["status"] = "complete"
        _write_refresh_state(state_path, state)
        emit(
            {
                "event": "stage_complete",
                "stage": "source_validation",
                "duration_seconds": stage_seconds["source_validation"],
            }
        )
        try:
            os.rename(temporary, target)
        except OSError:
            if target.exists():
                existing = inspect_completed_source(target, target_date)
                return {
                    **existing,
                    "operation_status": "exists",
                    "resumed_symbol_count": resumed_symbol_count,
                    "retry_count": progress["retry_count"],
                    "stage_seconds": stage_seconds,
                    "refresh_seconds": round(clock.perf_counter() - started, 4),
                }
            raise
        return {
            **inspection,
            "source_dir": str(target),
            "operation_status": operation_status,
            "resumed_symbol_count": resumed_symbol_count,
            "retry_count": state.get("retry_count", fetch_stats["retry_count"]),
            "stage_seconds": stage_seconds,
            "refresh_seconds": round(clock.perf_counter() - started, 4),
        }
    except Exception as exc:
        if state_path.is_file():
            try:
                state = _load_json(state_path, label="refresh state")
                state["status"] = "incomplete"
                state["last_error_type"] = type(exc).__name__
                _write_refresh_state(state_path, state)
                emit(
                    {
                        "event": "stage_failed",
                        "stage": "raw_refresh",
                        "error_type": type(exc).__name__,
                        "completed_symbols": state.get("completed_symbol_count", 0),
                        "retry_count": state.get("retry_count", 0),
                        "failure_count": state.get("failure_count", 0),
                    }
                )
            except Exception:
                pass
        raise


def _next_trading_date(provider: Path, completed_date: date) -> date:
    calendar = (provider / "calendars" / "day.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    later = [date.fromisoformat(value) for value in calendar if value > completed_date.isoformat()]
    if not later:
        raise QlibDailyError("provider calendar has no next real trading day")
    return later[0]


def inspect_prepared_provider(
    provider_uri: Path | str,
    *,
    completed_date: date | str,
    source_info: Mapping[str, Any],
) -> dict[str, Any]:
    target = _canonical_date(completed_date, field="completed_date")
    provider = Path(provider_uri).resolve()
    manifest = _load_json(
        provider / "metadata" / "manifest.json", label="provider manifest"
    )
    if manifest.get("source_symbol_file_count") != source_info.get("symbol_file_count"):
        raise QlibDailyError("provider/source file coverage differs")
    eligible = pd.read_parquet(provider / "metadata" / "eligible_rows.parquet")
    observed = pd.to_datetime(eligible["datetime"]).dt.date
    if observed.empty or observed.max() != target or (observed > target).any():
        raise QlibDailyError("provider maximum complete trading day must equal T")
    target_rows = eligible.loc[observed.eq(target)].copy()
    target_records = [
        _eligible_target_record(row)
        for row in target_rows.to_dict(orient="records")
    ]
    target_records.sort(key=lambda item: item["stock_code"])
    if (
        len(target_records) != source_info.get("eligible_target_count")
        or _content_sha256(target_records) != source_info.get("eligible_target_sha256")
    ):
        raise QlibDailyError("provider T-day eligible universe/content differs from source")
    provider_hash, file_count = provider_tree_sha256(provider)
    return {
        "eligible_row_count_at_T": int(observed.eq(target).sum()),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "max_complete_trading_date": target.isoformat(),
        "next_trading_date": _next_trading_date(provider, target).isoformat(),
        "provider_content_sha256": provider_hash,
        "provider_file_count": file_count,
        "provider_uri": str(provider),
        "source_symbol_file_count": manifest.get("source_symbol_file_count"),
    }


def prepare_provider_for_completed_date(
    *,
    source_dir: Path | str,
    provider_uri: Path | str,
    completed_date: date | str,
    start_date: str = "2023-09-01",
) -> dict[str, Any]:
    """Reuse a matching provider or atomically replace it from a complete source."""

    started = clock.perf_counter()
    target = _canonical_date(completed_date, field="completed_date")
    source = Path(source_dir).resolve()
    provider = Path(provider_uri).resolve()
    source_info = inspect_completed_source(source, target)
    if provider.exists():
        try:
            existing = inspect_prepared_provider(
                provider, completed_date=target, source_info=source_info
            )
        except (OSError, QlibDailyError, QlibShadowError):
            existing = None
        if existing is not None:
            return {
                **existing,
                "operation_status": "exists",
                "provider_prepare_seconds": round(clock.perf_counter() - started, 4),
            }

    provider.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{provider.name}-", dir=provider.parent))
    shutil.rmtree(staging)
    backup = provider.with_name(f".{provider.name}-previous")
    if backup.exists():
        raise QlibDailyError("stale provider switch backup exists")
    try:
        build_qlib_provider(
            source_dir=source,
            provider_uri=staging,
            replace=False,
            start_date=start_date,
        )
        inspect_prepared_provider(staging, completed_date=target, source_info=source_info)
        if provider.exists():
            os.rename(provider, backup)
        try:
            os.rename(staging, provider)
        except Exception:
            if backup.exists() and not provider.exists():
                os.rename(backup, provider)
            raise
        inspected = inspect_prepared_provider(
            provider, completed_date=target, source_info=source_info
        )
        shutil.rmtree(backup, ignore_errors=True)
        return {
            **inspected,
            "operation_status": "created",
            "provider_prepare_seconds": round(clock.perf_counter() - started, 4),
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and not provider.exists():
            os.rename(backup, provider)
        raise


def verify_fit_count(
    evidence_path: Path | str,
    *,
    model_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = _load_json(Path(evidence_path), label="prospective-v1 evidence")
    training = evidence.get("training")
    artifact = evidence.get("artifact")
    if not isinstance(training, Mapping) or training.get("fit_count") != 1:
        raise QlibDailyError("frozen prospective-v1 fit_count must remain 1")
    if not isinstance(artifact, Mapping):
        raise QlibDailyError("frozen prospective-v1 evidence is incomplete")
    model_sha = model_manifest.get("files", {}).get("model.pkl")
    if artifact.get("model_file_sha256") != model_sha:
        raise QlibDailyError("frozen model SHA differs from accepted fit_count evidence")
    return {"fit_count": 1, "model_file_sha256": model_sha}


def candidate_technical_context(
    *,
    source_dir: Path | str,
    candidates: Sequence[Mapping[str, Any]],
    data_as_of: date | str,
) -> list[dict[str, Any]]:
    """Calculate the minimum completed-close context; ATR14 is a 14-TR SMA."""

    cutoff = _canonical_date(data_as_of, field="data_as_of")
    source = Path(source_dir).resolve()
    output = []
    for candidate in candidates:
        code = str(candidate.get("code") or "")
        frame = pd.read_csv(source / "symbols" / f"{code}.csv", dtype=str)
        frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.date
        frame = frame.loc[frame["date"].le(cutoff)].copy()
        for column in ("open", "high", "low", "close", "preclose"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        active = frame.loc[frame["tradestatus"].astype(str).eq("1")].copy()
        if active.empty or active.iloc[-1]["date"] != cutoff or len(active) < 20:
            raise QlibDailyError(f"technical context is incomplete at T: {code}")
        previous_close = active["close"].shift(1)
        true_range = pd.concat(
            [
                active["high"] - active["low"],
                (active["high"] - previous_close).abs(),
                (active["low"] - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        active["ma5"] = active["close"].rolling(5).mean()
        active["ma10"] = active["close"].rolling(10).mean()
        active["ma20"] = active["close"].rolling(20).mean()
        active["atr14"] = true_range.rolling(14).mean()
        active["high20"] = active["high"].rolling(20).max()
        active["low20"] = active["low"].rolling(20).min()
        row = active.iloc[-1]
        fields = {
            "atr14": row["atr14"],
            "close": row["close"],
            "high_20d": row["high20"],
            "low_20d": row["low20"],
            "ma10": row["ma10"],
            "ma20": row["ma20"],
            "ma5": row["ma5"],
            "prev_close": row["preclose"],
        }
        if any(pd.isna(value) for value in fields.values()):
            raise QlibDailyError(f"technical context contains missing values: {code}")
        output.append(
            {
                "code": code,
                "data_as_of": cutoff.isoformat(),
                **{key: float(value) for key, value in fields.items()},
            }
        )
    return output


def _verify_ready_target(
    target: Path,
    *,
    expected_payload: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not target.is_dir() or {item.name for item in target.iterdir()} != {
        "manifest.json",
        "ready.json",
    }:
        raise QlibShadowConflictError("nightly ready archive file set differs")
    manifest = _load_json(target / "manifest.json", label="nightly ready manifest")
    manifest_source = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if manifest.get("manifest_sha256") != _content_sha256(manifest_source):
        raise QlibShadowConflictError("nightly ready manifest hash mismatch")
    if manifest.get("schema_version") != DAILY_READY_SCHEMA_VERSION:
        raise QlibShadowConflictError("nightly ready schema differs")
    ready_path = target / "ready.json"
    if manifest.get("files") != {"ready.json": _file_sha256(ready_path)}:
        raise QlibShadowConflictError("nightly ready file hash mismatch")
    ready = _load_json(ready_path, label="nightly ready payload")
    if manifest.get("ready_content_sha256") != _content_sha256(ready):
        raise QlibShadowConflictError("nightly ready content hash mismatch")
    if expected_payload is not None and canonical_json_bytes(ready) != canonical_json_bytes(
        expected_payload
    ):
        raise QlibShadowConflictError("nightly ready result differs; original preserved")
    return ready, manifest


def archive_nightly_ready(
    *,
    source_dir: Path | str,
    ready_root: Path | str,
    shadow_result: Mapping[str, Any],
    shadow_manifest: Mapping[str, Any],
    preparation: Mapping[str, Any],
    model_identity: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(shadow_result)
    if result.get("status") != "ok" or result.get("candidate_count") != 5:
        raise QlibDailyError("nightly Top5 was not produced; morning must fail closed")
    signal_date = _canonical_date(result.get("trade_date"), field="trade_date")
    cutoff = _canonical_date(result.get("data_as_of"), field="data_as_of")
    technical = candidate_technical_context(
        source_dir=source_dir,
        candidates=result["candidates"],
        data_as_of=cutoff,
    )
    technical_by_code = {item["code"]: item for item in technical}
    candidates = []
    for candidate in result["candidates"]:
        candidates.append(
            {
                "code": candidate["code"],
                "name": candidate.get("name"),
                "rank": candidate["rank"],
                "score": candidate["score"],
                "technical": technical_by_code[candidate["code"]],
            }
        )
    payload = {
        "candidate_count": 5,
        "candidates": candidates,
        "created_at": result["generated_at"],
        "data_as_of": cutoff.isoformat(),
        "fit_count": model_identity["fit_count"],
        "model_artifact_manifest_sha256": result[
            "model_artifact_manifest_sha256"
        ],
        "model_file_sha256": model_identity["model_file_sha256"],
        "model_version": result["model_version"],
        "preparation": dict(preparation),
        "provider_content_sha256": result["provider_content_sha256"],
        "schema_version": DAILY_READY_SCHEMA_VERSION,
        "shadow_manifest_sha256": shadow_manifest["manifest_sha256"],
        "shadow_run_sha256": result["run_sha256"],
        "status": "ready",
        "technical_formula": {
            "atr14": "simple_mean_of_true_range_14_completed_sessions",
            "high_20d": "max_high_20_completed_sessions",
            "low_20d": "min_low_20_completed_sessions",
            "ma": "simple_mean_of_close",
        },
        "trade_date": signal_date.isoformat(),
        "use_boundary": "manual_or_ChatGPT_context_only_no_trading_advice",
    }
    ready_bytes = _strict_json_bytes(payload)
    manifest = _manifest_with_hash(
        {
            "files": {"ready.json": _sha256_bytes(ready_bytes)},
            "ready_content_sha256": _content_sha256(payload),
            "schema_version": DAILY_READY_SCHEMA_VERSION,
            "shadow_run_sha256": result["run_sha256"],
        }
    )
    target = _ready_target(Path(ready_root).resolve(), signal_date)
    if target.exists():
        ready, existing_manifest = _verify_ready_target(
            target, expected_payload=payload
        )
        return {
            "archive_dir": str(target),
            "manifest": existing_manifest,
            "operation_status": "exists",
            "ready": ready,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        (temporary / "ready.json").write_bytes(ready_bytes)
        (temporary / "manifest.json").write_bytes(_strict_json_bytes(manifest))
        os.rename(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "archive_dir": str(target),
        "manifest": manifest,
        "operation_status": "created",
        "ready": payload,
    }


def load_nightly_ready(
    *, ready_root: Path | str, trade_date: date | str
) -> dict[str, Any]:
    signal_date = _canonical_date(trade_date, field="trade_date")
    target = _ready_target(Path(ready_root).resolve(), signal_date)
    if not target.exists():
        raise QlibDailyError(
            "nightly Top5 is unavailable; 先运行收盘后 prepare，禁止沿用旧候选"
        )
    ready, manifest = _verify_ready_target(target)
    if (
        ready.get("status") != "ready"
        or ready.get("trade_date") != signal_date.isoformat()
        or ready.get("candidate_count") != 5
        or ready.get("fit_count") != 1
        or ready.get("model_version") != FROZEN_MODEL_VERSION
    ):
        raise QlibDailyError("nightly Top5 readiness contract failed closed")
    return {"archive_dir": str(target), "manifest": manifest, "ready": ready}


def _private_codes(ready: Mapping[str, Any]) -> str:
    values = []
    for candidate in ready.get("candidates", []):
        code = str(candidate.get("code") or "")
        if len(code) != 6 or not code.isdigit():
            raise QlibDailyError("nightly candidate code is invalid")
        values.append(f"{code}.SH" if code.startswith("6") else f"{code}.SZ")
    if len(values) != 5 or len(set(values)) != 5:
        raise QlibDailyError("nightly candidate coverage must contain exactly five codes")
    return ",".join(values)


def dispatch_morning_quotes(
    *,
    ready_root: Path | str,
    trade_date: date | str,
    dry_run: bool = False,
    observed_at: datetime | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Read the frozen Top5 and manually dispatch the existing Private workflow."""

    started = clock.perf_counter()
    signal_date = _canonical_date(trade_date, field="trade_date")
    loaded = load_nightly_ready(ready_root=ready_root, trade_date=signal_date)
    codes = _private_codes(loaded["ready"])
    command = [
        "gh",
        "workflow",
        "run",
        PRIVATE_WORKFLOW,
        "--repo",
        PRIVATE_REPOSITORY,
        "--ref",
        "main",
        "-f",
        "mode=sample_only",
        "-f",
        f"stock_codes={codes}",
        "-f",
        f"confirm_private_read_only={PRIVATE_CONFIRMATION}",
        "-f",
        f"reason=DoubleEnsemble nightly Top5 {signal_date.isoformat()} quote confirmation",
    ]
    if not dry_run:
        if _now(observed_at).date() != signal_date:
            raise QlibDailyError("Private quote confirmation is allowed only on trade_date")
        try:
            runner(command, check=True)
        except subprocess.CalledProcessError:
            raise QlibDailyError(
                "Private workflow dispatch failed closed; no quote confirmation was claimed"
            ) from None
    return {
        "candidate_count": 5,
        "codes": codes.split(","),
        "morning_action": "validated_only" if dry_run else "private_workflow_dispatched",
        "morning_seconds": round(clock.perf_counter() - started, 4),
        "qlib_ran": False,
        "shadow_reordered": False,
        "trade_date": signal_date.isoformat(),
    }
