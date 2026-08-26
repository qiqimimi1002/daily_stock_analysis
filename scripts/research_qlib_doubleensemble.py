#!/usr/bin/env python3
"""Offline entry point for Qlib Alpha158 + DoubleEnsemble research shadow."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PROSPECTIVE_V1_MANIFEST_SHA256 = (
    "f282bd287fbdc07b06aa493955364ea46b6dd42616a5cdc512a28cd0288fe0ae"
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
from research.benchmarks.qlib_daily import (  # noqa: E402
    QlibDailyError,
    archive_nightly_ready,
    dispatch_morning_quotes,
    prepare_provider_for_completed_date,
    refresh_completed_source,
    verify_fit_count,
)
from research.benchmarks.qlib_shadow import (  # noqa: E402
    freeze_model_artifact,
    run_prospective_shadow,
)


DEFAULT_RUNTIME_ROOT = REPO_ROOT / "research" / "runtime" / "qlib"
DEFAULT_PROVIDER = DEFAULT_RUNTIME_ROOT / "provider"
DEFAULT_ARTIFACT = DEFAULT_RUNTIME_ROOT / "frozen-doubleensemble-prospective-v1"
DEFAULT_SHADOW_ROOT = DEFAULT_RUNTIME_ROOT / "prospective-shadow-v1"
DEFAULT_READY_ROOT = DEFAULT_RUNTIME_ROOT / "nightly-ready-v1"
DEFAULT_FIT_EVIDENCE = (
    REPO_ROOT / "research" / "results" / "qlib_doubleensemble_prospective_v1_2026-08-25.json"
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
    """Train and freeze the single approved prospective-v1 model."""

    result = freeze_model_artifact(
        provider_uri=args.provider,
        artifact_dir=args.artifact,
        generated_at=args.generated_at,
        expected_manifest_sha256=PROSPECTIVE_V1_MANIFEST_SHA256,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def shadow(args: argparse.Namespace) -> None:
    """Load the frozen model and immutably archive one prospective day."""

    result = run_prospective_shadow(
        provider_uri=args.provider,
        artifact_dir=args.artifact,
        archive_root=args.archive_root,
        trade_date=args.trade_date,
        data_as_of=args.data_as_of,
        generated_at=args.generated_at,
        expected_model_manifest_sha256=PROSPECTIVE_V1_MANIFEST_SHA256,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def after_close(args: argparse.Namespace) -> None:
    """Prepare completed T data and freeze the next real session's Top5."""

    source = refresh_completed_source(
        runtime_root=args.runtime_root,
        completed_date=args.date,
    )
    provider = prepare_provider_for_completed_date(
        source_dir=Path(source["source_dir"]),
        provider_uri=args.provider,
        completed_date=args.date,
    )
    artifact_manifest = json.loads(
        (args.artifact / "manifest.json").read_text(encoding="utf-8")
    )
    model_identity = verify_fit_count(
        args.fit_evidence,
        model_manifest=artifact_manifest,
    )
    inference_started = time.perf_counter()
    shadow_result = run_prospective_shadow(
        provider_uri=args.provider,
        artifact_dir=args.artifact,
        archive_root=args.shadow_root,
        trade_date=provider["next_trading_date"],
        data_as_of=args.date,
        generated_at=args.generated_at,
        expected_model_manifest_sha256=PROSPECTIVE_V1_MANIFEST_SHA256,
    )
    stable_preparation = {
        "active_file_count": source["active_file_count"],
        "eligible_target_count": source["eligible_target_count"],
        "eligible_target_sha256": source["eligible_target_sha256"],
        "failure_count": source["failure_count"],
        "inactive_file_count": source["inactive_file_count"],
        "max_complete_trading_date": provider["max_complete_trading_date"],
        "next_trading_date": provider["next_trading_date"],
        "provider_content_sha256": provider["provider_content_sha256"],
        "provider_file_count": provider["provider_file_count"],
        "source_manifest_file_sha256": source["source_manifest_file_sha256"],
        "symbol_file_count": source["symbol_file_count"],
        "target_row_coverage_count": source["target_row_coverage_count"],
    }
    ready = archive_nightly_ready(
        source_dir=Path(source["source_dir"]),
        ready_root=args.ready_root,
        shadow_result=shadow_result["result"],
        shadow_manifest=shadow_result["manifest"],
        preparation=stable_preparation,
        model_identity=model_identity,
    )
    top5_seconds = round(time.perf_counter() - inference_started, 4)
    result = {
        "data_as_of": args.date,
        "data_prepare_seconds": round(
            float(source["refresh_seconds"])
            + float(provider["provider_prepare_seconds"]),
            4,
        ),
        "fit_count": model_identity["fit_count"],
        "model_file_sha256": model_identity["model_file_sha256"],
        "provider": provider,
        "ready_archive_dir": ready["archive_dir"],
        "ready_manifest_sha256": ready["manifest"]["manifest_sha256"],
        "ready_status": ready["operation_status"],
        "source": source,
        "status": "ready",
        "top5": ready["ready"]["candidates"],
        "top5_generation_seconds": top5_seconds,
        "trade_date": provider["next_trading_date"],
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def morning_quotes(args: argparse.Namespace) -> None:
    """Read the previous night's Top5 and dispatch Private quotes only."""

    result = dispatch_morning_quotes(
        ready_root=args.ready_root,
        trade_date=args.trade_date,
        dry_run=args.dry_run,
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

    after_close_parser = subparsers.add_parser("after-close")
    after_close_parser.add_argument("--date", required=True, type=_date_arg)
    after_close_parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    after_close_parser.add_argument("--provider", type=Path, default=DEFAULT_PROVIDER)
    after_close_parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    after_close_parser.add_argument("--shadow-root", type=Path, default=DEFAULT_SHADOW_ROOT)
    after_close_parser.add_argument("--ready-root", type=Path, default=DEFAULT_READY_ROOT)
    after_close_parser.add_argument("--fit-evidence", type=Path, default=DEFAULT_FIT_EVIDENCE)
    after_close_parser.add_argument("--generated-at")
    after_close_parser.set_defaults(func=after_close)

    morning_parser = subparsers.add_parser("morning-quotes")
    morning_parser.add_argument("--trade-date", required=True, type=_date_arg)
    morning_parser.add_argument("--ready-root", type=Path, default=DEFAULT_READY_ROOT)
    morning_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify the frozen nightly package without dispatching Private Tushare",
    )
    morning_parser.set_defaults(func=morning_quotes)
    return value


def main() -> None:
    args = parser().parse_args()
    try:
        args.func(args)
    except QlibDailyError as exc:
        print(
            json.dumps(
                {"error": str(exc), "status": "failed_closed"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
