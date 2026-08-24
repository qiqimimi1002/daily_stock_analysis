from __future__ import annotations

import json
from pathlib import Path
import pickle
from types import SimpleNamespace

import pandas as pd
import pytest

import research.benchmarks.qlib_shadow as shadow
import scripts.research_qlib_doubleensemble as cli
from research.benchmarks.qlib_doubleensemble import (
    MODEL_CONFIG_VERSION,
    QLIB_PACKAGE_VERSION,
    build_candidate_batches,
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
        lambda artifact: (
            object(),
            {
                "files": {"model.pkl": "b" * 64},
                "manifest_sha256": "c" * 64,
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
    target.mkdir(parents=True)
    model_bytes = pickle.dumps({"frozen": True})
    replay_bytes = b"{}\n"
    manifest = shadow._manifest_with_hash(
        {
            "files": {
                "model.pkl": shadow._sha256_bytes(model_bytes),
                "replay.json": shadow._sha256_bytes(replay_bytes),
            },
            "model_config_sha256": model_config_sha256(),
            "model_version": MODEL_CONFIG_VERSION,
            "qlib_version": QLIB_PACKAGE_VERSION,
            "schema_version": shadow.FROZEN_SCHEMA_VERSION,
            "segments": shadow.APPROVED_SPLITS.to_dict(),
        }
    )
    (target / "model.pkl").write_bytes(model_bytes)
    (target / "replay.json").write_bytes(replay_bytes)
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
        expected_candidates_path=tmp_path / "missing-candidates.json",
    )
    assert result["status"] == "exists"


def test_frozen_artifact_tamper_fails_closed(tmp_path):
    target = tmp_path / "frozen"
    _write_fake_frozen_artifact(target)
    (target / "model.pkl").write_bytes(b"changed")
    with pytest.raises(shadow.QlibShadowConflictError, match="file hash mismatch"):
        shadow.verify_frozen_model_artifact(target)


def test_one_time_freeze_requires_matching_replay(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    predictions = _predictions()
    expected = build_candidate_batches(
        predictions=predictions,
        provider_uri=provider,
        calendar=["2026-08-21", "2026-08-24"],
    )
    expected_path = tmp_path / "candidates.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.setattr(
        shadow,
        "fit_model_and_predict",
        lambda **kwargs: (
            {"frozen": True},
            predictions,
            {"qlib_version": QLIB_PACKAGE_VERSION},
        ),
    )
    target = tmp_path / "frozen"
    result = shadow.freeze_model_artifact(
        provider_uri=provider,
        artifact_dir=target,
        expected_candidates_path=expected_path,
        generated_at="2026-08-24T16:00:00+08:00",
    )
    model, manifest = shadow.verify_frozen_model_artifact(target)
    assert result["status"] == "created"
    assert model == {"frozen": True}
    assert manifest["replay"]["status"] == "matched"
    assert manifest["training_input_sha256"] == shadow.provider_tree_sha256(provider)[0]


def test_one_time_freeze_mismatch_writes_no_model(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    expected = build_candidate_batches(
        predictions=_predictions(),
        provider_uri=provider,
        calendar=["2026-08-21", "2026-08-24"],
    )
    expected_path = tmp_path / "candidates.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.setattr(
        shadow,
        "fit_model_and_predict",
        lambda **kwargs: (
            {"frozen": True},
            _predictions([1.0, 2.0, 2.0, 8.0, 3.0, 0.0]),
            {"qlib_version": QLIB_PACKAGE_VERSION},
        ),
    )
    target = tmp_path / "frozen"
    with pytest.raises(shadow.QlibShadowError, match="score exceeds tolerance"):
        shadow.freeze_model_artifact(
            provider_uri=provider,
            artifact_dir=target,
            expected_candidates_path=expected_path,
        )
    assert not target.exists()


def test_replay_failure_receipt_prohibits_second_fit(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    predictions = _predictions()
    expected = build_candidate_batches(
        predictions=predictions,
        provider_uri=provider,
        calendar=["2026-08-21", "2026-08-24"],
    )
    expected_path = tmp_path / "candidates.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    target = tmp_path / "frozen"
    receipt = shadow.record_replay_failure(
        provider_uri=provider,
        artifact_dir=target,
        expected_candidates_path=expected_path,
        reason_code="score_exceeds_tolerance",
        attempted_at="2026-08-24T16:52:10+08:00",
    )

    def forbidden_fit(**kwargs):
        raise AssertionError("a failed replay must never be repeated")

    monkeypatch.setattr(shadow, "fit_model_and_predict", forbidden_fit)
    with pytest.raises(shadow.QlibShadowError, match="retraining is prohibited"):
        shadow.freeze_model_artifact(
            provider_uri=provider,
            artifact_dir=target,
            expected_candidates_path=expected_path,
        )
    with pytest.raises(shadow.QlibShadowError, match="one-time replay failed"):
        shadow.verify_frozen_model_artifact(target)
    assert receipt["status"] == "failed_closed"
    assert receipt["reason_code"] == "score_exceeds_tolerance"
    assert not target.exists()


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        (cli.freeze_model, "retraining is prohibited"),
        (cli.shadow, "prospective shadow is disabled"),
    ],
)
def test_committed_replay_failure_disables_cli_before_work(
    tmp_path, monkeypatch, entry, message
):
    evidence = tmp_path / "replay-failed.json"
    evidence.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "REPLAY_FAILURE_EVIDENCE", evidence)
    with pytest.raises(RuntimeError, match=message):
        entry(SimpleNamespace())
