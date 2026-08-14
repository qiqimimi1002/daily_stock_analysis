#!/usr/bin/env python3
"""Fail before deep analysis when the run-scoped market snapshot is invalid."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_provider.market_snapshot import (  # noqa: E402
    MarketSnapshotError,
    validate_market_snapshot,
)


def write_error_report(
    path: Path,
    *,
    snapshot_path: Path,
    codes: list[str],
    error: MarketSnapshotError,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# 行情快照校验失败",
                "",
                "Daily Stock 深度分析未启动，未切换到其他行情源或历史收盘价。",
                "",
                f"- 快照：`{snapshot_path.as_posix()}`",
                f"- 候选：`{','.join(codes)}`",
                f"- 错误：`{error}`",
                "",
            ]
        ),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument(
        "--error-report",
        type=Path,
        default=Path("reports/market_snapshot_error.md"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codes = [code.strip() for code in args.codes.split(",") if code.strip()]
    try:
        if not codes:
            raise MarketSnapshotError("market snapshot selected codes are empty")
        validate_market_snapshot(args.snapshot, codes)
    except MarketSnapshotError as exc:
        write_error_report(
            args.error_report,
            snapshot_path=args.snapshot,
            codes=codes,
            error=exc,
        )
        print(f"market_snapshot_validation_error: {exc}", file=sys.stderr)
        return 2
    print(f"market_snapshot_validation_ok: {len(codes)} quote(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
