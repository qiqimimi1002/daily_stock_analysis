"""Immutable prospective shadow archive for one frozen DoubleEnsemble model."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
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
FROZEN_SCHEMA_VERSION = "qlib-doubleensemble-frozen-model-v2"
TRAINING_ATTEMPT_SCHEMA_VERSION = "qlib-doubleensemble-training-attempt-v1"
TRAINING_RECORD_SCHEMA_VERSION = "qlib-doubleensemble-training-record-v1"
SHADOW_SCHEMA_VERSION = "qlib-doubleensemble-prospective-shadow-v1"
FROZEN_MODEL_VERSION = "qlib-alpha158-doubleensemble-prospective-v1"
ACCEPTANCE_TRADE_DATE = date(2026, 8, 24)
ACCEPTANCE_DATA_AS_OF = date(2026, 8, 21)
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


def _training_attempt_path(artifact_dir: Path) -> Path:
    return artifact_dir.with_name(f"{artifact_dir.name}.training-attempt.json")


def _write_training_attempt(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    attempt = _manifest_with_hash(value)
    try:
        with path.open("xb") as handle:
            handle.write(_strict_json_bytes(attempt))
    except FileExistsError:
        raise QlibShadowConflictError(
            "prospective-v1 training was already attempted; retraining is prohibited"
        ) from None
    return attempt


def _load_artifact_model(target: Path, expected_sha256: str) -> Any:
    model_bytes = (target / "model.pkl").read_bytes()
    if _sha256_bytes(model_bytes) != expected_sha256:
        raise QlibShadowConflictError("frozen model file hash mismatch")
    try:
        return pickle.loads(model_bytes)
    except Exception as exc:
        raise QlibShadowConflictError("frozen model cannot be loaded") from exc


def _acceptance_inference(
    *,
    target: Path,
    model_file_sha256: str,
    provider: Path,
) -> dict[str, Any]:
    model = _load_artifact_model(target, model_file_sha256)
    predictions, inference = predict_with_frozen_model(
        model=model,
        provider_uri=provider,
        data_as_of=ACCEPTANCE_DATA_AS_OF,
    )
    calendar = (provider / "calendars" / "day.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    batches = build_candidate_batches(
        predictions=predictions,
        provider_uri=provider,
        calendar=calendar,
    )
    matches = [
        item
        for item in batches
        if item["trade_date"] == ACCEPTANCE_TRADE_DATE.isoformat()
    ]
    if (
        len(matches) != 1
        or matches[0]["data_cutoff_date"] != ACCEPTANCE_DATA_AS_OF.isoformat()
    ):
        raise QlibShadowError(
            "frozen-model acceptance did not produce exactly one T-1 batch"
        )
    return {"candidate_batch": matches[0], "inference": inference}


def verify_frozen_model_artifact(
    artifact_dir: Path | str,
    *,
    expected_manifest_sha256: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Verify all hashes before loading the local frozen model pickle."""

    target = Path(artifact_dir).resolve()
    manifest = _load_manifest(target / "manifest.json")
    if (
        expected_manifest_sha256 is not None
        and manifest.get("manifest_sha256") != expected_manifest_sha256
    ):
        raise QlibShadowConflictError("frozen-model pinned identity differs")
    _verify_exact_files(
        target,
        manifest=manifest,
        expected_names={
            "acceptance.json",
            "manifest.json",
            "model.pkl",
            "training.json",
        },
    )
    if manifest.get("schema_version") != FROZEN_SCHEMA_VERSION:
        raise QlibShadowConflictError("frozen-model schema differs")
    if manifest.get("model_version") != FROZEN_MODEL_VERSION:
        raise QlibShadowConflictError("frozen-model version differs")
    if manifest.get("qlib_version") != QLIB_PACKAGE_VERSION:
        raise QlibShadowConflictError("frozen-model Qlib version differs")
    if manifest.get("model_config_sha256") != model_config_sha256():
        raise QlibShadowConflictError("frozen-model config hash differs")
    if manifest.get("segments") != APPROVED_SPLITS.to_dict():
        raise QlibShadowConflictError("frozen-model time segments differ")
    if manifest.get("status") != "accepted":
        raise QlibShadowConflictError("frozen-model status differs")
    training = _load_manifest(target / "training.json")
    acceptance = _load_manifest(target / "acceptance.json")
    attempt = _load_manifest(_training_attempt_path(target))
    if manifest.get("training_manifest_sha256") != training["manifest_sha256"]:
        raise QlibShadowConflictError("training manifest identity differs")
    if manifest.get("training_attempt_sha256") != attempt["manifest_sha256"]:
        raise QlibShadowConflictError("training attempt identity differs")
    if (
        manifest.get("acceptance_manifest_sha256")
        != acceptance["manifest_sha256"]
    ):
        raise QlibShadowConflictError("acceptance manifest identity differs")
    if training.get("model_version") != FROZEN_MODEL_VERSION:
        raise QlibShadowConflictError("training model version differs")
    if training.get("status") != "trained_once":
        raise QlibShadowConflictError("training status differs")
    if attempt.get("status") != "started":
        raise QlibShadowConflictError("training attempt status differs")
    if acceptance.get("status") != "matched":
        raise QlibShadowConflictError("same-artifact acceptance did not pass")
    if (
        acceptance.get("first_inference_sha256")
        != acceptance.get("second_inference_sha256")
    ):
        raise QlibShadowConflictError("same-artifact inference hashes differ")
    model = _load_artifact_model(target, manifest["files"]["model.pkl"])
    return model, manifest


