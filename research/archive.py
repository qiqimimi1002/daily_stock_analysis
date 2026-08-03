"""Immutable, idempotent archives for V2.1 market-screening signals.

This module only records the state that existed when a screening signal was
formed. It deliberately does not calculate forward returns, drawdowns, win
rates, trading points, or any other performance metric.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
import uuid
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
ARCHIVE_SCHEMA_VERSION = "V2.2.1"
SIGNAL_ID_NAMESPACE = uuid.UUID("23ca9f50-850c-4d27-b465-02d887e93788")
_CLOSE_PRICE_TYPES = {"close", "daily_close", "official_close", "当日收盘价", "收盘价"}
_STOCK_CODE_RE = re.compile(r"^[0-9]{6}$")


class SignalValidationError(ValueError):
    """Raised when a source signal cannot be archived without guessing."""


class ArchiveConflictError(RuntimeError):
    """Raised when an immutable batch already exists with different content."""


@dataclass(frozen=True)
class ArchiveResult:
    """Outcome of one archive request."""

    status: str
    archive_dir: Path
    signal_count: int
    signal_ids: Sequence[str]
    content_hash: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "archive_dir": str(self.archive_dir),
            "signal_count": self.signal_count,
            "signal_ids": list(self.signal_ids),
            "content_hash": self.content_hash,
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _clean_json_value(value: Any) -> Any:
    """Convert arbitrary input to strict JSON without inventing values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _clean_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    return numeric if math.isfinite(numeric) else None


def _as_mapping(value: Any, *, field: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SignalValidationError(f"{field} must be a JSON object")
    return dict(value)


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SignalValidationError(f"{field} is required")
    return text


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _finite_number(
    value: Any,
    *,
    field: str,
    required: bool = False,
    positive: bool = False,
) -> Optional[float]:
    if value is None or value == "":
        if required:
            raise SignalValidationError(f"{field} is required")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        if required:
            raise SignalValidationError(f"{field} must be a valid number") from None
        return None
    if not math.isfinite(number):
        if required:
            raise SignalValidationError(f"{field} must be finite")
        return None
    if positive and number <= 0:
        raise SignalValidationError(f"{field} must be positive")
    return number


def _aware_shanghai_datetime(value: Any, *, field: str) -> datetime:
    text = _required_text(value, field=field)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise SignalValidationError(f"{field} must be an ISO-8601 datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SignalValidationError(f"{field} must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def _signal_date(value: Any, *, generated_at: datetime) -> date:
    if value in (None, ""):
        return generated_at.date()
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        raise SignalValidationError("signal_date must be an ISO-8601 date") from None
    if parsed != generated_at.date():
        raise SignalValidationError(
            "signal_date must match signal_generated_at in Asia/Shanghai"
        )
    return parsed


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := str(item or "").strip())]


