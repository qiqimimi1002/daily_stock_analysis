# -*- coding: utf-8 -*-
"""Best-effort timing diagnostics for the production market screener."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
CN_TZ = ZoneInfo("Asia/Shanghai")


class MarketScreenerDiagnostics:
    """Append-only diagnostics that never controls screening behaviour."""

    schema_version = "1.0"

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        heartbeat_interval_seconds: float = 20.0,
        now: Optional[Callable[[], datetime]] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._now = now or (lambda: datetime.now(CN_TZ))
        self._monotonic = monotonic
        self._state_lock = Lock()
        self._write_lock = Lock()
        self._stop_event = Event()
        self._heartbeat_thread: Optional[Thread] = None
        self._run_started_monotonic: Optional[float] = None
        self._phase = "not_started"
        self._phase_total = 0
        self._phase_completed = 0
        self._pending_codes: set[str] = set()
        self._active_providers: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def timestamp(self) -> str:
        return self._now().astimezone(CN_TZ).isoformat(timespec="milliseconds")

    def monotonic(self) -> float:
        return self._monotonic()

    def emit(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        payload = {
            "schema_version": self.schema_version,
            "event": event,
            "timestamp": self.timestamp(),
            **fields,
        }
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            logger.warning("市场初筛诊断事件无法序列化: %s", type(exc).__name__)
            return

        logger.info("market_screener_timing %s", serialized)
        try:
            self._append_line(serialized)
        except OSError as exc:
            logger.warning("市场初筛诊断日志写入失败: %s", type(exc).__name__)

    def _append_line(self, serialized: str) -> None:
        if self.path is None:
            return
        with self._write_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", buffering=1) as handle:
                handle.write(serialized + "\n")
                handle.flush()

    def start(self) -> None:
        if not self.enabled or self._heartbeat_thread is not None:
            return
        self._run_started_monotonic = self.monotonic()
        self._stop_event.clear()
        self._heartbeat_thread = Thread(
            target=self._heartbeat_loop,
            name="market-screener-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        self.emit("run_started")

    def stop(self, *, status: str, error_category: Optional[str] = None) -> None:
        if not self.enabled:
            return
        elapsed = self._run_elapsed_seconds()
        self.emit(
            "run_completed",
            status=status,
            error_category=error_category,
            elapsed_seconds=elapsed,
        )
        self._stop_event.set()
        thread = self._heartbeat_thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._heartbeat_thread = None

    def begin_stage(
        self,
        stage: str,
        *,
        pending_codes: Iterable[str] = (),
        **fields: Any,
    ) -> float:
        started = self.monotonic()
        normalized_codes = {str(code) for code in pending_codes}
        with self._state_lock:
            self._phase = stage
            self._phase_total = len(normalized_codes)
            self._phase_completed = 0
            self._pending_codes = normalized_codes
            self._active_providers = {}
        self.emit(
            "stage_started",
            stage=stage,
            started_at=self.timestamp(),
            **fields,
        )
        return started

    def end_stage(
        self,
        stage: str,
        started_monotonic: float,
        *,
        status: str,
        error_category: Optional[str] = None,
        **fields: Any,
    ) -> None:
        self.emit(
            "stage_completed",
            stage=stage,
            completed_at=self.timestamp(),
            elapsed_seconds=round(self.monotonic() - started_monotonic, 6),
            status=status,
            error_category=error_category,
            **fields,
        )

    def request_started(
        self,
        *,
        code: str,
        provider: str,
        attempt: int,
    ) -> tuple[float, str]:
        started_monotonic = self.monotonic()
        started_at = self.timestamp()
        self.set_active_provider(
            code=str(code),
            provider=provider,
            attempt=attempt,
        )
        self.emit(
            "history_request_started",
            code=str(code),
            provider=provider,
            attempt=int(attempt),
            request_started_at=started_at,
            request_completed_at=None,
            elapsed_seconds=None,
            status="started",
            success=None,
            error_category=None,
        )
        return started_monotonic, started_at

    def request_completed(
        self,
        *,
        code: str,
        provider: str,
        attempt: int,
        started_monotonic: float,
        started_at: str,
        success: bool,
        error_category: Optional[str],
    ) -> None:
        self.clear_active_provider(code=str(code), provider=provider)
        self.emit(
            "history_request_completed",
            code=str(code),
            provider=provider,
            attempt=int(attempt),
            request_started_at=started_at,
            request_completed_at=self.timestamp(),
            elapsed_seconds=round(self.monotonic() - started_monotonic, 6),
            status="success" if success else "failure",
            success=bool(success),
            error_category=error_category,
        )

    def set_active_provider(
        self,
        *,
        code: str,
        provider: str,
        attempt: int = 1,
    ) -> None:
        if not self.enabled:
            return
        with self._state_lock:
            self._active_providers[str(code)] = {
                "code": str(code),
                "provider": provider,
                "attempt": int(attempt),
            }

    def clear_active_provider(self, *, code: str, provider: str) -> None:
        if not self.enabled:
            return
        with self._state_lock:
            active = self._active_providers.get(str(code))
            if active and active.get("provider") == provider:
                self._active_providers.pop(str(code), None)

    def mark_completed(self, code: str) -> None:
        normalized = str(code)
        with self._state_lock:
            if normalized in self._pending_codes:
                self._pending_codes.remove(normalized)
                self._phase_completed += 1
            self._active_providers.pop(normalized, None)

    def emit_heartbeat(self) -> None:
        if not self.enabled:
            return
        with self._state_lock:
            phase = self._phase
            total = self._phase_total
            completed = self._phase_completed
            pending = sorted(self._pending_codes)
            active = sorted(
                (dict(item) for item in self._active_providers.values()),
                key=lambda item: (str(item.get("code")), str(item.get("provider"))),
            )
        self.emit(
            "heartbeat",
            current_stage=phase,
            completed_count=completed,
            total_count=total,
            pending_codes=pending,
            active_providers=active,
            run_elapsed_seconds=self._run_elapsed_seconds(),
        )

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_interval_seconds):
            self.emit_heartbeat()

    def _run_elapsed_seconds(self) -> Optional[float]:
        if self._run_started_monotonic is None:
            return None
        return round(self.monotonic() - self._run_started_monotonic, 6)


def diagnostic_error_category(exc: BaseException) -> str:
    """Return a safe, stable category without logging provider error text."""
    return type(exc).__name__.lower()
