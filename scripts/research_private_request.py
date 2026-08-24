#!/usr/bin/env python3
"""Explicit network entrypoint for one same-day Private acquisition request."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.private_acquisition import PrivateAcquisitionError  # noqa: E402
from research.private_request import prepare_private_acquisition_request  # noqa: E402


def _outside_repository(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return resolved
    raise PrivateAcquisitionError(
        "private_boundary_failed", "Private request must be outside the repository"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare one complete prospective Private acquisition request."
    )
    parser.add_argument("--signal-date", type=date.fromisoformat, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--deadline-at", required=True)
    parser.add_argument(
        "--confirm-private-provider-terms-reviewed",
        action="store_true",
        help="Required operator confirmation for Private-only source capture.",
    )
    parser.add_argument("--allow-network", action="store_true")
    args = parser.parse_args()

    try:
        result = prepare_private_acquisition_request(
            signal_date=args.signal_date,
            request_path=_outside_repository(args.request),
            deadline_at=args.deadline_at,
            provider_terms_reviewed_for_private_capture=(
                args.confirm_private_provider_terms_reviewed
            ),
            allow_network=args.allow_network,
        )
    except PrivateAcquisitionError as exc:
        print(f"FAIL_CLOSED reason_code={exc.reason_code}", file=sys.stderr)
        return 1
    print(
        f"PASS status={result.status} spot_row_count={result.spot_row_count} "
        f"universe_count={result.universe_count} "
        f"universe_sha256={result.universe_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
