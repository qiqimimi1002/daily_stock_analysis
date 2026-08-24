"""Immutable prospective shadow archive for one frozen DoubleEnsemble model."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
import shutil
import tempfile
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from research.benchmarks.qlib_doubleensemble import (
    MODEL_CONFIG_VERSION,
    QLIB_PACKAGE_VERSION,
    RANDOM_SEED,
    TimeSplits,
    build_candidate_batches,
    fit_model_and_predict,
    model_config_payload,
    model_config_sha256,
    predict_with_frozen_model,
)
from research.benchmarks.schema import canonical_json_bytes


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
FROZEN_SCHEMA_VERSION = "qlib-doubleensemble-frozen-model-v1"
REPLAY_FAILURE_SCHEMA_VERSION = "qlib-doubleensemble-replay-failure-v1"
SHADOW_SCHEMA_VERSION = "qlib-doubleensemble-prospective-shadow-v1"
REPLAY_TRADE_DATE = date(2026, 8, 24)
REPLAY_SCORE_ABS_TOLERANCE = 1e-10
APPROVED_SPLITS = TimeSplits.create(
    train_start="2024-01-02",
    train_end="2024-12-25",
    valid_start="2025-01-02",
    valid_end="2025-06-25",
    test_start="2025-07-01",
    test_end="2026-08-21",
)


class QlibShadowError(ValueError):
    """Raised when a frozen-model or T-1 shadow contract is not satisfied."""


class QlibShadowConflictError(QlibShadowError):
    """Raised when immutable model or daily content differs."""


class QlibReplayMismatchError(QlibShadowError):
    """Raised when the one permitted replay differs from its frozen baseline."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _content_sha256(value: Any) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


def _canonical_date(value: date | str, *, field: str) -> date:
    if isinstance(value, datetime):
        raise QlibShadowError(f"{field} must be a date")
    text = value.isoformat() if isinstance(value, date) else str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise QlibShadowError(f"{field} must be canonical YYYY-MM-DD") from None
    if text != parsed.isoformat():
        raise QlibShadowError(f"{field} must be canonical YYYY-MM-DD")
    return parsed


def _generated_at(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI_TZ)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise QlibShadowError("generated_at must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QlibShadowError("generated_at must include a timezone")
    return parsed.astimezone(SHANGHAI_TZ)


def provider_tree_sha256(provider_uri: Path | str) -> tuple[str, int]:
    """Hash exact Qlib provider bytes and relative paths in stable order."""

    root = Path(provider_uri).resolve()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    if not files:
        raise QlibShadowError("Qlib provider is empty")
    inventory = []
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        inventory.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": digest.hexdigest(),
                "size": path.stat().st_size,
            }
        )
    return _content_sha256(inventory), len(inventory)


