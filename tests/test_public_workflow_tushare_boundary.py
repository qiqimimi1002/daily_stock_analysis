# -*- coding: utf-8 -*-
"""Static boundary checks for Tushare credentials in Public Actions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
PUBLIC_WORKFLOWS = (
    ROOT_DIR / ".github/workflows/00-daily-analysis.yml",
    ROOT_DIR / ".github/workflows/01-market-screening.yml",
)
FORBIDDEN_IDENTIFIERS = ("TUSHARE_TOKEN", "TUSHARE_HTTP_URL")
FREE_REALTIME_PRIORITY = "tencent,akshare_sina,efinance,akshare_em"


def _load_workflow(path: Path) -> dict:
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict), f"Expected {path.name} to contain a YAML mapping"
    return workflow


def _step_by_name(workflow: dict, job_name: str, step_name: str) -> dict:
    steps = workflow["jobs"][job_name]["steps"]
    step = next((item for item in steps if item.get("name") == step_name), None)
    assert step is not None, f"Expected {job_name} to contain step {step_name!r}"
    return step


@pytest.mark.parametrize("workflow_path", PUBLIC_WORKFLOWS, ids=lambda path: path.name)
def test_public_workflow_does_not_reference_tushare_credentials(workflow_path: Path) -> None:
    text = workflow_path.read_text(encoding="utf-8")

    for identifier in FORBIDDEN_IDENTIFIERS:
        assert identifier not in text

    _load_workflow(workflow_path)


def test_daily_analysis_keeps_existing_free_provider_fallback() -> None:
    workflow = _load_workflow(PUBLIC_WORKFLOWS[0])
    analyze_step = _step_by_name(workflow, "analyze", "执行股票分析")

    assert FREE_REALTIME_PRIORITY in analyze_step["env"]["REALTIME_SOURCE_PRIORITY"]
    assert "python main.py --market-review" in analyze_step["run"]
    assert "python main.py --no-market-review" in analyze_step["run"]


def test_screening_keeps_cloudflare_dispatch_and_free_provider_fallback() -> None:
    workflow = _load_workflow(PUBLIC_WORKFLOWS[1])
    dispatch_options = workflow["on"]["workflow_dispatch"]["inputs"]["trigger_source"]["options"]
    guard_step = _step_by_name(workflow, "screen", "检查当天是否需要执行初筛")
    deep_step = _step_by_name(workflow, "screen", "对候选运行 Daily Stock 深度分析")

    assert "external_scheduler_cloudflare" in dispatch_options
    assert "--dispatch-source" in guard_step["run"]
    assert FREE_REALTIME_PRIORITY in deep_step["env"]["REALTIME_SOURCE_PRIORITY"]
    assert "--no-market-review" in deep_step["run"]
