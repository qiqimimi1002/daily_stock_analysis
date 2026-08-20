#!/usr/bin/env python3
"""Fail before deep analysis when the same-run technical snapshot is invalid."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_provider.technical_snapshot import (  # noqa: E402
    TechnicalSnapshotError,
    validate_technical_snapshot,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-number", required=True)
    parser.add_argument(
        "--error-report",
        type=Path,
        default=Path("reports/technical_snapshot_error.md"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codes = [code.strip() for code in args.codes.split(",") if code.strip()]
    try:
        if not codes:
            raise TechnicalSnapshotError("technical snapshot selected codes are empty")
        validate_technical_snapshot(
            args.snapshot,
            codes,
            expected_run_id=args.run_id,
            expected_run_number=args.run_number,
        )
    except TechnicalSnapshotError as exc:
        args.error_report.parent.mkdir(parents=True, exist_ok=True)
        args.error_report.write_text(
            "\n".join(
                [
                    "# 技术快照校验失败",
                    "",
                    "Daily Stock 深度分析未启动，未回退到盘中实时均线重算。",
                    "",
                    f"- 快照：`{args.snapshot.as_posix()}`",
                    f"- 候选：`{','.join(codes)}`",
                    f"- 错误：`{exc}`",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"technical_snapshot_validation_error: {exc}", file=sys.stderr)
        return 2
    print(f"technical_snapshot_validation_ok: {len(codes)} context(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
