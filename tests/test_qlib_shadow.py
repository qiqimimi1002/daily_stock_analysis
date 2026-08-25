from __future__ import annotations

import json
from pathlib import Path
import pickle

import pandas as pd
import pytest

import research.benchmarks.qlib_shadow as shadow
from research.benchmarks.qlib_doubleensemble import (
    QLIB_PACKAGE_VERSION,
    model_config_sha256,
)


def _provider(tmp_path: Path) -> Path:
    provider = tmp_path / "provider"
    (provider / "calendars").mkdir(parents=True)
    (provider / "metadata").mkdir()
    (provider / "calendars" / "day.txt").write_text(
        "2026-08-21\n2026-08-24\n", encoding="utf-8"
    )
    (provider / "metadata" / "names.json").write_text(
        json.dumps(
            {f"60000{offset}": f"股票{offset}" for offset in range(6)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (provider / "metadata" / "eligible_rows.parquet").write_bytes(b"test")
    (provider / "metadata" / "manifest.json").write_text(
        json.dumps({"manifest_sha256": "a" * 64}), encoding="utf-8"
    )
    return provider


def _predictions(scores: list[float] | None = None) -> pd.Series:
    values = scores or [1.0, 2.0, 2.0, 4.0, 3.0, 0.0]
    index = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2026-08-21"), f"SH60000{offset}")
            for offset in range(len(values))
        ],
        names=["datetime", "instrument"],
    )
    return pd.Series(values, index=index, name="doubleensemble_score")


def _patch_inference(monkeypatch, scores: list[float] | None = None) -> None:
    monkeypatch.setattr(
        shadow,
        "verify_frozen_model_artifact",
        lambda artifact, **kwargs: (
            object(),
            {
                "files": {"model.pkl": "b" * 64},
                "manifest_sha256": "c" * 64,
                "model_version": shadow.FROZEN_MODEL_VERSION,
            },
        ),
    )
    monkeypatch.setattr(
        shadow,
        "predict_with_frozen_model",
        lambda **kwargs: (
            _predictions(scores),
            {
                "alpha158_complete": True,
                "alpha158_feature_count": 158,
                "data_as_of": "2026-08-21",
                "feature_non_null_rate": 1.0,
                "prediction_count": len(scores or [0] * 6),
                "qlib_version": QLIB_PACKAGE_VERSION,
            },
        ),
    )
    monkeypatch.setattr(
        shadow.pd,
        "read_parquet",
        lambda path: pd.DataFrame({"datetime": [pd.Timestamp("2026-08-21")]}),
    )


def _run(provider: Path, root: Path, *, generated_at: str) -> dict:
    return shadow.run_prospective_shadow(
        provider_uri=provider,
        artifact_dir=provider.parent / "frozen",
        archive_root=root,
        trade_date="2026-08-24",
        data_as_of="2026-08-21",
        generated_at=generated_at,
    )


def test_shadow_archive_is_idempotent_and_preserves_first_generated_at(
    tmp_path, monkeypatch
):
    provider = _provider(tmp_path)
    _patch_inference(monkeypatch)
    archive = tmp_path / "archive"

    first = _run(provider, archive, generated_at="2026-08-24T09:00:00+08:00")
    second = _run(provider, archive, generated_at="2026-08-24T10:00:00+08:00")

    assert first["status"] == "created"
    assert second["status"] == "exists"
    assert second["result"]["generated_at"] == "2026-08-24T09:00:00+08:00"
    assert second["result"]["candidate_count"] == 5
    required = {
        "trade_date",
        "data_as_of",
        "code",
        "name",
        "rank",
        "score",
        "model_version",
        "generated_at",
        "status",
    }
    assert required <= set(second["result"]["candidates"][0])
    assert len(second["manifest"]["manifest_sha256"]) == 64
    assert len(second["result"]["run_sha256"]) == 64


def test_shadow_input_conflict_preserves_original(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    _patch_inference(monkeypatch)
    archive = tmp_path / "archive"
    first = _run(provider, archive, generated_at="2026-08-24T09:00:00+08:00")
    result_path = Path(first["archive_dir"]) / "result.json"
    original = result_path.read_bytes()
    (provider / "metadata" / "names.json").write_text(
        '{"600000":"changed"}', encoding="utf-8"
    )

    with pytest.raises(shadow.QlibShadowConflictError, match="input differs"):
        _run(provider, archive, generated_at="2026-08-24T10:00:00+08:00")
    assert result_path.read_bytes() == original


def test_shadow_result_conflict_preserves_original(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    _patch_inference(monkeypatch)
    archive = tmp_path / "archive"
    first = _run(provider, archive, generated_at="2026-08-24T09:00:00+08:00")
    result_path = Path(first["archive_dir"]) / "result.json"
    original = result_path.read_bytes()
    _patch_inference(monkeypatch, [1.0, 2.0, 2.0, 5.0, 3.0, 0.0])

    with pytest.raises(shadow.QlibShadowConflictError, match="result differs"):
        _run(provider, archive, generated_at="2026-08-24T10:00:00+08:00")
    assert result_path.read_bytes() == original


def test_shadow_tamper_is_detected_before_overwrite(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    _patch_inference(monkeypatch)
    archive = tmp_path / "archive"
    first = _run(provider, archive, generated_at="2026-08-24T09:00:00+08:00")
    result_path = Path(first["archive_dir"]) / "result.json"
    result_path.write_bytes(result_path.read_bytes() + b" ")

    with pytest.raises(shadow.QlibShadowConflictError, match="file hash mismatch"):
        _run(provider, archive, generated_at="2026-08-24T10:00:00+08:00")


def test_shadow_outputs_zero_when_fewer_than_three_scores(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    _patch_inference(monkeypatch, [1.0, 0.5])
    result = _run(
        provider,
        tmp_path / "archive",
        generated_at="2026-08-24T09:00:00+08:00",
    )["result"]
    assert result["candidate_count"] == 0
    assert result["candidates"] == []
    assert result["status"] == "insufficient_reliable_candidates"


def test_shadow_requires_exact_previous_trading_session(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    _patch_inference(monkeypatch)
    with pytest.raises(shadow.QlibShadowError, match="completed T-1"):
        shadow.run_prospective_shadow(
            provider_uri=provider,
            artifact_dir=tmp_path / "frozen",
            archive_root=tmp_path / "archive",
            trade_date="2026-08-24",
            data_as_of="2026-08-20",
        )


def _write_fake_frozen_artifact(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True)
    model_bytes = pickle.dumps({"frozen": True})
    model_hash = shadow._sha256_bytes(model_bytes)
    attempt = shadow._manifest_with_hash(
        {
            "schema_version": shadow.TRAINING_ATTEMPT_SCHEMA_VERSION,
            "status": "started",
        }
    )
    training = shadow._manifest_with_hash(
        {
            "model_file_sha256": model_hash,
            "model_version": shadow.FROZEN_MODEL_VERSION,
            "schema_version": shadow.TRAINING_RECORD_SCHEMA_VERSION,
            "status": "trained_once",
        }
    )
    acceptance = shadow._manifest_with_hash(
        {
            "first_inference_sha256": "d" * 64,
            "second_inference_sha256": "d" * 64,
            "status": "matched",
        }
    )
    training_bytes = shadow._strict_json_bytes(training)
    acceptance_bytes = shadow._strict_json_bytes(acceptance)
    manifest = shadow._manifest_with_hash(
        {
            "acceptance_manifest_sha256": acceptance["manifest_sha256"],
            "files": {
                "acceptance.json": shadow._sha256_bytes(acceptance_bytes),
                "model.pkl": model_hash,
                "training.json": shadow._sha256_bytes(training_bytes),
            },
            "model_config_sha256": model_config_sha256(),
            "model_version": shadow.FROZEN_MODEL_VERSION,
            "qlib_version": QLIB_PACKAGE_VERSION,
            "schema_version": shadow.FROZEN_SCHEMA_VERSION,
            "segments": shadow.APPROVED_SPLITS.to_dict(),
            "status": "accepted",
            "training_attempt_sha256": attempt["manifest_sha256"],
            "training_manifest_sha256": training["manifest_sha256"],
        }
    )
    shadow._training_attempt_path(target).write_bytes(
        shadow._strict_json_bytes(attempt)
    )
    (target / "model.pkl").write_bytes(model_bytes)
    (target / "training.json").write_bytes(training_bytes)
    (target / "acceptance.json").write_bytes(acceptance_bytes)
    (target / "manifest.json").write_bytes(shadow._strict_json_bytes(manifest))


def test_existing_frozen_artifact_never_calls_fit(tmp_path, monkeypatch):
    target = tmp_path / "frozen"
    _write_fake_frozen_artifact(target)

    def forbidden_fit(**kwargs):
        raise AssertionError("fit must not be called for an existing artifact")

    monkeypatch.setattr(shadow, "fit_model_and_predict", forbidden_fit)
    result = shadow.freeze_model_artifact(
        provider_uri=tmp_path / "missing-provider",
        artifact_dir=target,
    )
    assert result["operation_status"] == "exists"
    assert result["status"] == "accepted"


def test_frozen_artifact_tamper_fails_closed(tmp_path):
    target = tmp_path / "frozen"
    _write_fake_frozen_artifact(target)
    (target / "model.pkl").write_bytes(b"changed")
    with pytest.raises(shadow.QlibShadowConflictError, match="file hash mismatch"):
        shadow.verify_frozen_model_artifact(target)


def test_pinned_model_identity_fails_closed(tmp_path):
    target = tmp_path / "frozen"
    _write_fake_frozen_artifact(target)
    with pytest.raises(shadow.QlibShadowConflictError, match="pinned identity"):
        shadow.verify_frozen_model_artifact(
            target,
            expected_manifest_sha256="0" * 64,
        )


def test_training_attempt_cannot_be_overwritten(tmp_path):
    path = tmp_path / "prospective-v1.training-attempt.json"
    first = shadow._write_training_attempt(path, {"status": "first"})
    original = path.read_bytes()
    with pytest.raises(shadow.QlibShadowConflictError, match="already attempted"):
        shadow._write_training_attempt(path, {"status": "second"})
    assert path.read_bytes() == original
    assert first["status"] == "first"


def test_prospective_v1_trains_once_and_accepts_two_identical_loads(
    tmp_path, monkeypatch
):
    provider = _provider(tmp_path)
    predictions = _predictions()
    calls = {"fit": 0, "predict": 0}

    def fit_once(**kwargs):
        calls["fit"] += 1
        return (
            {"frozen": True},
            predictions,
            {"qlib_version": QLIB_PACKAGE_VERSION},
        )

    def infer_loaded(**kwargs):
        calls["predict"] += 1
        return predictions, {"qlib_version": QLIB_PACKAGE_VERSION}

    monkeypatch.setattr(
        shadow,
        "fit_model_and_predict",
        fit_once,
    )
    monkeypatch.setattr(shadow, "predict_with_frozen_model", infer_loaded)
    target = tmp_path / "frozen"
    result = shadow.freeze_model_artifact(
        provider_uri=provider,
        artifact_dir=target,
        generated_at="2026-08-24T16:00:00+08:00",
    )
    model, manifest = shadow.verify_frozen_model_artifact(target)
    existing = shadow.freeze_model_artifact(
        provider_uri=tmp_path / "missing-provider",
        artifact_dir=target,
    )
    acceptance = json.loads((target / "acceptance.json").read_text(encoding="utf-8"))
    assert result["operation_status"] == "created"
    assert existing["operation_status"] == "exists"
    assert result["status"] == "accepted"
    assert model == {"frozen": True}
    assert manifest["model_version"] == shadow.FROZEN_MODEL_VERSION
    assert acceptance["status"] == "matched"
    assert acceptance["first_inference_sha256"] == acceptance["second_inference_sha256"]
    assert calls == {"fit": 1, "predict": 2}
    assert manifest["training_input_sha256"] == shadow.provider_tree_sha256(provider)[0]


def test_inconsistent_same_artifact_inference_leaves_model_but_blocks_retraining(
    tmp_path, monkeypatch
):
    provider = _provider(tmp_path)
    calls = {"fit": 0, "predict": 0}

    def fit_once(**kwargs):
        calls["fit"] += 1
        return {"frozen": True}, _predictions(), {}

    def inconsistent_inference(**kwargs):
        calls["predict"] += 1
        scores = None if calls["predict"] == 1 else [1.0, 2.0, 2.0, 8.0, 3.0, 0.0]
        return _predictions(scores), {}

    monkeypatch.setattr(
        shadow,
        "fit_model_and_predict",
        fit_once,
    )
    monkeypatch.setattr(shadow, "predict_with_frozen_model", inconsistent_inference)
    target = tmp_path / "frozen"
    with pytest.raises(shadow.QlibShadowError, match="inconsistent repeated"):
        shadow.freeze_model_artifact(
            provider_uri=provider,
            artifact_dir=target,
        )
    assert (target / "model.pkl").is_file()
    assert (target / "training.json").is_file()
    assert not (target / "manifest.json").exists()
    assert shadow._training_attempt_path(target).is_file()

    def forbidden_fit(**kwargs):
        raise AssertionError("incomplete prospective-v1 must never retrain")

    monkeypatch.setattr(shadow, "fit_model_and_predict", forbidden_fit)
    with pytest.raises(shadow.QlibShadowError, match="incomplete"):
        shadow.freeze_model_artifact(
            provider_uri=provider,
            artifact_dir=target,
        )
    assert calls == {"fit": 1, "predict": 2}
