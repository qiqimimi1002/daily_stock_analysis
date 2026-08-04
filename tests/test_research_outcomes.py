from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.archive import SHANGHAI_TZ, archive_signals
from research.cli import main as cli_main
from research.outcomes import (
    DEFAULT_CALCULATION_VERSION,
    OutcomeConflictError,
    OutcomeValidationError,
    build_outcome_id,
    calculate_outcomes,
    load_price_artifact,
)


TRADE_DATES = [
    "2026-01-05",
    "2026-01-06",
    # 2026-01-07 is an artificial exchange holiday in this fixed fixture.
    "2026-01-08",
    "2026-01-09",
    "2026-01-12",
    "2026-01-13",
    "2026-01-14",
    "2026-01-15",
    "2026-01-16",
    "2026-01-19",
    "2026-01-20",
    "2026-01-21",
    "2026-01-22",
    "2026-01-23",
    "2026-01-26",
    "2026-01-27",
    "2026-01-28",
    "2026-01-29",
    "2026-01-30",
    "2026-02-02",
    "2026-02-03",
]


def _fake_parquet(path: Path, records) -> None:
    path.write_bytes(json.dumps(list(records), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))


def _candidate(code: str, name: str, price: float = 100.0) -> dict:
    return {
        "code": code,
        "name": name,
        "score": 80.0,
        "raw_score": 40.0,
        "available_max_score": 50.0,
        "score_coverage_pct": 50.0,
        "confidence_label": "测试",
        "score_breakdown": {},
        "latest_price": price,
        "daily_pct": 1.0,
        "five_day_pct": 2.0,
        "amount_yi": 3.0,
        "avg_amount_20d_yi": 3.0,
        "turnover_pct": 1.0,
        "ma5": 99.0,
        "ma10": 98.0,
        "ma20": 97.0,
        "trend_label": "测试趋势",
        "watch_zone": "仅测试",
        "trigger_conditions": [],
        "abandon_conditions": [],
        "risk_gate": "仅测试",
        "risks": [],
        "evidence_gaps": [],
    }


def _archive_batch(root: Path, signal_date: str, candidates: list[dict], batch: str) -> Path:
    source = {
        "test_data": True,
        "signal_date": signal_date,
        "generated_at": f"{signal_date}T10:00:00+08:00",
        "market_data_at": f"{signal_date}T09:59:00+08:00",
        "market_data_at_precision": "exact_snapshot",
        "data_source": "synthetic-test",
        "model_version": "V2.1-TEST",
        "candidates": candidates,
    }
    source_bytes = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result = archive_signals(
        source,
        output_root=root,
        source_file_sha256=hashlib.sha256(source_bytes).hexdigest(),
        batch_id=batch,
        source_artifact=f"synthetic/{batch}.json",
        archived_at=datetime(2026, 2, 4, 9, 0, tzinfo=SHANGHAI_TZ),
        parquet_writer=_fake_parquet,
    )
    return result.archive_dir


def _bar(
    trade_date: str,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    suspended: bool = False,
    limit_up: bool | None = None,
    limit_down: bool | None = None,
    limit_up_price: float | None = None,
) -> dict:
    if suspended:
        return {"trade_date": trade_date, "is_suspended": True, "volume": 0}
    return {
        "trade_date": trade_date,
        "open": close,
        "high": high if high is not None else close + 1,
        "low": low if low is not None else close - 1,
        "close": close,
        "volume": 1000,
        "is_suspended": False,
        "is_limit_up": limit_up,
        "is_limit_down": limit_down,
        "limit_up_price": limit_up_price,
    }


def _normal_bars() -> list[dict]:
    manual = [
        _bar("2026-01-05", 100, high=100, low=99, limit_up_price=101),
        _bar("2026-01-06", 104, high=105, low=98, limit_up=True),
        _bar("2026-01-08", 107, high=108, low=101),
        _bar("2026-01-09", 96, high=106, low=95, limit_down=True),
        _bar("2026-01-12", 100, high=102, low=94),
        _bar("2026-01-13", 110, high=112, low=99),
    ]
    for index, trade_date in enumerate(TRADE_DATES[6:], start=1):
        manual.append(_bar(trade_date, 110 + index, high=112 + index, low=108 + index))
    return manual


