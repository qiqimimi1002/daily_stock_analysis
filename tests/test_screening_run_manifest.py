from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts.build_screening_run_manifest import build_manifest, write_summary


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class ScreeningRunManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.reports = self.root / "reports"
        self.logs = self.root / "logs"
        self.data.mkdir()
        self.reports.mkdir()
        self.logs.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_screening(
        self,
        *,
        candidate_codes: list[str],
        analysis_codes: list[str],
        coverages: list[float] | None = None,
    ) -> None:
        coverage_values = coverages or [75.0] * len(candidate_codes)
        candidate_rows = []
        for index, code in enumerate(candidate_codes):
            price = 10.0 + index
            prev_close = 10.0
            candidate_rows.append(
                {
                    "stock_code": code,
                    "stock_name": f"测试{index}",
                    "score_coverage_pct": coverage_values[index],
                    "latest_price": price,
                    "prev_close": prev_close,
                    "daily_pct": round((price - prev_close) / prev_close * 100.0, 2),
                }
            )
        market_snapshot = {
            "schema_version": "1.0",
            "market_data_at": "2026-08-04T10:18:00+08:00",
            "data_source": "synthetic-test-source",
            "price_adjustment": "none_realtime_spot",
            "prev_close_adjustment": "provider_exchange_reference",
            "price_change_formula": "(price - prev_close) / prev_close * 100",
            "quotes": {
                code: {
                    "code": code,
                    "price": candidate_rows[candidate_codes.index(code)]["latest_price"],
                    "prev_close": candidate_rows[candidate_codes.index(code)]["prev_close"],
                    "change_pct": candidate_rows[candidate_codes.index(code)]["daily_pct"],
                }
                for code in analysis_codes
            },
        }
        payload = {
            "generated_at": "2026-08-04T10:18:30+08:00",
            "market_data_at": "2026-08-04T10:18:00+08:00",
            "data_source": "synthetic-test-source",
            "model_version": "V2.1-test",
            "universe_count": 5534,
            "spot_filtered_count": 60,
            "history_success_count": 55,
            "history_failure_count": 5,
            "evidence_success_count": 4,
            "evidence_failure_count": 1,
            "candidates": candidate_rows,
            "analysis_codes": analysis_codes,
            "market_snapshot": market_snapshot,
        }
        (self.data / "market_screening_20260804_1018.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        (self.data / "screened_codes.txt").write_text(",".join(analysis_codes), encoding="utf-8")
        (self.data / "market_snapshot.json").write_text(
            json.dumps(market_snapshot, ensure_ascii=False), encoding="utf-8"
        )
        (self.reports / "market_screening_20260804_1018.md").write_text("测试初筛报告", encoding="utf-8")

    def _build(
        self,
        *,
        screening_outcome: str = "success",
        deep_analysis_outcome: str = "success",
        deep_analysis_requested: bool = True,
        execution_context: dict | None = None,
    ) -> dict:
        return build_manifest(
            repository_root=self.root,
            data_dir=self.data,
            reports_dir=self.reports,
            workflow_name="全市场初筛",
            run_id="123456",
            run_number="14",
            artifact_name="market-screening-14",
            screening_outcome=screening_outcome,
            deep_analysis_outcome=deep_analysis_outcome,
            deep_analysis_requested=deep_analysis_requested,
            started_at=datetime(2026, 8, 4, 2, 10, tzinfo=timezone.utc),
            completed_at=datetime(2026, 8, 4, 2, 20, tzinfo=timezone.utc),
            logs_dir=self.logs,
            branch="main",
            execution_context=execution_context,
        )

    def test_success_manifest_has_counts_reports_hashes_and_shanghai_times(self) -> None:
        codes = ["600089", "600309", "002415"]
        self._write_screening(candidate_codes=codes, analysis_codes=codes, coverages=[80.0, 70.0, 60.0])
        (self.reports / "report_20260804.md").write_text("\n".join(codes), encoding="utf-8")

        manifest = self._build()

        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["candidate_count"], 3)
        self.assertEqual(manifest["candidate_codes"], codes)
        self.assertEqual(manifest["deep_analysis_status"], "completed")
        self.assertEqual(manifest["evidence_coverage_average"], 70.0)
        self.assertEqual(manifest["evidence_coverage_minimum"], 60.0)
        self.assertTrue(manifest["integrity"]["ok"])
        self.assertTrue(manifest["started_at"].endswith("+08:00"))
        self.assertEqual(manifest["started_at"], "2026-08-04T10:10:00+08:00")
        self.assertEqual(manifest["screening_generated_at"], "2026-08-04T10:18:30+08:00")
        self.assertEqual(manifest["market_data_at"], "2026-08-04T10:18:00+08:00")
        self.assertEqual(manifest["market_snapshot"], "data/market_snapshot.json")
        self.assertEqual(manifest["data_source"], "synthetic-test-source")
        self.assertEqual(manifest["model_version"], "V2.1-test")
        self.assertEqual(manifest["fixed_result_entry"]["branch"], "screening-results")
        self.assertIn("reports/report_20260804.md", manifest["result_file_sha256"])

    def test_market_snapshot_must_match_screening_candidate_quote(self) -> None:
        self._write_screening(candidate_codes=["600089"], analysis_codes=["600089"])
        (self.reports / "report_20260804.md").write_text("600089", encoding="utf-8")
        snapshot_path = self.data / "market_snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["quotes"]["600089"]["change_pct"] = -1.62
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

        manifest = self._build()

        self.assertFalse(manifest["integrity"]["ok"])
        self.assertIn("market_snapshot_screening_mismatch", manifest["integrity"]["errors"])
        self.assertIn("market_snapshot_quote_mismatch:600089", manifest["integrity"]["errors"])

    def test_zero_candidates_is_success_without_deep_analysis(self) -> None:
        self._write_screening(candidate_codes=[], analysis_codes=[])

        manifest = self._build(deep_analysis_outcome="skipped")

        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["deep_analysis_status"], "not_required_no_candidates")
        self.assertTrue(manifest["integrity"]["ok"])

    def test_manual_screening_without_deep_request_is_screening_completed(self) -> None:
        self._write_screening(candidate_codes=["600089"], analysis_codes=["600089"])

        manifest = self._build(deep_analysis_outcome="skipped", deep_analysis_requested=False)

        self.assertEqual(manifest["status"], "screening_completed")
        self.assertEqual(manifest["deep_analysis_status"], "not_requested")
        self.assertTrue(manifest["integrity"]["ok"])

    def test_missing_deep_report_is_partial_success_not_full_success(self) -> None:
        self._write_screening(candidate_codes=["600089"], analysis_codes=["600089"])

        manifest = self._build()

        self.assertEqual(manifest["status"], "partial_success")
        self.assertEqual(manifest["deep_analysis_status"], "incomplete")
        self.assertIn("deep_analysis_incomplete", manifest["integrity"]["errors"])

    def test_retry_events_are_safely_exposed_in_manifest(self) -> None:
        self._write_screening(candidate_codes=["600089"], analysis_codes=["600089"])
        events = [
            {
                "action": "retry",
                "attempt": 1,
                "max_attempts": 3,
                "error_type": "gemini_503",
                "stock_code": "600089",
                "key_index": 0,
                "key_switched": False,
                "api_key": "SECRET_MUST_NOT_APPEAR",
            },
            {
                "action": "exhausted",
                "attempt": 3,
                "max_attempts": 3,
                "error_type": "gemini_429",
                "stock_code": "600089",
                "key_index": 1,
                "key_switched": False,
            },
        ]
        (self.logs / "llm_retry_events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
        )

        manifest = self._build(deep_analysis_outcome="failure")

        self.assertEqual(manifest["status"], "partial_success")
        self.assertEqual(manifest["reason_codes"], [
            "deep_analysis_incomplete",
            "gemini_503",
            "gemini_429",
        ])
        self.assertEqual(manifest["deep_analysis_failures"][0]["stock_code"], "600089")
        self.assertIn("logs/llm_retry_events.jsonl", manifest["result_file_sha256"])
        self.assertNotIn("SECRET_MUST_NOT_APPEAR", json.dumps(manifest))

    def test_history_quality_diagnostics_are_preserved_without_failing_integrity(self) -> None:
        self._write_screening(candidate_codes=[], analysis_codes=[])
        path = self.data / "market_screening_20260804_1018.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(
            {
                "history_success_rate": 31.67,
                "history_failure_reasons": {
                    "counts": {"remote_disconnect": 41},
                    "by_code": [{"stock_code": "600030", "reason": "remote_disconnect"}],
                },
                "history_source_stats": {
                    "akshare_eastmoney": {"attempts": 101, "successes": 19, "failures": 41, "retries": 41}
                },
                "history_consistency": {
                    "status_counts": {"single_backend": 19},
                    "checked_count": 0,
                    "conflict_count": 0,
                },
                "history_data_quality": {
                    "status": "insufficient",
                    "confidence_label": "low",
                    "warning": "测试告警",
                },
            }
        )
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        manifest = self._build(deep_analysis_outcome="skipped")

        self.assertEqual(manifest["schema_version"], "1.3")
        self.assertEqual(manifest["history_success_rate"], 31.67)
        self.assertEqual(manifest["history_failure_reasons"]["counts"]["remote_disconnect"], 41)
        self.assertEqual(manifest["history_data_quality"]["status"], "insufficient")
        self.assertIn("history_coverage_insufficient", manifest["reason_codes"])
        self.assertTrue(manifest["integrity"]["ok"])

    def test_trigger_and_idempotency_metadata_are_preserved(self) -> None:
        self._write_screening(candidate_codes=[], analysis_codes=[])
        (self.logs / "screening_execution_guard.log").write_text(
            '{"should_run":true}\n', encoding="utf-8"
        )
        context = {
            "trigger_source": "schedule_fallback",
            "scheduled_slot": "09:55",
            "run_created_at": "2026-08-04T09:56:00+08:00",
            "screening_started_at": "2026-08-04T09:57:30+08:00",
            "run_creation_delay_minutes": 1.0,
            "screening_start_delay_minutes": 2.5,
            "idempotency_skipped": False,
            "skip_reason": None,
            "existing_run_id": None,
            "existing_run_number": None,
        }

        result = self._build(deep_analysis_outcome="skipped", execution_context=context)

        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["trigger_source"], "schedule_fallback")
        self.assertEqual(result["scheduled_slot"], "09:55")
        self.assertEqual(result["screening_started_at"], "2026-08-04T09:57:30+08:00")
        self.assertEqual(result["screening_start_delay_minutes"], 2.5)
        self.assertFalse(result["idempotency_skipped"])
        self.assertIn("logs/screening_execution_guard.log", result["result_file_sha256"])

    def test_screened_codes_mismatch_is_reported(self) -> None:
        self._write_screening(candidate_codes=["600089", "600309"], analysis_codes=["600089"])
        (self.data / "screened_codes.txt").write_text("600309", encoding="utf-8")
        (self.reports / "report_20260804.md").write_text("600089", encoding="utf-8")

        manifest = self._build()

        self.assertEqual(manifest["status"], "partial_success")
        self.assertIn("screened_codes_mismatch", manifest["integrity"]["errors"])

    def test_screening_failure_still_produces_a_failure_manifest(self) -> None:
        manifest = self._build(screening_outcome="failure", deep_analysis_outcome="skipped")

        self.assertEqual(manifest["status"], "failure")
        self.assertFalse(manifest["integrity"]["ok"])
        self.assertIn("screening_json_missing", manifest["integrity"]["errors"])
        self.assertEqual(manifest["artifact_name"], "market-screening-14")

    def test_missing_screening_report_is_partial_success(self) -> None:
        self._write_screening(candidate_codes=[], analysis_codes=[])
        (self.reports / "market_screening_20260804_1018.md").unlink()

        manifest = self._build(deep_analysis_outcome="skipped")

        self.assertEqual(manifest["status"], "partial_success")
        self.assertIn("screening_report_missing_or_empty", manifest["integrity"]["errors"])

    def test_generated_time_outside_run_window_is_reported(self) -> None:
        self._write_screening(candidate_codes=[], analysis_codes=[])
        path = self.data / "market_screening_20260804_1018.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["generated_at"] = "2026-08-04T10:25:00+08:00"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        manifest = self._build(deep_analysis_outcome="skipped")

        self.assertEqual(manifest["status"], "partial_success")
        self.assertIn("generated_at_outside_run_window", manifest["integrity"]["errors"])

    def test_summary_distinguishes_status_and_integrity(self) -> None:
        self._write_screening(candidate_codes=["600089"], analysis_codes=["600089"])
        manifest = self._build()
        summary = self.root / "summary.md"

        write_summary(summary, manifest)

        text = summary.read_text(encoding="utf-8")
        self.assertIn("`partial_success`", text)
        self.assertIn("`deep_analysis_incomplete`", text)


if __name__ == "__main__":
    unittest.main()
