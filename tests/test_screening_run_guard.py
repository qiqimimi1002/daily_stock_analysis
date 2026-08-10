from __future__ import annotations

from datetime import datetime
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from scripts.screening_run_guard import (
    decide_execution,
    mark_screening_started,
    trigger_metadata,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TRADE_DATE = "2026-08-10"
WORKFLOW_NAME = "全市场初筛"
BRANCH = "main"


def run(
    run_id: int,
    run_number: int,
    status: str,
    *,
    conclusion: str | None = None,
    created_at: str = "2026-08-10T01:40:30Z",
) -> dict:
    return {
        "id": run_id,
        "run_number": run_number,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
        "head_branch": BRANCH,
    }


def manifest(
    source_run: dict,
    *,
    status: str = "success",
    errors: list[str] | None = None,
) -> dict:
    return {
        "trade_date": TRADE_DATE,
        "workflow_name": WORKFLOW_NAME,
        "branch": BRANCH,
        "run_id": str(source_run["id"]),
        "run_number": str(source_run["run_number"]),
        "status": status,
        "screening_outcome": "success",
        "screening_json": "data/market_screening_20260810_1000.json",
        "integrity": {"ok": not errors, "errors": errors or []},
    }


class ScreeningRunGuardTest(unittest.TestCase):
    def _decide(
        self,
        *,
        current: dict | None = None,
        runs: list[dict] | None = None,
        fixed: dict | None = None,
        event_name: str = "schedule",
        event_schedule: str = "40 1 * * 1-5",
        now: str = "2026-08-10T09:41:00+08:00",
    ) -> dict:
        current_run = current or run(30, 30, "in_progress")
        return decide_execution(
            now=datetime.fromisoformat(now),
            event_name=event_name,
            event_schedule=event_schedule,
            workflow_name=WORKFLOW_NAME,
            branch=BRANCH,
            current_run=current_run,
            workflow_runs=runs if runs is not None else [current_run],
            fixed_manifest=fixed,
        )

    def test_primary_0940_runs_when_no_prior_run_or_result(self) -> None:
        result = self._decide()
        self.assertTrue(result["should_run"])
        self.assertEqual(result["trigger_source"], "schedule_primary")
        self.assertEqual(result["scheduled_slot"], "09:40")

    def test_0955_fallback_runs_when_primary_never_started(self) -> None:
        current = run(31, 31, "in_progress", created_at="2026-08-10T01:55:15Z")
        result = self._decide(
            current=current,
            runs=[current],
            event_schedule="55 1 * * 1-5",
            now="2026-08-10T09:56:00+08:00",
        )
        self.assertTrue(result["should_run"])
        self.assertEqual(result["trigger_source"], "schedule_fallback")

    def test_later_run_skips_while_earlier_run_is_active(self) -> None:
        earlier = run(30, 30, "in_progress")
        current = run(31, 31, "in_progress", created_at="2026-08-10T01:55:00Z")
        result = self._decide(current=current, runs=[current, earlier], event_schedule="55 1 * * 1-5")
        self.assertFalse(result["should_run"])
        self.assertEqual(result["skip_reason"], "earlier_run_active")
        self.assertEqual(result["existing_run_id"], "30")

    def test_later_run_skips_after_earlier_successful_run(self) -> None:
        earlier = run(30, 30, "completed", conclusion="success")
        current = run(31, 31, "in_progress", created_at="2026-08-10T01:55:00Z")
        result = self._decide(current=current, runs=[current, earlier], event_schedule="55 1 * * 1-5")
        self.assertFalse(result["should_run"])
        self.assertEqual(result["skip_reason"], "earlier_successful_run")

    def test_partial_success_with_valid_screening_prevents_full_rerun(self) -> None:
        earlier = run(30, 30, "completed", conclusion="failure")
        current = run(31, 31, "in_progress", created_at="2026-08-10T02:10:00Z")
        fixed = manifest(earlier, status="partial_success", errors=["deep_analysis_incomplete"])
        result = self._decide(
            current=current,
            runs=[current, earlier],
            fixed=fixed,
            event_schedule="10 2 * * 1-5",
            now="2026-08-10T10:11:00+08:00",
        )
        self.assertFalse(result["should_run"])
        self.assertEqual(result["skip_reason"], "existing_valid_screening_result")

    def test_legacy_manifest_without_branch_prevents_full_rerun(self) -> None:
        earlier = run(30, 30, "completed", conclusion="failure")
        current = run(31, 31, "in_progress", created_at="2026-08-10T02:10:00Z")
        fixed = manifest(earlier, status="partial_success", errors=["deep_analysis_incomplete"])
        fixed.pop("branch")
        result = self._decide(current=current, runs=[current, earlier], fixed=fixed)
        self.assertFalse(result["should_run"])
        self.assertEqual(result["skip_reason"], "existing_valid_screening_result")

    def test_manifest_for_other_branch_does_not_block_run(self) -> None:
        earlier = run(30, 30, "completed", conclusion="failure")
        fixed = manifest(earlier, status="partial_success", errors=["deep_analysis_incomplete"])
        fixed["branch"] = "other-branch"
        result = self._decide(runs=[run(31, 31, "in_progress"), earlier], fixed=fixed)
        self.assertTrue(result["should_run"])

    def test_partial_success_with_invalid_screening_does_not_block_retry(self) -> None:
        earlier = run(30, 30, "completed", conclusion="failure")
        current = run(31, 31, "in_progress", created_at="2026-08-10T02:10:00Z")
        fixed = manifest(earlier, status="partial_success", errors=["screening_json_missing"])
        result = self._decide(current=current, runs=[current, earlier], fixed=fixed)
        self.assertTrue(result["should_run"])

    def test_delayed_primary_skips_after_fallback_published_result(self) -> None:
        fallback = run(31, 31, "completed", conclusion="success", created_at="2026-08-10T01:55:00Z")
        delayed_primary = run(32, 32, "in_progress", created_at="2026-08-10T02:30:00Z")
        result = self._decide(
            current=delayed_primary,
            runs=[delayed_primary, fallback],
            fixed=manifest(fallback),
            now="2026-08-10T10:31:00+08:00",
        )
        self.assertFalse(result["should_run"])
        self.assertEqual(result["existing_run_number"], "31")
        self.assertEqual(result["run_creation_delay_minutes"], 50.0)

    def test_previous_trade_day_latest_does_not_block_today(self) -> None:
        yesterday = run(29, 29, "completed", conclusion="success", created_at="2026-08-07T01:40:00Z")
        stale = manifest(yesterday)
        stale["trade_date"] = "2026-08-07"
        result = self._decide(runs=[run(30, 30, "in_progress"), yesterday], fixed=stale)
        self.assertTrue(result["should_run"])

    def test_manual_dispatch_cannot_duplicate_same_day_production(self) -> None:
        earlier = run(30, 30, "completed", conclusion="success")
        current = run(31, 31, "in_progress", created_at="2026-08-10T02:20:00Z")
        result = self._decide(
            current=current,
            runs=[current, earlier],
            fixed=manifest(earlier),
            event_name="workflow_dispatch",
            event_schedule="",
            now="2026-08-10T10:21:00+08:00",
        )
        self.assertFalse(result["should_run"])
        self.assertEqual(result["trigger_source"], "workflow_dispatch_manual")
        self.assertIsNone(result["scheduled_slot"])

    def test_manifest_identity_mismatch_does_not_block(self) -> None:
        earlier = run(30, 30, "completed", conclusion="failure")
        current = run(31, 31, "in_progress")
        fixed = manifest(earlier, status="partial_success", errors=["deep_analysis_incomplete"])
        fixed["run_id"] = "999"
        self.assertTrue(self._decide(current=current, runs=[current, earlier], fixed=fixed)["should_run"])

    def test_mark_started_records_exact_time_and_delay(self) -> None:
        context = self._decide()
        updated = mark_screening_started(context, datetime.fromisoformat("2026-08-10T09:44:30+08:00"))
        self.assertEqual(updated["screening_started_at"], "2026-08-10T09:44:30+08:00")
        self.assertEqual(updated["screening_start_delay_minutes"], 4.5)

    def test_second_trigger_simulation_does_not_call_production_steps(self) -> None:
        earlier = run(30, 30, "completed", conclusion="success")
        current = run(31, 31, "in_progress", created_at="2026-08-10T01:55:00Z")
        decision = self._decide(current=current, runs=[current, earlier], fixed=manifest(earlier))
        calls = {"market": 0, "deep": 0}
        if decision["should_run"]:
            calls["market"] += 1
            calls["deep"] += 1
        self.assertEqual(calls, {"market": 0, "deep": 0})

    def test_workflow_gates_market_and_deep_steps_after_guard(self) -> None:
        workflow = (Path(__file__).parents[1] / ".github/workflows/01-market-screening.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "40 1 * * 1-5"', workflow)
        self.assertIn('cron: "55 1 * * 1-5"', workflow)
        self.assertIn('cron: "10 2 * * 1-5"', workflow)
        self.assertGreaterEqual(workflow.count("steps.execution_guard.outputs.should_run == 'true'"), 7)

    def test_trigger_metadata_rejects_unknown_schedule(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown scheduled slot"):
            trigger_metadata("schedule", "0 0 * * *")


if __name__ == "__main__":
    unittest.main()
