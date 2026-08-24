#!/usr/bin/env python3
"""Offline entry point for Qlib Alpha158 + DoubleEnsemble research shadow."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
REPLAY_FAILURE_EVIDENCE = (
    REPO_ROOT
    / "research"
    / "results"
    / "qlib_doubleensemble_freeze_replay_2026-08-24.json"
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.benchmarks.qlib_doubleensemble import (  # noqa: E402
    TimeSplits,
    baseline_batches,
    build_candidate_batches,
    build_qlib_provider,
    evaluate_out_of_sample,
    fit_and_predict,
    model_config_payload,
    model_config_sha256,
)
from research.benchmarks.qlib_shadow import (  # noqa: E402
    freeze_model_artifact,
    run_prospective_shadow,
)


def _date_arg(value: str) -> str:
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise argparse.ArgumentTypeError("date must be canonical YYYY-MM-DD")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare(args: argparse.Namespace) -> None:
    """Adapt the existing private raw-history directory to Qlib file storage."""
    source_manifest = json.loads(
        (args.source / "manifest.json").read_text(encoding="utf-8")
    )
    if source_manifest.get("status") != "complete":
        raise RuntimeError("existing source manifest is not complete")
    result = build_qlib_provider(
        source_dir=args.source,
        provider_uri=args.provider,
        replace=args.replace,
        start_date=args.start,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def run(args: argparse.Namespace) -> None:
    """Run the official model and frozen out-of-sample evaluation offline."""
    splits = TimeSplits.create(
        train_start=args.train_start,
        train_end=args.train_end,
        valid_start=args.valid_start,
        valid_end=args.valid_end,
        test_start=args.test_start,
        test_end=args.test_end,
    )
    predictions, run_manifest = fit_and_predict(
        provider_uri=args.provider,
        splits=splits,
    )
    calendar = (args.provider / "calendars" / "day.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    candidates = build_candidate_batches(
        predictions=predictions,
        provider_uri=args.provider,
        calendar=calendar,
    )
    baseline = baseline_batches(
        provider_uri=args.provider,
        candidate_batches=candidates,
    )
    evaluation = evaluate_out_of_sample(
        source_dir=args.source,
        doubleensemble_batches=candidates,
        v21_batches=baseline,
    )
    output = args.output.resolve()
    _write_json(output / "candidates.json", candidates)
    _write_json(output / "v2_1_baseline.json", baseline)
    _write_json(output / "out_of_sample_evaluation.json", evaluation)
    final_manifest = {
        **run_manifest,
        "candidate_batch_count": len(candidates),
        "candidate_nonempty_batch_count": sum(
            bool(item["candidates"]) for item in candidates
        ),
        "model_config": model_config_payload(),
        "model_config_sha256": model_config_sha256(),
        "output_files": [
            "candidates.json",
            "v2_1_baseline.json",
            "out_of_sample_evaluation.json",
        ],
    }
    _write_json(output / "run_manifest.json", final_manifest)
    print(json.dumps(final_manifest, ensure_ascii=False, sort_keys=True))


def freeze_model(args: argparse.Namespace) -> None:
    """Perform the single approved replay and freeze the fitted model."""

    if REPLAY_FAILURE_EVIDENCE.is_file():
        raise RuntimeError(
            "the approved one-time replay is recorded as failed; retraining is prohibited"
        )
    result = freeze_model_artifact(
        provider_uri=args.provider,
        artifact_dir=args.artifact,
        expected_candidates_path=args.expected_candidates,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def shadow(args: argparse.Namespace) -> None:
    """Load the frozen model and immutably archive one prospective day."""

    if REPLAY_FAILURE_EVIDENCE.is_file():
        raise RuntimeError(
            "prospective shadow is disabled because the frozen-model replay failed"
        )
    result = run_prospective_shadow(
        provider_uri=args.provider,
        artifact_dir=args.artifact,
        archive_root=args.archive_root,
        trade_date=args.trade_date,
        data_as_of=args.data_as_of,
        generated_at=args.generated_at,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subparsers = value.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--source", required=True, type=Path)
    prepare_parser.add_argument("--provider", required=True, type=Path)
    prepare_parser.add_argument("--replace", action="store_true")
    prepare_parser.add_argument("--start", type=_date_arg)
    prepare_parser.set_defaults(func=prepare)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--source", required=True, type=Path)
    run_parser.add_argument("--provider", required=True, type=Path)
    run_parser.add_argument("--output", required=True, type=Path)
    for name in (
        "train_start",
        "train_end",
        "valid_start",
        "valid_end",
        "test_start",
        "test_end",
    ):
        run_parser.add_argument(
            f"--{name.replace('_', '-')}", required=True, type=_date_arg
        )
    run_parser.set_defaults(func=run)

    freeze_parser = subparsers.add_parser("freeze-model")
    freeze_parser.add_argument("--provider", required=True, type=Path)
    freeze_parser.add_argument("--artifact", required=True, type=Path)
    freeze_parser.add_argument("--expected-candidates", required=True, type=Path)
    freeze_parser.add_argument("--generated-at")
    freeze_parser.set_defaults(func=freeze_model)

    shadow_parser = subparsers.add_parser("shadow")
    shadow_parser.add_argument("--provider", required=True, type=Path)
    shadow_parser.add_argument("--artifact", required=True, type=Path)
    shadow_parser.add_argument("--archive-root", required=True, type=Path)
    shadow_parser.add_argument("--trade-date", required=True, type=_date_arg)
    shadow_parser.add_argument("--data-as-of", required=True, type=_date_arg)
    shadow_parser.add_argument("--generated-at")
    shadow_parser.set_defaults(func=shadow)
    return value


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