def freeze_model_artifact(
    *,
    provider_uri: Path | str,
    artifact_dir: Path | str,
    generated_at: datetime | str | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Train prospective-v1 once, save it, then verify two loads infer identically."""

    target = Path(artifact_dir).resolve()
    if target.exists():
        try:
            _, manifest = verify_frozen_model_artifact(
                target,
                expected_manifest_sha256=expected_manifest_sha256,
            )
        except QlibShadowError as exc:
            raise QlibShadowConflictError(
                "prospective-v1 artifact is incomplete; retraining is prohibited"
            ) from exc
        return {
            "operation_status": "exists",
            "artifact_dir": str(target),
            **manifest,
        }

    provider = Path(provider_uri).resolve()
    provider_hash, provider_file_count = provider_tree_sha256(provider)
    created_at = _generated_at(generated_at).isoformat(timespec="seconds")
    target.parent.mkdir(parents=True, exist_ok=True)
    attempt_path = _training_attempt_path(target)
    if attempt_path.exists():
        _load_manifest(attempt_path)
        raise QlibShadowError(
            "prospective-v1 training was already attempted; retraining is prohibited"
        )
    attempt = _write_training_attempt(
        attempt_path,
        {
            "attempted_at": created_at,
            "model_config_sha256": model_config_sha256(),
            "model_version": FROZEN_MODEL_VERSION,
            "provider_file_count": provider_file_count,
            "qlib_version": QLIB_PACKAGE_VERSION,
            "random_seed": RANDOM_SEED,
            "schema_version": TRAINING_ATTEMPT_SCHEMA_VERSION,
            "segments": APPROVED_SPLITS.to_dict(),
            "status": "started",
            "training_input_sha256": provider_hash,
        },
    )
    model, _, run_manifest = fit_model_and_predict(
        provider_uri=provider,
        splits=APPROVED_SPLITS,
    )
    try:
        model_bytes = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        raise QlibShadowError("DoubleEnsemble model cannot be serialized") from exc
    provider_manifest = json.loads(
        (provider / "metadata" / "manifest.json").read_text(encoding="utf-8")
    )
    model_file_sha256 = _sha256_bytes(model_bytes)
    training = _manifest_with_hash(
        {
            "created_at": created_at,
            "model_file_sha256": model_file_sha256,
            "model_config": model_config_payload(),
            "model_config_sha256": model_config_sha256(),
            "model_version": FROZEN_MODEL_VERSION,
            "provider_file_count": provider_file_count,
            "provider_manifest_sha256": provider_manifest.get("manifest_sha256"),
            "qlib_version": QLIB_PACKAGE_VERSION,
            "random_seed": RANDOM_SEED,
            "run_manifest": run_manifest,
            "schema_version": TRAINING_RECORD_SCHEMA_VERSION,
            "segments": APPROVED_SPLITS.to_dict(),
            "status": "trained_once",
            "training_attempt_sha256": attempt["manifest_sha256"],
            "training_input_sha256": provider_hash,
        }
    )
    training_bytes = _strict_json_bytes(training)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        (temporary / "model.pkl").write_bytes(model_bytes)
        (temporary / "training.json").write_bytes(training_bytes)
        try:
            os.rename(temporary, target)
        except OSError:
            shutil.rmtree(temporary, ignore_errors=True)
            raise QlibShadowConflictError(
                "prospective-v1 artifact path appeared during one-time training"
            ) from None
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    first = _acceptance_inference(
        target=target,
        model_file_sha256=model_file_sha256,
        provider=provider,
    )
    second = _acceptance_inference(
        target=target,
        model_file_sha256=model_file_sha256,
        provider=provider,
    )
    first_hash = _content_sha256(first)
    second_hash = _content_sha256(second)
    if first_hash != second_hash or canonical_json_bytes(first) != canonical_json_bytes(
        second
    ):
        raise QlibShadowConflictError(
            "same frozen artifact produced inconsistent repeated inference"
        )
    acceptance = _manifest_with_hash(
        {
            "candidate_batch": first["candidate_batch"],
            "candidate_count": len(first["candidate_batch"].get("candidates", [])),
            "comparison": "exact_canonical_json",
            "data_as_of": ACCEPTANCE_DATA_AS_OF.isoformat(),
            "first_inference_sha256": first_hash,
            "inference": first["inference"],
            "model_file_sha256": model_file_sha256,
            "repeated_load_count": 2,
            "second_inference_sha256": second_hash,
            "status": "matched",
            "trade_date": ACCEPTANCE_TRADE_DATE.isoformat(),
        }
    )
    acceptance_bytes = _strict_json_bytes(acceptance)
    manifest = _manifest_with_hash(
        {
            "acceptance_manifest_sha256": acceptance["manifest_sha256"],
            "created_at": created_at,
            "files": {
                "acceptance.json": _sha256_bytes(acceptance_bytes),
                "model.pkl": model_file_sha256,
                "training.json": _sha256_bytes(training_bytes),
            },
            "model_config_sha256": model_config_sha256(),
            "model_config_version": MODEL_CONFIG_VERSION,
            "model_version": FROZEN_MODEL_VERSION,
            "qlib_version": QLIB_PACKAGE_VERSION,
            "schema_version": FROZEN_SCHEMA_VERSION,
            "segments": APPROVED_SPLITS.to_dict(),
            "status": "accepted",
            "training_attempt_sha256": attempt["manifest_sha256"],
            "training_input_sha256": provider_hash,
            "training_manifest_sha256": training["manifest_sha256"],
        }
    )
    try:
        with (target / "acceptance.json").open("xb") as handle:
            handle.write(acceptance_bytes)
        with (target / "manifest.json").open("xb") as handle:
            handle.write(_strict_json_bytes(manifest))
    except OSError as exc:
        raise QlibShadowConflictError(
            "prospective-v1 acceptance files cannot be written immutably"
        ) from exc
    verify_frozen_model_artifact(
        target,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    return {
        "operation_status": "created",
        "artifact_dir": str(target),
        **manifest,
    }


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
    model_version: str,
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
            "model_version": model_version,
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
        "model_version": model_version,
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
    expected_model_manifest_sha256: str | None = None,
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
    model, model_manifest = verify_frozen_model_artifact(
        artifact_dir,
        expected_manifest_sha256=expected_model_manifest_sha256,
    )
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
        model_version=model_manifest["model_version"],
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
