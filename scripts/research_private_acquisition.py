#!/usr/bin/env python3
"""Explicit network entrypoint for prospective Private shared-batch acquisition."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.private_acquisition import (  # noqa: E402
    PrivateAcquisitionError,
    acquire_private_shared_batch,
)
from research.prospective_batch import (  # noqa: E402
    ProspectiveBatchError,
    load_private_bundle,
)


def _outside_repository(path: Path, *, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError:
        return resolved
    raise PrivateAcquisitionError(
        "private_boundary_failed", f"{label} must be outside the repository"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acquire and atomically freeze one research-only shared batch."
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--public-manifest", type=Path)
    parser.add_argument(
        "--deadline-at",
        required=True,
        help="Asia/Shanghai timestamp before which the archive must be completed.",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Required explicit opt-in; offline tests never set it.",
    )
    args = parser.parse_args()

    try:
        request_path = _outside_repository(args.request, label="Private request")
        private_root = _outside_repository(args.private_root, label="Private root")
        request = load_private_bundle(request_path)
        result = acquire_private_shared_batch(
            request,
            private_root=private_root,
            public_manifest_path=args.public_manifest,
            allow_network=args.allow_network,
            deadline_at=args.deadline_at,
        )
    except (PrivateAcquisitionError, ProspectiveBatchError) as exc:
        reason = getattr(exc, "reason_code", "acquisition_failed")
        print(f"FAIL_CLOSED reason_code={reason}", file=sys.stderr)
        return 1

    print(
        f"PASS status={result.capture.status} "
        f"batch_id={result.capture.batch_id} "
        f"symbol_count={result.symbol_count} "
        f"shared_evidence_sha256="
        f"{next(iter(result.capture.public_manifest['model_bindings'].values()))['shared_evidence_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