def _price_source(as_of: str = "2026-02-03T16:00:00+08:00") -> dict:
    cutoff = datetime.fromisoformat(as_of).date()
    normal = [bar for bar in _normal_bars() if date.fromisoformat(bar["trade_date"]) <= cutoff]
    suspended = deepcopy(normal)
    suspended[3] = _bar("2026-01-09", 0, suspended=True)
    suspended.append(_bar("2026-02-04", 130)) if cutoff >= date(2026, 2, 4) else None
    missing = [bar for bar in deepcopy(normal) if bar["trade_date"] != "2026-01-09"]
    corporate = deepcopy(normal)
    conflicted = deepcopy(normal)
    return {
        "test_data": True,
        "price_source": "synthetic-unadjusted-ohlc",
        "price_data_as_of": as_of,
        "calendar_source": "synthetic-sse-szse-calendar",
        "market_trade_dates": TRADE_DATES,
        "stocks": {
            "600100": {"prices": normal, "corporate_actions": [], "data_conflicts": []},
            "600200": {"prices": suspended, "corporate_actions": [], "data_conflicts": []},
            "600300": {
                "prices": corporate,
                "corporate_actions": [{"trade_date": "2026-01-08", "action_type": "cash_dividend"}],
                "data_conflicts": [],
            },
            "600400": {"prices": missing, "corporate_actions": [], "data_conflicts": []},
            "600500": {
                "prices": conflicted,
                "corporate_actions": [],
                "data_conflicts": [{"trade_date": "2026-01-08", "reason": "source close mismatch"}],
            },
        },
    }


