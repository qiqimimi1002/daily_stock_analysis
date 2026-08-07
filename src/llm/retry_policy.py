"""Bounded retry policy for transient Gemini API failures."""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from typing import Any, Callable, TypeVar


T = TypeVar("T")
_EVENT_LOCK = threading.Lock()


def classify_gemini_error(exc: BaseException) -> str:
    """Classify only the transient Gemini errors that are safe to retry."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if re.search(r"\b429\b", text) or any(
        marker in text
        for marker in ("rate limit", "quota", "resource_exhausted", "too many requests")
    ):
        return "gemini_429"
    if re.search(r"\b503\b", text) or any(
        marker in text
        for marker in ("service unavailable", "serviceunavailable", "high demand", "temporarily unavailable")
    ):
        return "gemini_503"
    return "non_retryable"


def append_retry_event(path: Path, event: dict[str, Any]) -> None:
    """Append a sanitized retry event as one JSONL record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, allow_nan=False, sort_keys=True)
    with _EVENT_LOCK, path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def run_with_gemini_retries(
    call: Callable[[int], T],
    *,
    key_count: int,
    max_retries: int,
    base_delay: float,
    max_delay: float,
    sleep: Callable[[float], None],
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> T:
    """Run ``call`` with finite backoff; rotate keys only after explicit 429 errors."""
    retries = max(0, max_retries)
    available_keys = max(1, key_count)
    key_index = 0
    attempt = 1
    while True:
        try:
            return call(key_index)
        except Exception as exc:
            error_type = classify_gemini_error(exc)
            retryable = error_type in {"gemini_429", "gemini_503"}
            exhausted = not retryable or attempt > retries
            key_switched = False
            next_key_index = key_index
            if not exhausted and error_type == "gemini_429" and key_index + 1 < available_keys:
                next_key_index = key_index + 1
                key_switched = True
            delay = 0.0 if exhausted else min(max_delay, base_delay * (2 ** (attempt - 1)))
            event = {
                "action": "exhausted" if exhausted else "retry",
                "attempt": attempt,
                "max_attempts": retries + 1,
                "error_type": error_type,
                "delay_seconds": delay,
                "key_index": key_index,
                "key_switched": key_switched,
            }
            if on_event:
                on_event(event)
            if exhausted:
                raise
            sleep(delay)
            key_index = next_key_index
            attempt += 1
