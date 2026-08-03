"""Command-line entry points for the isolated research archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Optional, Sequence

from research.archive import (
    ArchiveConflictError,
    SignalValidationError,
    archive_signals,
    load_source,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.cli",
        description="Daily Stock offline research utilities",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser(
        "archive-signals",
        help="archive an existing V2.1 market-screening JSON artifact",
    )
    archive.add_argument("--input", required=True, type=Path, help="V2.1 JSON artifact")
    archive.add_argument(
        "--output",
        type=Path,
        default=Path("research/data/signals"),
        help="archive root (default: research/data/signals)",
    )
    archive.add_argument(
        "--market-data-at",
        help="ISO-8601 quote timestamp with timezone; required for legacy artifacts",
    )
    archive.add_argument(
        "--batch-id",
        help="stable signal-batch identifier; defaults to the source batch or generated_at",
    )
    archive.add_argument(
        "--source-artifact",
        help="stable artifact label stored in every signal; defaults to the input file name",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "archive-signals":
        raise AssertionError(f"unsupported command: {args.command}")

    try:
        source = load_source(args.input)
        result = archive_signals(
            source,
            output_root=args.output,
            market_data_at=args.market_data_at,
            batch_id=args.batch_id,
            source_artifact=args.source_artifact or args.input.name,
        )
    except SignalValidationError as exc:
        print(f"validation_error: {exc}", file=sys.stderr)
        return 2
    except ArchiveConflictError as exc:
        print(f"archive_conflict: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"archive_error: {exc}", file=sys.stderr)
        return 4

    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
