"""Opt-in real-source Phase 2B raw-history acceptance smoke.

Only a metadata/hash manifest is written. Raw provider rows never leave memory.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.benchmarks.raw_history import evaluate_raw_history  # noqa: E402
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes  # noqa: E402
from research.data_sources.raw_history import fetch_raw_history_pair  # noqa: E402
from research.data_sources.trade_calendar import (  # noqa: E402
    fetch_verified_trade_calendar,
)


SAMPLES = (
    {"label": "sh_large_cap", "symbol": "600519", "start": "2026-07-01"},
    {"label": "sz_large_cap", "symbol": "000001", "start": "2026-07-01"},
    {"label": "sh_volatile", "symbol": "600734", "start": "2026-07-01"},
    {
        "label": "suspension_resumption_boundary",
        "symbol": "000029",
        "start": "2020-10-26",
        "end": "2020-11-20",
    },
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="explicitly permit the two free read-only provider calls",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/runtime/raw-history-acceptance.json"),
    )
    parser.add_argument(
        "--current-sample-end",
        type=date.fromisoformat,
        help=(
            "explicit completed end date for the three current samples; "
            "omitting it requires the live frozen cutoff"
        ),
    )
    args = parser.parse_args()
    if not args.allow_network:
        parser.error("--allow-network is required; network is disabled by default")
    args.output.unlink(missing_ok=True)

    request_at = datetime.now(SHANGHAI_TZ)
    calendar_start = min(date.fromisoformat(item["start"]) for item in SAMPLES)
    calendar_end = request_at.date()
    calendar = fetch_verified_trade_calendar(
        calendar_start,
        calendar_end,
        allow_network=True,
    )
    cutoff = calendar.previous_completed_trade_date(request_at)
    results = []
    for sample in SAMPLES:
        start = date.fromisoformat(sample["start"])
        end = (
            date.fromisoformat(sample["end"])
            if "end" in sample
            else args.current_sample_end or cutoff
        )
        if end > cutoff:
            parser.error("--current-sample-end cannot be later than frozen cutoff")
        primary, cross = fetch_raw_history_pair(
            sample["symbol"], start, end, allow_network=True
        )
        acceptance = evaluate_raw_history(
            calendar=calendar,
            request_at=request_at,
            market_data_at=datetime.now(SHANGHAI_TZ),
            primary=primary,
            cross=cross,
        )
        results.append({"label": sample["label"], **acceptance.manifest})

    payload = {
        "evidence_type": "sanitized_metadata_and_hashes_only",
        "raw_rows_persisted": False,
        "sample_count": len(results),
        "samples": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(payload) + b"\n")
    print(json.dumps({
        "output": str(args.output),
        "sample_count": len(results),
        "statuses": [item["acceptance_status"] for item in results],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