def _score_breakdown(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    cleaned = _clean_json_value(value)
    return dict(cleaned) if isinstance(cleaned, Mapping) else {}


def _normalize_stock_code(value: Any) -> str:
    code = _required_text(value, field="stock_code")
    if not _STOCK_CODE_RE.fullmatch(code):
        raise SignalValidationError("stock_code must contain exactly six digits")
    return code


def _reference_price_type(
    candidate: Mapping[str, Any],
    *,
    signal_date: date,
    market_data_at: datetime,
) -> str:
    supplied = _optional_text(candidate.get("reference_price_type"))
    local_market_date = market_data_at.date()
    is_same_day_intraday = (
        local_market_date == signal_date and market_data_at.time() < time(15, 0)
    )
    if supplied and supplied.lower() in _CLOSE_PRICE_TYPES and is_same_day_intraday:
        raise SignalValidationError(
            "intraday signals cannot use the current day's not-yet-formed close"
        )
    if supplied:
        return supplied
    return "intraday_latest" if is_same_day_intraday else "latest_available"


def build_signal_id(
    *,
    signal_date: date | str,
    stock_code: str,
    model_version: str,
    batch_id: str,
) -> str:
    """Return the stable identity for one signal in one immutable batch."""
    identity = "|".join(
        (
            str(signal_date),
            _normalize_stock_code(stock_code),
            _required_text(model_version, field="model_version"),
            _required_text(batch_id, field="batch_id"),
        )
    )
    return str(uuid.uuid5(SIGNAL_ID_NAMESPACE, identity))


def _normalize_signal(
    candidate: Mapping[str, Any],
    *,
    signal_date: date,
    signal_generated_at: datetime,
    market_data_at: datetime,
    archived_at: datetime,
    data_source: str,
    model_version: str,
    source_artifact: str,
    batch_id: str,
) -> Dict[str, Any]:
    code = _normalize_stock_code(candidate.get("code", candidate.get("stock_code")))
    reference_price = _finite_number(
        candidate.get("reference_price", candidate.get("latest_price")),
        field="reference_price",
        required=True,
        positive=True,
    )
    assert reference_price is not None
    reference_type = _reference_price_type(
        candidate,
        signal_date=signal_date,
        market_data_at=market_data_at,
    )

    record: Dict[str, Any] = {
        "signal_id": build_signal_id(
            signal_date=signal_date,
            stock_code=code,
            model_version=model_version,
            batch_id=batch_id,
        ),
        "signal_date": signal_date.isoformat(),
        "signal_generated_at": signal_generated_at.isoformat(timespec="seconds"),
        "market_data_at": market_data_at.isoformat(timespec="seconds"),
        "stock_code": code,
        "stock_name": _optional_text(candidate.get("name", candidate.get("stock_name"))),
        "reference_price": reference_price,
        "reference_price_type": reference_type,
        "total_score": _finite_number(
            candidate.get("total_score", candidate.get("score")), field="total_score"
        ),
        "raw_score": _finite_number(candidate.get("raw_score"), field="raw_score"),
        "available_max_score": _finite_number(
            candidate.get("available_max_score"), field="available_max_score"
        ),
        "score_coverage_pct": _finite_number(
            candidate.get("score_coverage_pct"), field="score_coverage_pct"
        ),
        "confidence_label": _optional_text(candidate.get("confidence_label")),
        "score_breakdown": _score_breakdown(candidate.get("score_breakdown")),
        "latest_price": _finite_number(
            candidate.get("latest_price"), field="latest_price"
        ),
        "daily_pct": _finite_number(candidate.get("daily_pct"), field="daily_pct"),
        "five_day_pct": _finite_number(
            candidate.get("five_day_pct"), field="five_day_pct"
        ),
        "amount_yi": _finite_number(candidate.get("amount_yi"), field="amount_yi"),
        "avg_amount_20d_yi": _finite_number(
            candidate.get("avg_amount_20d_yi"), field="avg_amount_20d_yi"
        ),
        "turnover_pct": _finite_number(
            candidate.get("turnover_pct"), field="turnover_pct"
        ),
        "ma5": _finite_number(candidate.get("ma5"), field="ma5"),
        "ma10": _finite_number(candidate.get("ma10"), field="ma10"),
        "ma20": _finite_number(candidate.get("ma20"), field="ma20"),
        "trend_label": _optional_text(candidate.get("trend_label")),
        "watch_zone": _optional_text(candidate.get("watch_zone")),
        "trigger_conditions": _string_list(candidate.get("trigger_conditions")),
        "abandon_conditions": _string_list(candidate.get("abandon_conditions")),
        "risk_gate": _optional_text(candidate.get("risk_gate")),
        "risks": _string_list(candidate.get("risks")),
        "evidence_gaps": _string_list(candidate.get("evidence_gaps")),
        "data_source": data_source,
        "model_version": model_version,
        "source_artifact": source_artifact,
        "archived_at": archived_at.isoformat(timespec="seconds"),
    }
    return record


def _parquet_rows(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    nested_fields = {
        "score_breakdown",
        "trigger_conditions",
        "abandon_conditions",
        "risks",
        "evidence_gaps",
    }
    rows: List[Dict[str, Any]] = []
    for record in records:
        row = dict(record)
        for field in nested_fields:
            row[field] = json.dumps(
                record.get(field),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        rows.append(row)
    return rows


def _write_parquet(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "Parquet support requires the isolated research dependencies: "
            "pip install -r requirements-research.txt"
        ) from exc
    table = pa.Table.from_pylist(_parquet_rows(records))
    pq.write_table(table, path, compression="zstd")


def _archive_content(
    source: Mapping[str, Any],
    *,
    records: Sequence[Mapping[str, Any]],
    batch_id: str,
    source_artifact: str,
) -> Dict[str, Any]:
    records_without_archive_time = [
        {key: value for key, value in record.items() if key != "archived_at"}
        for record in records
    ]
    return {
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "batch_id": batch_id,
        "source_artifact": source_artifact,
        "signals": records_without_archive_time,
        "raw_source": _clean_json_value(source),
    }


def _archive_directory(output_root: Path, *, signal_date: date, batch_id: str) -> Path:
    batch_key = _sha256_bytes(batch_id.encode("utf-8"))[:16]
    return (
        output_root
        / f"{signal_date.year:04d}"
        / f"{signal_date.month:02d}"
        / f"{signal_date.day:02d}"
        / f"batch-{batch_key}"
    )


def _existing_result(archive_dir: Path, *, content_hash: str) -> ArchiveResult:
    manifest_path = archive_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ArchiveConflictError(
            f"archive path exists without a valid manifest: {archive_dir}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveConflictError(
            f"existing manifest cannot be verified: {archive_dir}"
        ) from exc
    if manifest.get("content_hash") != content_hash:
        raise ArchiveConflictError(
            f"immutable archive conflict; original preserved at {archive_dir}"
        )
    files = manifest.get("files")
    if not isinstance(files, Mapping) or set(files) != {
        "signals.json",
        "signals.parquet",
    }:
        raise ArchiveConflictError(
            f"existing archive has no verifiable file hashes: {archive_dir}"
        )
    for name, expected_hash in files.items():
        existing_file = archive_dir / str(name)
        if not existing_file.is_file():
            raise ArchiveConflictError(
                f"existing archive file is missing; original path preserved: {existing_file}"
            )
        if _sha256_bytes(existing_file.read_bytes()) != expected_hash:
            raise ArchiveConflictError(
                f"existing archive file hash mismatch; original preserved: {existing_file}"
            )
    try:
        payload = json.loads((archive_dir / "signals.json").read_text(encoding="utf-8"))
        payload_records = payload["signals"]
        payload_source = payload["raw_source"]
        payload_batch = payload["batch_id"]
        payload_artifact = payload["source_artifact"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ArchiveConflictError(
            f"existing signal payload cannot be verified: {archive_dir}"
        ) from exc
    if (
        not isinstance(payload_source, Mapping)
        or not isinstance(payload_records, list)
        or any(not isinstance(record, Mapping) for record in payload_records)
        or not isinstance(payload_batch, str)
        or not isinstance(payload_artifact, str)
    ):
        raise ArchiveConflictError(
            f"existing signal payload has an invalid structure: {archive_dir}"
        )
    recomputed_hash = _sha256_bytes(
        _canonical_json_bytes(
            _archive_content(
                payload_source,
                records=payload_records,
                batch_id=payload_batch,
                source_artifact=payload_artifact,
            )
        )
    )
    payload_ids = [record.get("signal_id") for record in payload_records]
    if (
        recomputed_hash != content_hash
        or manifest.get("signal_count") != len(payload_records)
        or manifest.get("signal_ids") != payload_ids
    ):
        raise ArchiveConflictError(
            f"existing archive manifest is inconsistent; original preserved at {archive_dir}"
        )
    return ArchiveResult(
        status="exists",
        archive_dir=archive_dir,
        signal_count=int(manifest.get("signal_count", 0)),
        signal_ids=tuple(manifest.get("signal_ids") or ()),
        content_hash=content_hash,
    )


def archive_signals(
    source: Mapping[str, Any],
    *,
    output_root: Path | str,
    market_data_at: Optional[str] = None,
    batch_id: Optional[str] = None,
    source_artifact: str,
    archived_at: Optional[datetime] = None,
    parquet_writer: Callable[[Path, Sequence[Mapping[str, Any]]], None] = _write_parquet,
) -> ArchiveResult:
    """Archive one existing V2.1 result without recalculating its candidates."""
    root = _as_mapping(source, field="input")
    generated_at = _aware_shanghai_datetime(
        root.get("generated_at", root.get("signal_generated_at")),
        field="signal_generated_at",
    )
    signal_day = _signal_date(root.get("signal_date"), generated_at=generated_at)
    market_time_value = (
        market_data_at
        or root.get("market_data_at")
        or _as_mapping(root.get("market_environment") or {}, field="market_environment").get(
            "market_data_at"
        )
    )
    quote_time = _aware_shanghai_datetime(market_time_value, field="market_data_at")
    if quote_time > generated_at:
        raise SignalValidationError(
            "market_data_at cannot be later than signal_generated_at"
        )

    archive_time = archived_at or datetime.now(SHANGHAI_TZ)
    if archive_time.tzinfo is None or archive_time.utcoffset() is None:
        raise SignalValidationError("archived_at must include a timezone offset")
    archive_time = archive_time.astimezone(SHANGHAI_TZ)
    if archive_time < generated_at:
        raise SignalValidationError("archived_at cannot be earlier than signal_generated_at")

    model_version = _required_text(root.get("model_version"), field="model_version")
    data_source = _required_text(root.get("data_source"), field="data_source")
    artifact = _required_text(source_artifact, field="source_artifact")
    stable_batch = _required_text(
        batch_id or root.get("signal_batch_id") or root.get("batch_id") or root.get("generated_at"),
        field="batch_id",
    )
    raw_candidates = root.get("candidates")
    if not isinstance(raw_candidates, list):
        raise SignalValidationError("candidates must be a JSON array")

    records: List[Dict[str, Any]] = []
    raw_signals: List[Any] = []
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = _as_mapping(raw_candidate, field=f"candidates[{index}]")
        records.append(
            _normalize_signal(
                candidate,
                signal_date=signal_day,
                signal_generated_at=generated_at,
                market_data_at=quote_time,
                archived_at=archive_time,
                data_source=data_source,
                model_version=model_version,
                source_artifact=artifact,
                batch_id=stable_batch,
            )
        )
        raw_signals.append(_clean_json_value(candidate))

    if len({record["signal_id"] for record in records}) != len(records):
        raise SignalValidationError("input contains duplicate stock codes in the same batch")

    content = _archive_content(
        root,
        records=records,
        batch_id=stable_batch,
        source_artifact=artifact,
    )
    content_hash = _sha256_bytes(_canonical_json_bytes(content))
    target = _archive_directory(
        Path(output_root), signal_date=signal_day, batch_id=stable_batch
    )
    if target.exists():
        return _existing_result(target, content_hash=content_hash)

    payload = {
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "test_data": bool(root.get("test_data", False)),
        "batch_id": stable_batch,
        "signal_date": signal_day.isoformat(),
        "signal_generated_at": generated_at.isoformat(timespec="seconds"),
        "market_data_at": quote_time.isoformat(timespec="seconds"),
        "archived_at": archive_time.isoformat(timespec="seconds"),
        "source_artifact": artifact,
        "signals": records,
        "raw_signals": raw_signals,
        "raw_source": _clean_json_value(root),
    }
    json_bytes = _strict_json_bytes(payload)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        json_path = temp_dir / "signals.json"
        parquet_path = temp_dir / "signals.parquet"
        json_path.write_bytes(json_bytes)
        parquet_writer(parquet_path, records)
        if not parquet_path.is_file():
            raise RuntimeError("parquet writer did not create signals.parquet")

        manifest = {
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "batch_id": stable_batch,
            "signal_date": signal_day.isoformat(),
            "signal_generated_at": generated_at.isoformat(timespec="seconds"),
            "market_data_at": quote_time.isoformat(timespec="seconds"),
            "archived_at": archive_time.isoformat(timespec="seconds"),
            "data_source": data_source,
            "model_version": model_version,
            "source_artifact": artifact,
            "signal_count": len(records),
            "signal_ids": [record["signal_id"] for record in records],
            "content_hash": content_hash,
            "raw_source_hash": _sha256_bytes(
                _canonical_json_bytes(_clean_json_value(root))
            ),
            "files": {
                "signals.json": _sha256_bytes(json_path.read_bytes()),
                "signals.parquet": _sha256_bytes(parquet_path.read_bytes()),
            },
        }
        (temp_dir / "manifest.json").write_bytes(_strict_json_bytes(manifest))
        try:
            os.rename(temp_dir, target)
        except OSError:
            if target.exists():
                existing = _existing_result(target, content_hash=content_hash)
                shutil.rmtree(temp_dir, ignore_errors=True)
                return existing
            raise
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return ArchiveResult(
        status="created",
        archive_dir=target,
        signal_count=len(records),
        signal_ids=tuple(record["signal_id"] for record in records),
        content_hash=content_hash,
    )


def load_source(path: Path | str) -> Mapping[str, Any]:
    """Load a strict JSON V2.1 screening artifact."""
    source_path = Path(path)
    try:
        value = json.loads(
            source_path.read_text(encoding="utf-8"),
            parse_constant=lambda token: float(token),
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SignalValidationError(f"cannot read input JSON: {source_path}") from exc
    return _as_mapping(value, field="input")
