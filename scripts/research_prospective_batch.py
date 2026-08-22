"""Archive one already-acquired private prospective shared-evidence bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.prospective_batch import (  # noqa: E402
    ProspectiveBatchError,
    capture_prospective_batch,
    load_private_bundle,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and immutably archive one private same-day shared "
            "Short-term v1 / Phase 2A evidence bundle. No network is used."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--public-manifest", type=Path)
    return parser


def _inside_project(path: Path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    args = _parser().parse_args()
    if _inside_project(args.input) or _inside_project(args.private_root):
        print(
            "FAIL_CLOSED reason_code=private_boundary_required",
            file=sys.stderr,
        )
        return 1
    try:
        bundle = load_private_bundle(args.input)
        result = capture_prospective_batch(
            bundle,
            private_root=args.private_root,
            public_manifest_path=args.public_manifest,
        )
    except ProspectiveBatchError as exc:
        print(f"FAIL_CLOSED reason_code={exc.reason_code}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "batch_id": result.batch_id,
                "private_content_sha256": result.private_content_sha256,
                "private_manifest_sha256": result.private_manifest_sha256,
                "public_manifest_sha256": result.public_manifest["manifest_sha256"],
                "status": result.status,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
