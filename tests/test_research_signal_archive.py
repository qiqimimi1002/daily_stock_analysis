from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from research.archive import (
    ArchiveConflictError,
    SignalValidationError,
    archive_signals,
    build_signal_id,
)


CN_TZ = ZoneInfo("Asia/Shanghai")


def _candidate(code: str = "600100") -> dict:
    return {
        "code": code,
        "name": f"测试样本{code}",
        "score": 72.5,
        "raw_score": 58.0,
        "available_max_score": 80.0,
        "score_coverage_pct": 80.0,
        "confidence_label": "中",
        "score_breakdown": {
            "fundamental": {"score": 22.0, "max_score": 30.0},
            "technical": {"score": 16.0, "max_score": 20.0},
        },
        "latest_price": 12.34,
        "daily_pct": 1.2,
        "five_day_pct": 3.4,
        "amount_yi": 4.5,
        "avg_amount_20d_yi": 3.8,
        "turnover_pct": 2.1,
        "ma5": 12.1,
        "ma10": 11.9,
        "ma20": 11.5,
        "trend_label": "震荡偏强",
        "watch_zone": "11.50-12.10",
        "trigger_conditions": ["测试触发条件"],
        "abandon_conditions": ["测试放弃条件"],
        "risk_gate": "pass",
        "risks": ["测试风险"],
        "evidence_gaps": ["测试证据缺口"],
    }


def _source(
    *,
    day: str = "2026-08-03",
    batch_id: str = "TEST-AM-01",
    market_data_at: str | None = None,
) -> dict:
    quote_time = market_data_at or f"{day}T10:00:00+08:00"
    return {
        "test_data": True,
        "signal_batch_id": batch_id,
        "signal_date": day,
        "generated_at": f"{day}T10:00:30+08:00",
        "market_data_at": quote_time,
        "data_source": "synthetic-test-source",
        "model_version": "V2.1-TEST",
        "candidates": [_candidate("600100"), _candidate("000100")],
    }


