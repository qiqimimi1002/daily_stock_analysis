#!/usr/bin/env python3
"""Explicit network smoke check for the Phase 2B-0B1 calendar sources."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.benchmarks.trade_calendar import (  # noqa: E402
    TradeCalendarContractError,
)
from research.data_sources.trade_calendar import (  # noqa: E402
    fetch_verified_trade_calendar,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read Baostock and AKShare calendars, fail closed on any difference, "
            "and write only the verified normalized result."
        )
    )
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Required explicit opt-in; omitted by all offline tests.",
    )
    parser.add_argument(
        "--output",
        help="Verified JSON output path; defaults to a new system temp directory.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        calendar = fetch_verified_trade_calendar(
            args.start_date,
            args.end_date,
            allow_network=args.allow_network,
        )
    except TradeCalendarContractError as exc:
        print(f"FAIL_CLOSED: {exc}", file=sys.stderr)
        return 1

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path(
            tempfile.mkdtemp(prefix="phase2b-0b1-trade-calendar-")
        ) / "verified-calendar.json"
    output_path.write_bytes(calendar.serialize())
    print(
        "PASS "
        f"primary_count={calendar.primary_count} "
        f"cross_count={calendar.cross_count} "
        f"content_sha256={calendar.content_sha256} "
        f"output={output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
