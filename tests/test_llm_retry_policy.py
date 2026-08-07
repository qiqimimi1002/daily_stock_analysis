from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.llm.retry_policy import (
    append_retry_event,
    classify_gemini_error,
    run_with_gemini_retries,
)


class GeminiRetryPolicyTest(unittest.TestCase):
    def test_classifies_transient_and_non_retryable_errors(self) -> None:
        self.assertEqual(classify_gemini_error(RuntimeError("HTTP 429 quota exceeded")), "gemini_429")
        self.assertEqual(classify_gemini_error(RuntimeError("503 service unavailable")), "gemini_503")
        self.assertEqual(classify_gemini_error(ValueError("invalid request")), "non_retryable")

    def test_503_uses_bounded_backoff_without_switching_key(self) -> None:
        calls: list[int] = []
        sleeps: list[float] = []
        events: list[dict] = []

        def call(key_index: int) -> str:
            calls.append(key_index)
            if len(calls) < 3:
                raise RuntimeError("503 high demand")
            return "ok"

        result = run_with_gemini_retries(
            call,
            key_count=3,
            max_retries=2,
            base_delay=2,
            max_delay=3,
            sleep=sleeps.append,
            on_event=events.append,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [0, 0, 0])
        self.assertEqual(sleeps, [2, 3])
        self.assertTrue(all(not event["key_switched"] for event in events))

    def test_429_switches_only_to_an_unused_configured_key(self) -> None:
        calls: list[int] = []

        def call(key_index: int) -> str:
            calls.append(key_index)
            if len(calls) < 3:
                raise RuntimeError("429 rate limit")
            return "ok"

        result = run_with_gemini_retries(
            call,
            key_count=2,
            max_retries=3,
            base_delay=0,
            max_delay=0,
            sleep=lambda _: None,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, [0, 1, 1])

    def test_non_retryable_error_is_not_retried(self) -> None:
        calls = 0
        events: list[dict] = []

        def call(_: int) -> str:
            nonlocal calls
            calls += 1
            raise ValueError("bad request")

        with self.assertRaises(ValueError):
            run_with_gemini_retries(
                call,
                key_count=2,
                max_retries=5,
                base_delay=1,
                max_delay=4,
                sleep=lambda _: None,
                on_event=events.append,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(events[0]["action"], "exhausted")

    def test_retry_exhaustion_is_finite_and_event_contains_no_secret(self) -> None:
        events: list[dict] = []
        with self.assertRaises(RuntimeError):
            run_with_gemini_retries(
                lambda _: (_ for _ in ()).throw(RuntimeError("429 key SECRET_VALUE quota")),
                key_count=1,
                max_retries=1,
                base_delay=0,
                max_delay=0,
                sleep=lambda _: None,
                on_event=events.append,
            )
        self.assertEqual([event["action"] for event in events], ["retry", "exhausted"])
        self.assertNotIn("SECRET_VALUE", json.dumps(events))

    def test_jsonl_event_writer_produces_valid_sanitized_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            append_retry_event(path, {"error_type": "gemini_503", "attempt": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {
                "attempt": 1,
                "error_type": "gemini_503",
            })


if __name__ == "__main__":
    unittest.main()