def _fake_parquet(path: Path, records) -> None:
    payload = json.dumps(
        list(records), ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")
    path.write_bytes(b"PAR1-TEST\n" + payload)


class TestSignalArchive(unittest.TestCase):
    def setUp(self) -> None:
        self.archive_time = datetime(2026, 8, 3, 16, 0, tzinfo=CN_TZ)

    def _archive(self, source: dict, root: Path, **kwargs):
        return archive_signals(
            source,
            output_root=root,
            source_artifact="market_screening_TEST.json",
            archived_at=kwargs.pop("archived_at", self.archive_time),
            parquet_writer=kwargs.pop("parquet_writer", _fake_parquet),
            **kwargs,
        )

    def test_normal_archive_writes_json_parquet_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._archive(_source(), Path(directory))

            self.assertEqual(result.status, "created")
            self.assertEqual(result.signal_count, 2)
            self.assertRegex(
                str(result.archive_dir).replace("\\", "/"),
                r"/2026/08/03/batch-[0-9a-f]{16}$",
            )
            for name in ("signals.json", "signals.parquet", "manifest.json"):
                self.assertTrue((result.archive_dir / name).is_file(), name)

            payload = json.loads(
                (result.archive_dir / "signals.json").read_text(encoding="utf-8")
            )
            signal = payload["signals"][0]
            required_fields = {
                "signal_id",
                "signal_date",
                "signal_generated_at",
                "market_data_at",
                "stock_code",
                "stock_name",
                "reference_price",
                "reference_price_type",
                "total_score",
                "raw_score",
                "available_max_score",
                "score_coverage_pct",
                "confidence_label",
                "score_breakdown",
                "latest_price",
                "daily_pct",
                "five_day_pct",
                "amount_yi",
                "avg_amount_20d_yi",
                "turnover_pct",
                "ma5",
                "ma10",
                "ma20",
                "trend_label",
                "watch_zone",
                "trigger_conditions",
                "abandon_conditions",
                "risk_gate",
                "risks",
                "evidence_gaps",
                "data_source",
                "model_version",
                "source_artifact",
                "archived_at",
            }
            self.assertEqual(set(signal), required_fields)
            self.assertEqual(signal["reference_price_type"], "intraday_latest")
            self.assertEqual(signal["signal_generated_at"], "2026-08-03T10:00:30+08:00")
            self.assertEqual(signal["market_data_at"], "2026-08-03T10:00:00+08:00")
            self.assertEqual(signal["archived_at"], "2026-08-03T16:00:00+08:00")
            self.assertEqual(len(payload["raw_signals"]), 2)
            self.assertEqual(payload["raw_source"]["signal_batch_id"], "TEST-AM-01")

            manifest = json.loads(
                (result.archive_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["signal_count"], 2)
            self.assertEqual(manifest["model_version"], "V2.1-TEST")
            self.assertRegex(manifest["raw_source_hash"], r"^[0-9a-f]{64}$")
            for name, expected_hash in manifest["files"].items():
                actual = hashlib.sha256((result.archive_dir / name).read_bytes()).hexdigest()
                self.assertEqual(actual, expected_hash)

    def test_repeated_archive_is_idempotent_and_keeps_original_archive_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._archive(_source(), root)
            original_json = (first.archive_dir / "signals.json").read_bytes()
            second = self._archive(
                _source(),
                root,
                archived_at=datetime(2026, 8, 4, 9, 0, tzinfo=CN_TZ),
            )

            self.assertEqual(second.status, "exists")
            self.assertEqual(second.signal_ids, first.signal_ids)
            self.assertEqual(
                (first.archive_dir / "signals.json").read_bytes(), original_json
            )

    def test_same_stock_on_different_days_has_different_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._archive(_source(day="2026-08-03"), root)
            second = self._archive(
                _source(day="2026-08-04"),
                root,
                archived_at=datetime(2026, 8, 4, 16, 0, tzinfo=CN_TZ),
            )

            self.assertNotEqual(first.archive_dir, second.archive_dir)
            self.assertNotEqual(first.signal_ids[0], second.signal_ids[0])

    def test_same_day_different_batches_have_different_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._archive(_source(batch_id="TEST-AM-01"), root)
            second = self._archive(_source(batch_id="TEST-AM-02"), root)

            self.assertNotEqual(first.archive_dir, second.archive_dir)
            self.assertNotEqual(first.signal_ids[0], second.signal_ids[0])

    def test_conflict_refuses_to_overwrite_original_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._archive(_source(), root)
            original = (first.archive_dir / "signals.json").read_bytes()
            changed = _source()
            changed["candidates"][0]["score"] = 99.0

            with self.assertRaisesRegex(ArchiveConflictError, "original preserved"):
                self._archive(changed, root)

            self.assertEqual((first.archive_dir / "signals.json").read_bytes(), original)

    def test_corrupted_existing_file_is_not_reported_as_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._archive(_source(), root)
            (first.archive_dir / "signals.json").write_text(
                "corrupted", encoding="utf-8"
            )

            with self.assertRaisesRegex(ArchiveConflictError, "hash mismatch"):
                self._archive(_source(), root)

    def test_stable_signal_id_contract(self) -> None:
        values = {
            "signal_date": "2026-08-03",
            "stock_code": "600100",
            "model_version": "V2.1",
            "batch_id": "AM-01",
        }
        self.assertEqual(build_signal_id(**values), build_signal_id(**values))
        self.assertNotEqual(
            build_signal_id(**values),
            build_signal_id(**{**values, "batch_id": "AM-02"}),
        )

    def test_missing_critical_fields_are_rejected(self) -> None:
        cases = {
            "stock_code": ("candidate", "code"),
            "signal_generated_at": ("root", "generated_at"),
            "market_data_at": ("root", "market_data_at"),
            "model_version": ("root", "model_version"),
            "reference_price": ("candidate", "latest_price"),
        }
        for expected, (level, field) in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                source = _source()
                target = source if level == "root" else source["candidates"][0]
                target.pop(field)
                with self.assertRaisesRegex(SignalValidationError, expected):
                    self._archive(source, Path(directory))

    def test_stock_code_must_be_six_digits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _source()
            source["candidates"][0]["code"] = "SH600100"
            with self.assertRaisesRegex(SignalValidationError, "six digits"):
                self._archive(source, Path(directory))

    def test_non_finite_optional_numbers_are_cleaned_to_null(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _source()
            source["candidates"][0]["raw_score"] = float("nan")
            source["candidates"][0]["ma10"] = float("inf")
            source["candidates"][0]["score_breakdown"]["test"] = float("-inf")
            result = self._archive(source, Path(directory))
            raw = (result.archive_dir / "signals.json").read_text(encoding="utf-8")
            payload = json.loads(raw)

            self.assertNotIn("NaN", raw)
            self.assertNotIn("Infinity", raw)
            self.assertIsNone(payload["signals"][0]["raw_score"])
            self.assertIsNone(payload["signals"][0]["ma10"])
            self.assertIsNone(payload["signals"][0]["score_breakdown"]["test"])

    def test_non_finite_or_non_positive_reference_price_is_rejected(self) -> None:
        for invalid in (float("nan"), float("inf"), 0, -1):
            with self.subTest(value=invalid), tempfile.TemporaryDirectory() as directory:
                source = _source()
                source["candidates"][0]["latest_price"] = invalid
                with self.assertRaises(SignalValidationError):
                    self._archive(source, Path(directory))

    def test_times_are_converted_to_asia_shanghai(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _source()
            source["generated_at"] = "2026-08-03T02:00:30+00:00"
            source["market_data_at"] = "2026-08-03T02:00:00Z"
            result = self._archive(source, Path(directory))
            signal = json.loads(
                (result.archive_dir / "signals.json").read_text(encoding="utf-8")
            )["signals"][0]

            self.assertEqual(signal["signal_generated_at"], "2026-08-03T10:00:30+08:00")
            self.assertEqual(signal["market_data_at"], "2026-08-03T10:00:00+08:00")

    def test_legacy_v2_1_artifact_accepts_explicit_market_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _source()
            source.pop("market_data_at")
            result = self._archive(
                source,
                Path(directory),
                market_data_at="2026-08-03T10:00:00+08:00",
            )
            signal = json.loads(
                (result.archive_dir / "signals.json").read_text(encoding="utf-8")
            )["signals"][0]
            self.assertEqual(signal["market_data_at"], "2026-08-03T10:00:00+08:00")

    def test_naive_times_are_rejected(self) -> None:
        for field in ("generated_at", "market_data_at"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                source = _source()
                source[field] = "2026-08-03T10:00:00"
                with self.assertRaisesRegex(SignalValidationError, "timezone"):
                    self._archive(source, Path(directory))

    def test_market_time_cannot_be_after_signal_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _source(market_data_at="2026-08-03T10:01:00+08:00")
            with self.assertRaisesRegex(SignalValidationError, "cannot be later"):
                self._archive(source, Path(directory))

    def test_intraday_signal_cannot_claim_current_day_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _source()
            source["candidates"][0]["reference_price_type"] = "close"
            with self.assertRaisesRegex(SignalValidationError, "not-yet-formed close"):
                self._archive(source, Path(directory))

    def test_input_is_not_mutated_or_recalculated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = _source()
            before = deepcopy(source)
            result = self._archive(source, Path(directory))
            payload = json.loads(
                (result.archive_dir / "signals.json").read_text(encoding="utf-8")
            )

            self.assertEqual(source, before)
            self.assertEqual(
                [signal["stock_code"] for signal in payload["signals"]],
                [candidate["code"] for candidate in source["candidates"]],
            )
            for forbidden in (
                "future_1d_return",
                "future_5d_return",
                "max_drawdown",
                "win_rate",
            ):
                self.assertNotIn(forbidden, payload["signals"][0])

    def test_real_parquet_writer_when_research_dependency_is_available(self) -> None:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("pyarrow is installed only in the isolated research environment")

        with tempfile.TemporaryDirectory() as directory:
            result = archive_signals(
                _source(),
                output_root=Path(directory),
                source_artifact="market_screening_TEST.json",
                archived_at=self.archive_time,
            )
            table = pq.read_table(result.archive_dir / "signals.parquet")
            self.assertEqual(table.num_rows, 2)
            self.assertIn("signal_id", table.column_names)


if __name__ == "__main__":
    unittest.main()
