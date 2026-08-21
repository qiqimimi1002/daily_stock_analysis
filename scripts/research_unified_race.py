"""Evaluate a private aligned Benchmark bundle and write aggregates only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.benchmarks.schema import canonical_json_bytes  # noqa: E402
from research.benchmarks.unified_race import evaluate_unified_race  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Private aligned input bundle; never copied into the output.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/runtime/unified-race-summary.json"),
        help="Sanitized aggregate output (default is gitignored).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        summary = evaluate_unified_race(payload)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json_bytes(summary) + b"\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"unified race failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "evidence_status": summary["evidence_status"],
                "output": str(args.output),
                "output_content_sha256": summary["output_content_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
