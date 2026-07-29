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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="沪深主板全市场初筛")
    parser.add_argument("--top-n", type=int, default=5, help="报告保留的候选数量")
    parser.add_argument("--analysis-limit", type=int, default=3, help="交给AI深度分析的数量")
    parser.add_argument("--preselect-limit", type=int, default=60, help="历史数据核验数量")
    parser.add_argument("--history-workers", type=int, default=4, help="历史行情并发数")
    parser.add_argument("--min-amount-yuan", type=float, default=100_000_000)
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--data-dir", default="data")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = ScreeningConfig(
        top_n=args.top_n,
        analysis_limit=min(args.analysis_limit, args.top_n),
        preselect_limit=max(args.preselect_limit, args.top_n),
        history_workers=args.history_workers,
        min_amount_yuan=args.min_amount_yuan,
    )
    result = MarketScreener(config=config).run()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    stamp = now.strftime("%Y%m%d_%H%M")
    report_dir = Path(args.report_dir)
    data_dir = Path(args.data_dir)
    report_path = report_dir / f"market_screening_{stamp}.md"
    json_path = data_dir / f"market_screening_{stamp}.json"
    codes_path = data_dir / "screened_codes.txt"
    save_result(
        result,
        report_path=report_path,
        json_path=json_path,
        codes_path=codes_path,
    )

    print(f"全市场记录: {result.universe_count}")
    print(f"基础过滤后: {result.spot_filtered_count}")
    print(f"最终候选: {len(result.candidates)}")
    print(f"深度分析代码: {','.join(result.analysis_codes) or '无'}")
    print(f"初筛报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