def _price_bytes(source: dict) -> bytes:
    return (json.dumps(source, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


class TestResearchOutcomes(unittest.TestCase):
    def setUp(self) -> None:
        self.calculated_at = datetime(2026, 2, 4, 9, 0, tzinfo=SHANGHAI_TZ)

    def _signals(self, root: Path, candidates: list[dict] | None = None) -> Path:
        signals_root = root / "signals"
        _archive_batch(
            signals_root,
            "2026-01-05",
            candidates or [_candidate("600100", "正常案例")],
            "batch-a",
        )
        return signals_root

    def _run(self, root: Path, signals_root: Path, prices: dict, *, as_of: str | None = None, **kwargs):
        raw = _price_bytes(prices)
        return calculate_outcomes(
            signals_root,
            prices,
            price_file_sha256=hashlib.sha256(raw).hexdigest(),
            output_root=root / "outcomes",
            as_of=as_of or prices["price_data_as_of"],
            calculated_at=kwargs.pop("calculated_at", self.calculated_at),
            parquet_writer=kwargs.pop("parquet_writer", _fake_parquet),
            **kwargs,
        )

    @staticmethod
    def _records(result) -> list[dict]:
        return json.loads((result.archive_dir / "outcomes.json").read_text(encoding="utf-8"))["outcomes"]

    def test_trade_horizons_skip_weekend_and_fixed_holiday(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self._run(root, self._signals(root), _price_source())
            by_horizon = {row["horizon_days"]: row for row in self._records(result)}
            self.assertEqual(
                {horizon: row["horizon_trade_date"] for horizon, row in by_horizon.items()},
                {1: "2026-01-06", 3: "2026-01-09", 5: "2026-01-13", 10: "2026-01-20", 20: "2026-02-03"},
            )

    def test_outcome_archive_keeps_required_metadata_and_no_win_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prices = _price_source()
            result = self._run(root, self._signals(root), prices)
            payload = json.loads((result.archive_dir / "outcomes.json").read_text(encoding="utf-8"))
            manifest = json.loads((result.archive_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result.outcome_count, 5)
            self.assertEqual(manifest["outcome_count"], 5)
            self.assertEqual(manifest["price_source"], "synthetic-unadjusted-ohlc")
            self.assertEqual(manifest["price_data_as_of"], "2026-02-03T16:00:00+08:00")
            self.assertEqual(manifest["price_coverage_start"], "2026-01-05")
            self.assertEqual(manifest["price_coverage_end"], "2026-02-03")
            self.assertEqual(
                manifest["price_file_sha256"],
                hashlib.sha256(_price_bytes(prices)).hexdigest(),
            )
            for name, expected_hash in manifest["files"].items():
                self.assertEqual(hashlib.sha256((result.archive_dir / name).read_bytes()).hexdigest(), expected_hash)
            for record in payload["outcomes"]:
                for field in (
                    "horizon_days",
                    "horizon_trade_date",
                    "horizon_close",
                    "future_return_pct",
                    "max_upside_pct",
                    "max_adverse_excursion_pct",
                    "max_drawdown_pct",
                    "valid_market_days",
                    "missing_price_days",
                    "outcome_status",
                    "calculated_at",
                    "price_data_as_of",
                    "price_source",
                    "calculation_version",
                ):
                    self.assertIn(field, record)
                self.assertNotIn("win_rate", record)

    def test_manual_normal_case_return_upside_adverse_and_true_drawdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            records = self._records(self._run(root, self._signals(root), _price_source()))
            by_horizon = {row["horizon_days"]: row for row in records}
            one = by_horizon[1]
            self.assertEqual(one["future_return_pct"], 4.0)
            self.assertEqual(one["max_upside_pct"], 5.0)
            self.assertEqual(one["max_adverse_excursion_pct"], -2.0)
            self.assertEqual(one["max_drawdown_pct"], 0.0)
            three = by_horizon[3]
            self.assertEqual(three["future_return_pct"], -4.0)
            self.assertEqual(three["max_upside_pct"], 8.0)
            self.assertEqual(three["max_adverse_excursion_pct"], -5.0)
            self.assertAlmostEqual(three["max_drawdown_pct"], -10.280374, places=6)
            five = by_horizon[5]
            self.assertEqual(five["future_return_pct"], 10.0)
            self.assertEqual(five["max_upside_pct"], 12.0)
            self.assertEqual(five["max_adverse_excursion_pct"], -6.0)
            self.assertAlmostEqual(five["max_drawdown_pct"], -10.280374, places=6)

    def test_unmatured_horizons_remain_pending_with_fixed_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prices = _price_source("2026-01-09T16:00:00+08:00")
            result = self._run(
                root,
                self._signals(root),
                prices,
                calculated_at=datetime(2026, 1, 10, 9, 0, tzinfo=SHANGHAI_TZ),
            )
            by_horizon = {row["horizon_days"]: row for row in self._records(result)}
            self.assertEqual(by_horizon[3]["outcome_status"], "complete")
            for horizon in (5, 10, 20):
                self.assertEqual(by_horizon[horizon]["outcome_status"], "pending")
                self.assertIsNone(by_horizon[horizon]["future_return_pct"])
                self.assertIsNone(by_horizon[horizon]["max_drawdown_pct"])

    def test_calendar_not_yet_covered_keeps_long_horizons_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prices = _price_source("2026-01-13T16:00:00+08:00")
            prices["market_trade_dates"] = TRADE_DATES[:6]
            result = self._run(
                root,
                self._signals(root),
                prices,
                calculated_at=datetime(2026, 1, 14, 9, 0, tzinfo=SHANGHAI_TZ),
            )
            by_horizon = {row["horizon_days"]: row for row in self._records(result)}
            self.assertEqual(by_horizon[5]["outcome_status"], "complete")
            for horizon in (10, 20):
                self.assertEqual(by_horizon[horizon]["outcome_status"], "pending")
                self.assertIsNone(by_horizon[horizon]["horizon_trade_date"])

    def test_target_session_before_close_is_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prices = _price_source("2026-01-09T14:00:00+08:00")
            result = self._run(
                root,
                self._signals(root),
                prices,
                calculated_at=datetime(2026, 1, 9, 14, 30, tzinfo=SHANGHAI_TZ),
            )
            three = next(row for row in self._records(result) if row["horizon_days"] == 3)
            self.assertEqual(three["outcome_status"], "pending")
            self.assertIsNone(three["future_return_pct"])

    def test_stale_intraday_price_artifact_cannot_supply_a_later_completed_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prices = _price_source("2026-01-09T14:00:00+08:00")
            result = self._run(
                root,
                self._signals(root),
                prices,
                as_of="2026-01-09T16:00:00+08:00",
                calculated_at=datetime(2026, 1, 10, 9, 0, tzinfo=SHANGHAI_TZ),
            )
            three = next(row for row in self._records(result) if row["horizon_days"] == 3)
            self.assertEqual(three["outcome_status"], "missing_price")
            self.assertIsNone(three["horizon_close"])
            self.assertIsNone(three["future_return_pct"])
            self.assertEqual(three["missing_price_days"], 1)

    def test_suspended_target_does_not_roll_to_next_price(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root, [_candidate("600200", "停牌案例")])
            three = next(
                row for row in self._records(self._run(root, signals, _price_source())) if row["horizon_days"] == 3
            )
            self.assertEqual(three["horizon_trade_date"], "2026-01-09")
            self.assertEqual(three["outcome_status"], "suspended")
            self.assertIsNone(three["horizon_close"])
            self.assertIsNone(three["future_return_pct"])
            self.assertIn("target_suspended", three["execution_risks"])

    def test_missing_target_is_not_forward_filled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root, [_candidate("600400", "缺失案例")])
            three = next(
                row for row in self._records(self._run(root, signals, _price_source())) if row["horizon_days"] == 3
            )
            self.assertEqual(three["outcome_status"], "missing_price")
            self.assertIsNone(three["horizon_close"])
            self.assertIsNone(three["future_return_pct"])
            self.assertEqual(three["valid_market_days"], 2)
            self.assertEqual(three["missing_price_days"], 1)

    def test_corporate_action_marks_review_but_keeps_raw_observation_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root, [_candidate("600300", "公司行为案例")])
            three = next(
                row for row in self._records(self._run(root, signals, _price_source())) if row["horizon_days"] == 3
            )
            self.assertEqual(three["outcome_status"], "corporate_action_review")
            self.assertEqual(three["future_return_pct"], -4.0)
            self.assertEqual(
                three["corporate_actions"],
                [{"trade_date": "2026-01-08", "action_type": "cash_dividend"}],
            )

    def test_data_conflict_suppresses_price_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root, [_candidate("600500", "冲突案例")])
            three = next(
                row for row in self._records(self._run(root, signals, _price_source())) if row["horizon_days"] == 3
            )
            self.assertEqual(three["outcome_status"], "data_conflict")
            self.assertIsNone(three["future_return_pct"])
            self.assertIsNone(three["max_upside_pct"])

    def test_limit_and_execution_risk_flags_are_recorded_without_trade_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            one = next(
                row
                for row in self._records(self._run(root, self._signals(root), _price_source()))
                if row["horizon_days"] == 1
            )
            self.assertTrue(one["target_is_limit_up"])
            self.assertTrue(one["signal_near_limit_up"])
            self.assertIn("signal_price_near_limit_up", one["execution_risks"])
            self.assertIn("target_closed_at_limit_up", one["execution_risks"])
            self.assertNotIn("profit", one)
            self.assertNotIn("total_score", one)
            three = next(
                row
                for row in self._records(self._run(root / "down", self._signals(root / "down"), _price_source()))
                if row["horizon_days"] == 3
            )
            self.assertTrue(three["target_is_limit_down"])
            self.assertIn("target_closed_at_limit_down", three["execution_risks"])

    def test_consecutive_day_signals_are_calculated_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals_root = root / "signals"
            first = _archive_batch(signals_root, "2026-01-05", [_candidate("600100", "连续案例")], "batch-a")
            second = _archive_batch(signals_root, "2026-01-06", [_candidate("600100", "连续案例", 104)], "batch-b")
            result = self._run(root, signals_root, _price_source())
            records = self._records(result)
            ids = json.loads((first / "signals.json").read_text(encoding="utf-8"))["signals"][0]["signal_id"], json.loads(
                (second / "signals.json").read_text(encoding="utf-8")
            )["signals"][0]["signal_id"]
            self.assertEqual(len(records), 10)
            self.assertEqual({row["signal_id"] for row in records}, set(ids))

    def test_repeated_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root)
            prices = _price_source()
            first = self._run(root, signals, prices)
            original = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first.archive_dir.iterdir()}
            second = self._run(root, signals, prices, calculated_at=datetime(2026, 2, 5, 9, 0, tzinfo=SHANGHAI_TZ))
            self.assertEqual(first.status, "created")
            self.assertEqual(second.status, "exists")
            self.assertEqual(first.archive_dir, second.archive_dir)
            self.assertEqual(first.outcome_ids, second.outcome_ids)
            self.assertEqual(
                original,
                {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first.archive_dir.iterdir()},
            )

    def test_price_artifact_hash_uses_exact_input_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "prices.json"
            source_bytes = _price_bytes(_price_source()).replace(b"  ", b"    ", 1)
            path.write_bytes(source_bytes)
            loaded = load_price_artifact(path)
            self.assertEqual(loaded.source_file_sha256, hashlib.sha256(source_bytes).hexdigest())

    def test_synchronized_payload_tampering_is_detected_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root)
            prices = _price_source()
            first = self._run(root, signals, prices)
            json_path = first.archive_dir / "outcomes.json"
            manifest_path = first.archive_dir / "manifest.json"
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            payload["outcomes"][0]["future_return_pct"] = 999.0
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["outcomes.json"] = hashlib.sha256(json_path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first.archive_dir.iterdir()}
            with self.assertRaises(OutcomeConflictError):
                self._run(root, signals, prices)
            self.assertEqual(
                before,
                {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first.archive_dir.iterdir()},
            )

    def test_changed_price_file_creates_new_versioned_batch_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root)
            first = self._run(root, signals, _price_source())
            original_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first.archive_dir.iterdir()}
            changed = _price_source()
            changed["stocks"]["600100"]["prices"][1]["close"] = 104.5
            changed["stocks"]["600100"]["prices"][1]["open"] = 104.5
            second = self._run(root, signals, changed)
            self.assertNotEqual(first.archive_dir, second.archive_dir)
            self.assertEqual(first.outcome_ids, second.outcome_ids)
            self.assertEqual(
                original_hashes,
                {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in first.archive_dir.iterdir()},
            )

    def test_changed_calculation_version_creates_distinct_ids_and_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root)
            prices = _price_source()
            first = self._run(root, signals, prices)
            second = self._run(root, signals, prices, calculation_version="V2.2-OUTCOME-2")
            self.assertNotEqual(first.archive_dir, second.archive_dir)
            self.assertNotEqual(first.outcome_ids, second.outcome_ids)

    def test_signal_archive_files_are_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root)
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in signals.rglob("*") if path.is_file()}
            self._run(root, signals, _price_source())
            after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in signals.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_stable_outcome_id_contract(self) -> None:
        first = build_outcome_id("signal-a", 5, DEFAULT_CALCULATION_VERSION)
        self.assertEqual(first, build_outcome_id("signal-a", 5, DEFAULT_CALCULATION_VERSION))
        self.assertNotEqual(first, build_outcome_id("signal-a", 10, DEFAULT_CALCULATION_VERSION))

    def test_price_data_later_than_evaluation_cutoff_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(OutcomeValidationError, "price_data_as_of"):
                self._run(root, self._signals(root), _price_source(), as_of="2026-02-02T16:00:00+08:00")

    def test_cli_calculate_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            signals = self._signals(root)
            prices_path = root / "prices.json"
            prices_path.write_bytes(_price_bytes(_price_source()))
            try:
                import pyarrow  # noqa: F401
            except ImportError:
                self.skipTest("isolated research PyArrow dependency is not installed")
            exit_code = cli_main(
                [
                    "calculate-outcomes",
                    "--signals",
                    str(signals),
                    "--prices",
                    str(prices_path),
                    "--output",
                    str(root / "cli-outcomes"),
                    "--as-of",
                    "2026-02-03T16:00:00+08:00",
                ]
            )
            self.assertEqual(exit_code, 0)

    def test_real_parquet_writer_when_research_dependency_is_available(self) -> None:
        try:
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("isolated research PyArrow dependency is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = _price_bytes(_price_source())
            result = calculate_outcomes(
                self._signals(root),
                _price_source(),
                price_file_sha256=hashlib.sha256(raw).hexdigest(),
                output_root=root / "real-parquet",
                as_of="2026-02-03T16:00:00+08:00",
                calculated_at=self.calculated_at,
            )
            table = pq.read_table(result.archive_dir / "outcomes.parquet")
            self.assertEqual(table.num_rows, 5)
            self.assertIn("max_drawdown_pct", table.column_names)


if __name__ == "__main__":
    unittest.main()
