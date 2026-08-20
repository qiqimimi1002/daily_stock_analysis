#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the A-share main-board market screener from the command line."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.market_screener import (  # noqa: E402
    MarketScreener,
    ScreeningConfig,
    save_result,
)
from src.services.market_screener_diagnostics import (  # noqa: E402
    MarketScreenerDiagnostics,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="沪深主板全市场初筛")
    parser.add_argument("--top-n", type=int, default=5, help="报告保留的候选数量")
    parser.add_argument("--analysis-limit", type=int, default=3, help="交给AI深度分析的数量")
    parser.add_argument("--preselect-limit", type=int, default=60, help="历史数据核验数量")
    parser.add_argument("--history-workers", type=int, default=4, help="历史行情并发数")
    parser.add_argument("--enrichment-limit", type=int, default=8, help="基本面和资金面增强数量")
    parser.add_argument("--evidence-workers", type=int, default=2, help="证据增强并发数")
    parser.add_argument("--evidence-budget-seconds", type=float, default=12.0)
    parser.add_argument("--min-amount-yuan", type=float, default=200_000_000)
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--data-dir", default="data")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True, write_through=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = ScreeningConfig(
        top_n=args.top_n,
        analysis_limit=min(args.analysis_limit, args.top_n),
        preselect_limit=max(args.preselect_limit, args.top_n),
        history_workers=args.history_workers,
        enrichment_limit=max(args.enrichment_limit, args.top_n),
        evidence_workers=args.evidence_workers,
        evidence_budget_seconds=args.evidence_budget_seconds,
        min_amount_yuan=args.min_amount_yuan,
    )
    diagnostics = MarketScreenerDiagnostics(
        PROJECT_ROOT / "logs" / "market_screener_timing.jsonl",
    )
    result = MarketScreener(
        config=config,
        diagnostics=diagnostics,
    ).run()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    stamp = now.strftime("%Y%m%d_%H%M")
    report_dir = Path(args.report_dir)
    data_dir = Path(args.data_dir)
    report_path = report_dir / f"market_screening_{stamp}.md"
    json_path = data_dir / f"market_screening_{stamp}.json"
    codes_path = data_dir / "screened_codes.txt"
    snapshot_path = data_dir / "market_snapshot.json"
    save_result(
        result,
        report_path=report_path,
        json_path=json_path,
        codes_path=codes_path,
        snapshot_path=snapshot_path,
    )

    print(f"全市场记录: {result.universe_count}")
    print(f"基础过滤后: {result.spot_filtered_count}")
    print(
        "历史行情成功/失败/覆盖率: "
        f"{result.history_success_count}/{result.history_failure_count}/"
        f"{result.history_success_rate if result.history_success_rate is not None else '无法确认'}%"
    )
    print(f"历史行情数据质量: {result.history_data_quality.get('status', 'unknown')}")
    print(f"最终候选: {len(result.candidates)}")
    print(f"证据增强成功/失败: {result.evidence_success_count}/{result.evidence_failure_count}")
    print(f"市场环境评分: {result.market_environment.get('score', '无法确认')}")
    print(f"深度分析代码: {','.join(result.analysis_codes) or '无'}")
    print(f"初筛报告: {report_path}")
    print(f"统一行情快照: {snapshot_path} ({result.market_data_at})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
