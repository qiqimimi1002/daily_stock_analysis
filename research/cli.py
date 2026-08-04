"""Command-line entry points for the isolated research archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Optional, Sequence

from research.archive import (
    ArchiveConflictError,
    MARKET_DATA_AT_PRECISIONS,
    MARKET_DATA_AT_SOURCES,
    SignalValidationError,
    archive_signals,
    load_source_artifact,
)
from research.outcomes import (
    DEFAULT_CALCULATION_VERSION,
    OutcomeConflictError,
    OutcomeValidationError,
    calculate_outcomes,
    load_price_artifact,
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
        "--market-data-at-source",
        choices=sorted(MARKET_DATA_AT_SOURCES),
        help=(
            "provenance of the quote timestamp; required with --market-data-at "
            "and must be artifact_field for an artifact timestamp"
        ),
    )
    archive.add_argument(
        "--market-data-at-precision",
        choices=sorted(MARKET_DATA_AT_PRECISIONS),
        help=(
            "precision of the quote timestamp; required with --market-data-at "
            "and never inferred"
        ),
    )
    archive.add_argument(
        "--batch-id",
        help="stable signal-batch identifier; defaults to the source batch or generated_at",
    )
    archive.add_argument(
        "--source-artifact",
        help="stable artifact label stored in every signal; defaults to the input file name",
    )
    outcomes = commands.add_parser(
        "calculate-outcomes",
        help="calculate forward outcomes from immutable phase-1 signal archives",
    )
    outcomes.add_argument("--signals", required=True, type=Path, help="phase-1 signal batch or archive root")
    outcomes.add_argument("--prices", required=True, type=Path, help="unadjusted OHLC price JSON")
    outcomes.add_argument(
        "--output",
        type=Path,
        default=Path("research/data/outcomes"),
        help="outcome archive root (default: research/data/outcomes)",
    )
    outcomes.add_argument("--as-of", required=True, help="evaluation cutoff with timezone")
    outcomes.add_argument(
        "--calculation-version",
        default=DEFAULT_CALCULATION_VERSION,
        help=f"calculation contract version (default: {DEFAULT_CALCULATION_VERSION})",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "archive-signals":
            loaded = load_source_artifact(args.input)
            result = archive_signals(
                loaded.source,
                output_root=args.output,
                source_file_sha256=loaded.source_file_sha256,
                market_data_at=args.market_data_at,
                market_data_at_source=args.market_data_at_source,
                market_data_at_precision=args.market_data_at_precision,
                batch_id=args.batch_id,
                source_artifact=args.source_artifact or args.input.name,
            )
        elif args.command == "calculate-outcomes":
            loaded_prices = load_price_artifact(args.prices)
            result = calculate_outcomes(
                args.signals,
                loaded_prices.source,
                price_file_sha256=loaded_prices.source_file_sha256,
                output_root=args.output,
                as_of=args.as_of,
                calculation_version=args.calculation_version,
            )
        else:
            raise AssertionError(f"unsupported command: {args.command}")
    except (SignalValidationError, OutcomeValidationError) as exc:
        print(f"validation_error: {exc}", file=sys.stderr)
        return 2
    except (ArchiveConflictError, OutcomeConflictError) as exc:
        print(f"archive_conflict: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"archive_error: {exc}", file=sys.stderr)
        return 4

    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
