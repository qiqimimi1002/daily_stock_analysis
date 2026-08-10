from __future__ import annotations

import unittest
from unittest.mock import patch
import urllib.request

from scripts.read_screening_status import (
    _NoCrossHostAuthRedirect,
    classify_screening_status,
    read_status,
    select_latest_run,
)


TRADE_DATE = "2026-08-04"
RUN = {
    "id": 30881432666,
    "run_number": 14,
    "status": "completed",
    "conclusion": "success",
    "created_at": "2026-08-04T01:40:39Z",
    "head_branch": "main",
}


def manifest(status: str = "success", deep: str = "completed", integrity: bool = True) -> dict:
    return {
        "trade_date": TRADE_DATE,
        "run_id": str(RUN["id"]),
        "run_number": str(RUN["run_number"]),
        "status": status,
        "screening_outcome": "success",
        "deep_analysis_status": deep,
        "candidate_count": 5,
        "preselected_count": 60,
        "history_success_count": 60,
        "history_failure_count": 0,
        "integrity": {"ok": integrity, "errors": [] if integrity else ["deep_analysis_incomplete"]},
    }


class ScreeningStatusClassifierTest(unittest.TestCase):
    def test_not_started(self) -> None:
        self.assertEqual(classify_screening_status(trade_date=TRADE_DATE, run=None)["status"], "not_started")

    def test_queued(self) -> None:
        run = {**RUN, "status": "queued", "conclusion": None}
        self.assertEqual(classify_screening_status(trade_date=TRADE_DATE, run=run)["status"], "queued")

    def test_in_progress(self) -> None:
        run = {**RUN, "status": "in_progress", "conclusion": None}
        self.assertEqual(classify_screening_status(trade_date=TRADE_DATE, run=run)["status"], "in_progress")

    def test_failure_without_any_result(self) -> None:
        run = {**RUN, "conclusion": "failure"}
        self.assertEqual(classify_screening_status(trade_date=TRADE_DATE, run=run)["status"], "failure")

    def test_completed_run_without_readable_results(self) -> None:
        self.assertEqual(classify_screening_status(trade_date=TRADE_DATE, run=RUN)["status"], "artifact_read_failure")

    def test_screening_completed(self) -> None:
        result = classify_screening_status(
            trade_date=TRADE_DATE,
            run=RUN,
            fixed_manifest=manifest("screening_completed", "not_requested"),
            fixed_entry_reachable=True,
        )
        self.assertEqual(result["status"], "screening_completed")

    def test_partial_success(self) -> None:
        partial = manifest("partial_success", "incomplete", False)
        partial["reason_codes"] = ["gemini_503", "gemini_429"]
        failed_workflow_run = {**RUN, "conclusion": "failure"}
        result = classify_screening_status(
            trade_date=TRADE_DATE,
            run=failed_workflow_run,
            fixed_manifest=partial,
            fixed_entry_reachable=True,
        )
        self.assertEqual(result["status"], "partial_success")
        self.assertIn("gemini_429", result["reason_codes"])

    def test_success(self) -> None:
        result = classify_screening_status(
            trade_date=TRADE_DATE, run=RUN, fixed_manifest=manifest(), fixed_entry_reachable=True
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["candidate_count"], 5)
        self.assertEqual(result["data_quality_status"], "ok")

    def test_all_history_fetches_failed_is_reported_as_degraded_data_quality(self) -> None:
        degraded = manifest(deep="not_required_no_candidates")
        degraded.update({
            "candidate_count": 0,
            "history_success_count": 0,
            "history_failure_count": 60,
        })
        result = classify_screening_status(
            trade_date=TRADE_DATE,
            run=RUN,
            fixed_manifest=degraded,
            fixed_entry_reachable=True,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data_quality_status"], "degraded")
        self.assertIn("history_data_all_failed", result["reason_codes"])

    def test_declared_insufficient_history_coverage_is_preserved(self) -> None:
        insufficient = manifest()
        insufficient.update(
            {
                "history_success_count": 19,
                "history_failure_count": 41,
                "history_success_rate": 31.67,
                "history_data_quality": {"status": "insufficient"},
            }
        )
        result = classify_screening_status(
            trade_date=TRADE_DATE,
            run=RUN,
            fixed_manifest=insufficient,
            fixed_entry_reachable=True,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data_quality_status"], "insufficient")
        self.assertEqual(result["history_success_rate"], 31.67)
        self.assertIn("history_coverage_insufficient", result["reason_codes"])

    def test_stale_latest_is_not_used(self) -> None:
        stale = manifest()
        stale["trade_date"] = "2026-08-03"
        result = classify_screening_status(
            trade_date=TRADE_DATE, run=RUN, fixed_manifest=stale, fixed_entry_reachable=True
        )
        self.assertEqual(result["status"], "artifact_read_failure")
        self.assertIn("fixed_entry_stale", result["reason_codes"])

    def test_manifest_run_id_mismatch_is_not_used(self) -> None:
        wrong = manifest()
        wrong["run_id"] = "1"
        result = classify_screening_status(
            trade_date=TRADE_DATE, run=RUN, fixed_manifest=wrong, fixed_entry_reachable=True
        )
        self.assertEqual(result["status"], "artifact_read_failure")
        self.assertIn("manifest_run_id_mismatch", result["reason_codes"])

    def test_artifact_fallback_succeeds_when_fixed_entry_is_stale(self) -> None:
        stale = manifest()
        stale["trade_date"] = "2026-08-03"
        result = classify_screening_status(
            trade_date=TRADE_DATE,
            run=RUN,
            fixed_manifest=stale,
            artifact_manifest=manifest(),
            fixed_entry_reachable=True,
            artifact_reachable=True,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["manifest_source"], "artifact")
        self.assertTrue(result["artifact_available"])

    def test_selects_only_run_created_on_shanghai_trade_date(self) -> None:
        runs = [
            {**RUN, "id": 1, "created_at": "2026-08-03T15:59:59Z"},
            {**RUN, "id": 2, "created_at": "2026-08-04T01:40:00Z"},
        ]
        self.assertEqual(select_latest_run(runs, TRADE_DATE)["id"], 2)

    @patch("scripts.read_screening_status.GitHubReader")
    def test_actions_lookup_failure_is_not_misreported_as_not_started(self, reader_type) -> None:
        reader_type.return_value.json.side_effect = OSError("network unavailable")

        result = read_status("owner/repo", "workflow.yml", TRADE_DATE)

        self.assertEqual(result["status"], "artifact_read_failure")
        self.assertEqual(result["reason_codes"], ["actions_run_lookup_failed"])

    @patch("scripts.read_screening_status.GitHubReader")
    def test_actions_state_is_checked_before_result_entries(self, reader_type) -> None:
        reader = reader_type.return_value
        reader.json.return_value = {
            "workflow_runs": [{**RUN, "status": "in_progress", "conclusion": None}]
        }

        result = read_status("owner/repo", "workflow.yml", TRADE_DATE)

        self.assertEqual(result["status"], "in_progress")
        reader.read_fixed_manifest.assert_not_called()
        reader.read_artifact_manifest.assert_not_called()

    @patch("scripts.read_screening_status.GitHubReader")
    def test_completed_idempotent_skip_resolves_to_original_result_run(self, reader_type) -> None:
        original = {**RUN, "id": 30, "run_number": 30}
        skipped = {
            **RUN,
            "id": 31,
            "run_number": 31,
            "created_at": "2026-08-04T02:10:00Z",
        }
        original_manifest = manifest()
        original_manifest.update({"run_id": "30", "run_number": "30"})
        reader = reader_type.return_value
        reader.json.return_value = {"workflow_runs": [original, skipped]}
        reader.read_artifact_guard.return_value = (
            {
                "idempotency_skipped": True,
                "existing_run_id": "30",
                "existing_run_number": "30",
            },
            True,
        )
        reader.read_fixed_manifest.return_value = original_manifest
        reader.read_artifact_manifest.return_value = (None, False)

        result = read_status("owner/repo", "workflow.yml", TRADE_DATE)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["run_id"], 30)
        self.assertIn("idempotent_run_skipped", result["reason_codes"])
        reader.read_artifact_manifest.assert_called_once_with(30)

    def test_artifact_redirect_does_not_forward_github_token_to_blob_host(self) -> None:
        request = urllib.request.Request(
            "https://api.github.com/repos/owner/repo/actions/artifacts/1/zip",
            headers={"Authorization": "Bearer secret"},
        )
        redirected = _NoCrossHostAuthRedirect().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://example.blob.core.windows.net/artifact.zip?sig=test",
        )
        self.assertIsNotNone(redirected)
        self.assertNotIn("Authorization", redirected.headers)


if __name__ == "__main__":
    unittest.main()
