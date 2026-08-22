"""Public-repository safety boundaries for the Draft ``rt_k`` adapter."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_production_screener_does_not_receive_tushare_secret():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/01-market-screening.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["screen"]["steps"]
    screening_step = next(
        step for step in steps if step.get("name") == "执行沪深主板全市场初筛"
    )

    assert "TUSHARE_TOKEN" not in screening_step.get("env", {})


def test_public_repository_has_no_live_rt_k_acceptance_workflow():
    assert not (
        ROOT / ".github/workflows/tushare-rt-k-acceptance.yml"
    ).exists()


def test_market_screener_does_not_import_draft_rt_k_provider():
    source = (ROOT / "src/services/market_screener.py").read_text(
        encoding="utf-8"
    )

    assert "tushare_rt_k_provider" not in source