def _manifest_with_hash(source: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(source)
    value["manifest_sha256"] = _content_sha256(value)
    return value


def _verify_manifest_hash(manifest: Mapping[str, Any]) -> None:
    expected = manifest.get("manifest_sha256")
    source = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if expected != _content_sha256(source):
        raise QlibShadowConflictError("immutable manifest hash mismatch")


def _verify_exact_files(
    target: Path,
    *,
    manifest: Mapping[str, Any],
    expected_names: set[str],
) -> None:
    if not target.is_dir():
        raise QlibShadowConflictError("immutable path is not a directory")
    if {item.name for item in target.iterdir()} != expected_names:
        raise QlibShadowConflictError("immutable file set differs")
    files = manifest.get("files")
    if not isinstance(files, Mapping) or set(files) != expected_names - {"manifest.json"}:
        raise QlibShadowConflictError("immutable file hashes are incomplete")
    for name, expected in files.items():
        path = target / str(name)
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected:
            raise QlibShadowConflictError(f"immutable file hash mismatch: {name}")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QlibShadowConflictError("immutable manifest cannot be read") from exc
    if not isinstance(value, dict):
        raise QlibShadowConflictError("immutable manifest must be an object")
    _verify_manifest_hash(value)
    return value


def _expected_batch(path: Path | str, trade_date: date) -> tuple[dict[str, Any], str]:
    source = Path(path)
    try:
        raw = source.read_bytes()
        batches = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QlibShadowError("expected candidate artifact cannot be read") from exc
    if not isinstance(batches, list):
        raise QlibShadowError("expected candidate artifact must be a list")
    matches = [item for item in batches if item.get("trade_date") == trade_date.isoformat()]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise QlibShadowError("expected replay trade date must appear exactly once")
    return matches[0], _sha256_bytes(raw)


def _replay_evidence(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> dict[str, Any]:
    expected_rows = expected.get("candidates")
    actual_rows = actual.get("candidates")
    if not isinstance(expected_rows, list) or not isinstance(actual_rows, list):
        raise QlibReplayMismatchError(
            "replay candidates must be lists",
            reason_code="invalid_candidate_shape",
        )
    if len(expected_rows) != len(actual_rows):
        raise QlibReplayMismatchError(
            "frozen-model replay candidate count differs",
            reason_code="candidate_count_differs",
        )
    comparisons = []
    for expected_row, actual_row in zip(expected_rows, actual_rows):
        expected_identity = (
            str(expected_row.get("stock_code")),
            int(expected_row.get("model_rank")),
        )
        actual_identity = (
            str(actual_row.get("stock_code")),
            int(actual_row.get("model_rank")),
        )
        if expected_identity != actual_identity:
            raise QlibReplayMismatchError(
                "frozen-model replay code or rank differs",
                reason_code="code_or_rank_differs",
            )
        expected_score = float(expected_row.get("doubleensemble_score"))
        actual_score = float(actual_row.get("doubleensemble_score"))
        difference = abs(expected_score - actual_score)
        if not math.isfinite(difference) or difference > REPLAY_SCORE_ABS_TOLERANCE:
            raise QlibReplayMismatchError(
                "frozen-model replay score exceeds tolerance",
                reason_code="score_exceeds_tolerance",
            )
        comparisons.append(
            {
                "code": actual_identity[0],
                "rank": actual_identity[1],
                "expected_score": expected_score,
                "actual_score": actual_score,
                "absolute_difference": difference,
            }
        )
    return {
        "candidate_count": len(comparisons),
        "comparisons": comparisons,
        "score_absolute_tolerance": REPLAY_SCORE_ABS_TOLERANCE,
        "status": "matched",
        "trade_date": REPLAY_TRADE_DATE.isoformat(),
    }


def _replay_failure_path(artifact_dir: Path) -> Path:
    return artifact_dir.with_name(f"{artifact_dir.name}.replay-failed.json")


def _load_replay_failure(path: Path) -> dict[str, Any]:
    failure = _load_manifest(path)
    if failure.get("schema_version") != REPLAY_FAILURE_SCHEMA_VERSION:
        raise QlibShadowConflictError("replay-failure schema differs")
    if failure.get("status") != "failed_closed":
        raise QlibShadowConflictError("replay-failure status differs")
    return failure


def record_replay_failure(
    *,
    provider_uri: Path | str,
    artifact_dir: Path | str,
    expected_candidates_path: Path | str,
    reason_code: str,
    attempted_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Write the immutable receipt that prohibits a second training replay."""

    target = Path(artifact_dir).resolve()
    failure_path = _replay_failure_path(target)
    if target.exists():
        raise QlibShadowConflictError("model artifact already exists")
    if failure_path.exists():
        return _load_replay_failure(failure_path)
    _, expected_file_hash = _expected_batch(
        expected_candidates_path, REPLAY_TRADE_DATE
    )
    provider_hash, provider_file_count = provider_tree_sha256(provider_uri)
    failure = _manifest_with_hash(
        {
            "attempted_at": _generated_at(attempted_at).isoformat(timespec="seconds"),
            "expected_candidates_file_sha256": expected_file_hash,
            "model_config_sha256": model_config_sha256(),
            "model_version": MODEL_CONFIG_VERSION,
            "provider_file_count": provider_file_count,
            "qlib_version": QLIB_PACKAGE_VERSION,
            "random_seed": RANDOM_SEED,
            "reason_code": str(reason_code),
            "replay_trade_date": REPLAY_TRADE_DATE.isoformat(),
            "schema_version": REPLAY_FAILURE_SCHEMA_VERSION,
            "score_absolute_tolerance": REPLAY_SCORE_ABS_TOLERANCE,
            "segments": APPROVED_SPLITS.to_dict(),
            "status": "failed_closed",
            "training_input_sha256": provider_hash,
        }
    )
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = failure_path.with_name(f".{failure_path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_strict_json_bytes(failure))
    try:
        os.rename(temporary, failure_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        if failure_path.exists():
            return _load_replay_failure(failure_path)
        raise
    return failure


def verify_frozen_model_artifact(
    artifact_dir: Path | str,
) -> tuple[Any, dict[str, Any]]:
    """Verify all hashes before loading the local frozen model pickle."""

    target = Path(artifact_dir).resolve()
    failure_path = _replay_failure_path(target)
    if failure_path.exists():
        _load_replay_failure(failure_path)
        raise QlibShadowError(
            "frozen model is unavailable because the one-time replay failed"
        )
    manifest = _load_manifest(target / "manifest.json")
    _verify_exact_files(
        target,
        manifest=manifest,
        expected_names={"manifest.json", "model.pkl", "replay.json"},
    )
    if manifest.get("schema_version") != FROZEN_SCHEMA_VERSION:
        raise QlibShadowConflictError("frozen-model schema differs")
    if manifest.get("model_version") != MODEL_CONFIG_VERSION:
        raise QlibShadowConflictError("frozen-model version differs")
    if manifest.get("qlib_version") != QLIB_PACKAGE_VERSION:
        raise QlibShadowConflictError("frozen-model Qlib version differs")
    if manifest.get("model_config_sha256") != model_config_sha256():
        raise QlibShadowConflictError("frozen-model config hash differs")
    if manifest.get("segments") != APPROVED_SPLITS.to_dict():
        raise QlibShadowConflictError("frozen-model time segments differ")
    try:
        model = pickle.loads((target / "model.pkl").read_bytes())
    except Exception as exc:
        raise QlibShadowConflictError("frozen model cannot be loaded") from exc
    return model, manifest


def freeze_model_artifact(
    *,
    provider_uri: Path | str,
    artifact_dir: Path | str,
    expected_candidates_path: Path | str,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Replay the approved fit once, verify 2026-08-24, then freeze atomically."""

    target = Path(artifact_dir).resolve()
    failure_path = _replay_failure_path(target)
    if failure_path.exists():
        _load_replay_failure(failure_path)
        raise QlibShadowError(
            "one-time frozen-model replay previously failed; retraining is prohibited"
        )
    if target.exists():
        _, manifest = verify_frozen_model_artifact(target)
        return {"status": "exists", "artifact_dir": str(target), **manifest}

    provider = Path(provider_uri).resolve()
    expected, expected_file_hash = _expected_batch(
        expected_candidates_path, REPLAY_TRADE_DATE
    )
    provider_hash, provider_file_count = provider_tree_sha256(provider)
    model, predictions, run_manifest = fit_model_and_predict(
        provider_uri=provider,
        splits=APPROVED_SPLITS,
    )
    calendar = (provider / "calendars" / "day.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    batches = build_candidate_batches(
        predictions=predictions,
        provider_uri=provider,
        calendar=calendar,
    )
    actual_matches = [
        item for item in batches if item["trade_date"] == REPLAY_TRADE_DATE.isoformat()
    ]
    if len(actual_matches) != 1:
        raise QlibShadowError("replay output must contain 2026-08-24 exactly once")
    replay = _replay_evidence(expected, actual_matches[0])
    replay["expected_candidates_file_sha256"] = expected_file_hash
    replay_bytes = _strict_json_bytes(replay)
    try:
        model_bytes = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        raise QlibShadowError("DoubleEnsemble model cannot be serialized") from exc
    provider_manifest = json.loads(
        (provider / "metadata" / "manifest.json").read_text(encoding="utf-8")
    )
    created_at = _generated_at(generated_at).isoformat(timespec="seconds")
    manifest = _manifest_with_hash(
        {
            "created_at": created_at,
            "files": {
                "model.pkl": _sha256_bytes(model_bytes),
                "replay.json": _sha256_bytes(replay_bytes),
            },
            "model_config": model_config_payload(),
            "model_config_sha256": model_config_sha256(),
            "model_version": MODEL_CONFIG_VERSION,
            "provider_file_count": provider_file_count,
            "provider_manifest_sha256": provider_manifest.get("manifest_sha256"),
            "qlib_version": QLIB_PACKAGE_VERSION,
            "random_seed": RANDOM_SEED,
            "replay": {
                "candidate_count": replay["candidate_count"],
                "score_absolute_tolerance": REPLAY_SCORE_ABS_TOLERANCE,
                "status": "matched",
                "trade_date": REPLAY_TRADE_DATE.isoformat(),
            },
            "run_manifest": run_manifest,
            "schema_version": FROZEN_SCHEMA_VERSION,
            "segments": APPROVED_SPLITS.to_dict(),
            "training_input_sha256": provider_hash,
        }
    )
    manifest_bytes = _strict_json_bytes(manifest)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        (temporary / "model.pkl").write_bytes(model_bytes)
        (temporary / "replay.json").write_bytes(replay_bytes)
        (temporary / "manifest.json").write_bytes(manifest_bytes)
        try:
            os.rename(temporary, target)
        except OSError:
            if target.exists():
                verify_frozen_model_artifact(target)
                shutil.rmtree(temporary, ignore_errors=True)
                raise QlibShadowConflictError(
                    "frozen-model path appeared during one-time training"
                )
            raise
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"status": "created", "artifact_dir": str(target), **manifest}


def _validate_t_minus_one(
    provider: Path,
    *,
    trade_date: date,
    data_as_of: date,
) -> list[str]:
    calendar = (provider / "calendars" / "day.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    try:
        trade_index = calendar.index(trade_date.isoformat())
    except ValueError:
        raise QlibShadowError("trade_date is absent from trading calendar") from None
    if trade_index == 0 or calendar[trade_index - 1] != data_as_of.isoformat():
        raise QlibShadowError("data_as_of must be the completed T-1 trading session")
    eligible = pd.read_parquet(provider / "metadata" / "eligible_rows.parquet")
    observed = pd.to_datetime(eligible["datetime"]).dt.date
    if observed.empty or observed.max() != data_as_of or (observed > data_as_of).any():
        raise QlibShadowError("provider daily rows must end exactly at data_as_of")
    return calendar


def _shadow_target(root: Path, trade_date: date) -> Path:
    return (
        root
        / f"{trade_date.year:04d}"
        / f"{trade_date.month:02d}"
        / f"{trade_date.day:02d}"
        / "doubleensemble-shadow-v1"
    )


def _shadow_payload(
    batch: Mapping[str, Any],
    *,
    trade_date: date,
    data_as_of: date,
    generated_at: datetime,
    input_content_sha256: str,
    model_manifest_sha256: str,
    provider_content_sha256: str,
    inference: Mapping[str, Any],
) -> dict[str, Any]:
    generated = generated_at.isoformat(timespec="seconds")
    status = str(batch.get("data_status"))
    candidates = [
        {
            "code": str(row["stock_code"]),
            "data_as_of": data_as_of.isoformat(),
            "generated_at": generated,
            "model_version": MODEL_CONFIG_VERSION,
            "name": row.get("stock_name"),
            "rank": int(row["model_rank"]),
            "score": float(row["doubleensemble_score"]),
            "status": status,
            "trade_date": trade_date.isoformat(),
        }
        for row in batch.get("candidates", [])
    ]
    run_identity = {
        "candidates": candidates,
        "data_as_of": data_as_of.isoformat(),
        "inference": dict(inference),
        "input_content_sha256": input_content_sha256,
        "model_version": MODEL_CONFIG_VERSION,
        "status": status,
        "trade_date": trade_date.isoformat(),
    }
    return {
        **run_identity,
        "candidate_count": len(candidates),
        "evidence_status": "PROSPECTIVE SHADOW",
        "generated_at": generated,
        "model_artifact_manifest_sha256": model_manifest_sha256,
        "provider_content_sha256": provider_content_sha256,
        "run_sha256": _content_sha256(run_identity),
        "schema_version": SHADOW_SCHEMA_VERSION,
    }


def _verify_existing_shadow(
    target: Path,
    *,
    input_content_sha256: str,
    expected_result: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _load_manifest(target / "manifest.json")
    _verify_exact_files(
        target,
        manifest=manifest,
        expected_names={"manifest.json", "result.json"},
    )
    if manifest.get("schema_version") != SHADOW_SCHEMA_VERSION:
        raise QlibShadowConflictError("prospective shadow schema differs")
    if manifest.get("input_content_sha256") != input_content_sha256:
        raise QlibShadowConflictError(
            "same-day prospective input differs; original sample preserved"
        )
    try:
        result = json.loads((target / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QlibShadowConflictError("prospective result cannot be read") from exc
    if manifest.get("result_content_sha256") != _content_sha256(result):
        raise QlibShadowConflictError("prospective result content hash differs")
    if expected_result is not None and canonical_json_bytes(result) != canonical_json_bytes(
        expected_result
    ):
        raise QlibShadowConflictError(
            "same-day prospective result differs; original sample preserved"
        )
    return result, manifest


def run_prospective_shadow(
    *,
    provider_uri: Path | str,
    artifact_dir: Path | str,
    archive_root: Path | str,
    trade_date: date | str,
    data_as_of: date | str,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Load one frozen model, infer T from T-1 only, and archive immutably."""

    provider = Path(provider_uri).resolve()
    signal_day = _canonical_date(trade_date, field="trade_date")
    cutoff = _canonical_date(data_as_of, field="data_as_of")
    calendar = _validate_t_minus_one(
        provider,
        trade_date=signal_day,
        data_as_of=cutoff,
    )
    model, model_manifest = verify_frozen_model_artifact(artifact_dir)
    provider_hash, provider_file_count = provider_tree_sha256(provider)
    input_identity = {
        "data_as_of": cutoff.isoformat(),
        "model_artifact_manifest_sha256": model_manifest["manifest_sha256"],
        "model_file_sha256": model_manifest["files"]["model.pkl"],
        "provider_content_sha256": provider_hash,
        "provider_file_count": provider_file_count,
        "schema_version": SHADOW_SCHEMA_VERSION,
        "trade_date": signal_day.isoformat(),
    }
    input_hash = _content_sha256(input_identity)
    target = _shadow_target(Path(archive_root).resolve(), signal_day)
    existing_result = None
    if target.exists():
        existing_result, _ = _verify_existing_shadow(
            target,
            input_content_sha256=input_hash,
        )

    predictions, inference = predict_with_frozen_model(
        model=model,
        provider_uri=provider,
        data_as_of=cutoff,
    )
    batches = build_candidate_batches(
        predictions=predictions,
        provider_uri=provider,
        calendar=calendar,
    )
    matches = [item for item in batches if item["trade_date"] == signal_day.isoformat()]
    if len(matches) != 1 or matches[0]["data_cutoff_date"] != cutoff.isoformat():
        raise QlibShadowError("inference did not produce exactly one T-1 candidate batch")
    archive_time = (
        _generated_at(existing_result["generated_at"])
        if existing_result is not None
        else _generated_at(generated_at)
    )
    result = _shadow_payload(
        matches[0],
        trade_date=signal_day,
        data_as_of=cutoff,
        generated_at=archive_time,
        input_content_sha256=input_hash,
        model_manifest_sha256=model_manifest["manifest_sha256"],
        provider_content_sha256=provider_hash,
        inference=inference,
    )
    if existing_result is not None:
        _, manifest = _verify_existing_shadow(
            target,
            input_content_sha256=input_hash,
            expected_result=result,
        )
        return {
            "status": "exists",
            "archive_dir": str(target),
            "manifest": manifest,
            "result": existing_result,
        }

    result_bytes = _strict_json_bytes(result)
    manifest = _manifest_with_hash(
        {
            "created_at": result["generated_at"],
            "files": {"result.json": _sha256_bytes(result_bytes)},
            "input_content_sha256": input_hash,
            "model_artifact_manifest_sha256": model_manifest["manifest_sha256"],
            "result_content_sha256": _content_sha256(result),
            "run_sha256": result["run_sha256"],
            "schema_version": SHADOW_SCHEMA_VERSION,
        }
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        (temporary / "result.json").write_bytes(result_bytes)
        (temporary / "manifest.json").write_bytes(_strict_json_bytes(manifest))
        try:
            os.rename(temporary, target)
        except OSError:
            if target.exists():
                shutil.rmtree(temporary, ignore_errors=True)
                _verify_existing_shadow(
                    target,
                    input_content_sha256=input_hash,
                    expected_result=result,
                )
                raise QlibShadowConflictError(
                    "same-day archive appeared concurrently; rerun to verify"
                )
            raise
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "status": "created",
        "archive_dir": str(target),
        "manifest": manifest,
        "result": result,
    }
