"""Tushare ``rt_k`` realtime daily-bar provider.

The provider owns authentication, request retry, field/unit normalization and
quote-time validation.  It never appends the unfinished intraday bar to daily
history.  Synthetic tests exercise this module; live rows must not be committed.
"""

from __future__ import annotations

import math
import os
import re
import time
from datetime import datetime, time as clock_time
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from data_provider.tushare_fetcher import _TushareHttpClient


CN_TZ = ZoneInfo("Asia/Shanghai")
RT_K_API_NAME = "rt_k"
RT_K_MAX_CODES = 5
RT_K_FIELDS = (
    "ts_code,name,pre_close,open,high,low,close,vol,amount,num,"
    "bid_price1,bid_volume1,ask_price1,ask_volume1,trade_time"
)
_RT_K_STOCK_CODE_PATTERN = re.compile(r"^[0-9]{6}\.(SH|SZ)$")


class TushareRtKError(RuntimeError):
    """Base error with a stable, non-sensitive category."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class TushareRtKAuthError(TushareRtKError):
    """Missing, invalid or unauthorized token."""


class TushareRtKValidationError(TushareRtKError):
    """Response cannot satisfy the realtime snapshot contract."""


class TushareRtKRecoverableError(TushareRtKError):
    """Transient request failure after the finite retry budget is exhausted."""


def validate_rt_k_stock_codes(value: str) -> list[str]:
    """Validate one to five exact Tushare A-share stock codes."""

    if not isinstance(value, str):
        raise TushareRtKValidationError(
            "stock_code_invalid",
            "rt_k stock code input must be a comma-separated string",
        )
    codes = [item.strip() for item in value.split(",")]
    if not codes or any(not code for code in codes):
        raise TushareRtKValidationError(
            "stock_code_invalid",
            "rt_k stock code input contains an empty item",
        )
    if len(codes) > RT_K_MAX_CODES:
        raise TushareRtKValidationError(
            "stock_code_limit",
            f"rt_k accepts at most {RT_K_MAX_CODES} stock codes",
        )
    if any(_RT_K_STOCK_CODE_PATTERN.fullmatch(code) is None for code in codes):
        raise TushareRtKValidationError(
            "stock_code_invalid",
            "rt_k stock code must match six digits followed by .SH or .SZ",
        )
    if len(set(codes)) != len(codes):
        raise TushareRtKValidationError(
            "stock_code_duplicate",
            "rt_k duplicate stock code is not allowed",
        )
    return codes


def _default_phase(now: datetime) -> str:
    from src.core.trading_calendar import infer_market_phase

    phase = infer_market_phase("cn", current_time=now)
    return str(getattr(phase, "value", phase))


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _parse_trade_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed.astimezone(CN_TZ)


def _request_error_category(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in ("token", "认证", "权限", "permission", "http 401", "http 403")):
        return "authentication_or_permission"
    if "429" in text or "rate limit" in text or "频率" in text:
        return "rate_limited"
    if any(f"http {status}" in text for status in range(500, 600)):
        return "server_error"
    if any(marker in text for marker in ("timeout", "timed out", "connection", "network")):
        return "network_timeout"
    return "non_retryable_provider_error"


class TushareRtKProvider:
    """Fetch and validate one Tushare realtime daily-bar snapshot."""

    name = "tushare_rt_k"

    def __init__(
        self,
        *,
        client: Optional[Any] = None,
        now: Optional[Callable[[], datetime]] = None,
        market_phase: Optional[Callable[[datetime], str]] = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        max_stale_seconds: Optional[int] = None,
        request_timeout_seconds: int = 15,
        rate_limit_per_minute: int = 50,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if rate_limit_per_minute < 1 or rate_limit_per_minute > 50:
            raise ValueError("rate_limit_per_minute must be between 1 and 50")
        self._now = now or (lambda: datetime.now(CN_TZ))
        self._market_phase = market_phase or _default_phase
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._max_stale_seconds = int(
            max_stale_seconds
            if max_stale_seconds is not None
            else os.getenv("TUSHARE_RT_K_MAX_STALE_SECONDS", "180")
        )
        self._rate_limit_per_minute = rate_limit_per_minute
        self._calls_in_window = 0
        self._window_started = time.monotonic()
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}
        self._last_quote_times: dict[str, datetime] = {}
        self._client = client
        self._request_timeout_seconds = request_timeout_seconds

    def fetch(self, ts_code: str) -> pd.DataFrame:
        codes = validate_rt_k_stock_codes(ts_code)
        validated_query = ",".join(codes)
        self._ensure_client()
        now = self._now().astimezone(CN_TZ)
        cache_key = (now.date().isoformat(), validated_query)
        cached = self._cache.get(cache_key)
        if cached is not None:
            result = cached.copy(deep=True)
            result.attrs = dict(cached.attrs)
            result.attrs["cache_hit"] = True
            return result

        started_at = datetime.now(CN_TZ)
        started = time.perf_counter()
        raw = self._query_with_retry(validated_query)
        fetched_at = datetime.now(CN_TZ)
        result = self._normalize(raw, now=now)
        result.attrs.update(
            {
                "api_name": RT_K_API_NAME,
                "market_data_source": self.name,
                "fetched_at": fetched_at.isoformat(timespec="seconds"),
                "request_started_at": started_at.isoformat(timespec="seconds"),
                "request_elapsed_seconds": round(time.perf_counter() - started, 4),
                "row_count": len(result),
                "cache_hit": False,
                "fallback_status": "primary",
                "quality_status": "ok",
                "price_basis": "raw_unadjusted_intraday_daily_bar",
                "volume_unit": "shares",
                "amount_unit": "yuan",
            }
        )
        self._cache = {cache_key: result.copy(deep=True)}
        self._cache[cache_key].attrs = dict(result.attrs)
        self._last_quote_times.update(
            {
                str(row["code"]): _parse_trade_time(row["market_data_at"])
                for _, row in result.iterrows()
            }
        )
        return result

    def _ensure_client(self) -> None:
        resolved_token = os.getenv("TUSHARE_TOKEN")
        if not resolved_token or not str(resolved_token).strip():
            raise TushareRtKAuthError(
                "token_missing",
                "tushare_rt_k token is missing",
            )
        if self._client is not None:
            return
        api_url = str(
            os.getenv("TUSHARE_HTTP_URL") or "https://api.tushare.pro"
        ).strip()
        if not api_url.startswith("https://"):
            raise ValueError("tushare_rt_k requires an HTTPS API endpoint")
        self._client = _TushareHttpClient(
            token=str(resolved_token).strip(),
            timeout=self._request_timeout_seconds,
            api_url=api_url,
        )

    def _query_with_retry(self, ts_code: str) -> pd.DataFrame:
        last_category = "non_retryable_provider_error"
        for attempt in range(1, self._max_attempts + 1):
            self._apply_rate_limit()
            try:
                return self._client.query(
                    RT_K_API_NAME,
                    fields=RT_K_FIELDS,
                    ts_code=ts_code,
                )
            except BaseException as exc:
                last_category = _request_error_category(exc)
                if last_category == "authentication_or_permission":
                    raise TushareRtKAuthError(
                        last_category,
                        "tushare_rt_k authentication or permission failed",
                    ) from None
                recoverable = last_category in {
                    "rate_limited",
                    "server_error",
                    "network_timeout",
                }
                if not recoverable:
                    raise TushareRtKError(
                        last_category,
                        "tushare_rt_k provider request failed",
                    ) from None
                if attempt < self._max_attempts:
                    self._sleep(min(4.0, float(2 ** (attempt - 1))))
                    continue
                break
        raise TushareRtKRecoverableError(
            last_category,
            "tushare_rt_k recoverable request retry budget exhausted",
        )

    def _apply_rate_limit(self) -> None:
        now = time.monotonic()
        elapsed = now - self._window_started
        if elapsed >= 60.0:
            self._window_started = now
            self._calls_in_window = 0
        if self._calls_in_window >= self._rate_limit_per_minute:
            self._sleep(max(0.0, 60.0 - elapsed))
            self._window_started = time.monotonic()
            self._calls_in_window = 0
        self._calls_in_window += 1

    def _normalize(self, raw: pd.DataFrame, *, now: datetime) -> pd.DataFrame:
        if raw is None or raw.empty:
            raise TushareRtKValidationError(
                "empty_response",
                "tushare_rt_k returned no rows",
            )
        required = {
            "ts_code",
            "name",
            "pre_close",
            "open",
            "high",
            "low",
            "close",
            "vol",
            "amount",
            "trade_time",
        }
        missing = sorted(required.difference(str(column) for column in raw.columns))
        if missing:
            raise TushareRtKValidationError(
                "field_missing",
                "tushare_rt_k response is missing required fields",
            )

        from src.services.market_screener import (
            is_excluded_name,
            is_main_board_code,
            normalize_stock_code,
        )

        frame = raw.copy()
        frame["code"] = frame["ts_code"].map(normalize_stock_code)
        if frame["code"].duplicated().any():
            raise TushareRtKValidationError(
                "duplicate_code",
                "tushare_rt_k duplicate ts_code",
            )
        frame = frame.loc[
            frame["code"].map(is_main_board_code)
            & ~frame["name"].map(is_excluded_name)
        ].copy()
        if frame.empty:
            raise TushareRtKValidationError(
                "main_board_empty",
                "tushare_rt_k has no eligible main-board rows",
            )

        numeric_map = {
            "pre_close": "prev_close",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "vol": "volume",
            "amount": "amount",
            "num": "num",
            "bid_price1": "bid_price1",
            "bid_volume1": "bid_volume1",
            "ask_price1": "ask_price1",
            "ask_volume1": "ask_volume1",
        }
        for source, target in numeric_map.items():
            if source in frame.columns:
                frame[target] = pd.to_numeric(frame[source], errors="coerce")
            else:
                frame[target] = math.nan
        parsed_times = frame["trade_time"].map(_parse_trade_time)
        frame["market_data_at"] = parsed_times.map(
            lambda value: value.isoformat(timespec="seconds") if value else None
        )
        frame["pct_change"] = (
            (frame["close"] - frame["prev_close"])
            / frame["prev_close"]
            * 100.0
        )
        frame["provider_pct_change"] = math.nan
        frame["prev_close_basis"] = "provider_exchange_reference"

        valid_rows = []
        invalid_categories: dict[str, int] = {}
        market_state = self._market_state(now)
        for index, row in frame.iterrows():
            category = self._row_error(row, parsed_times.loc[index], now)
            if category:
                invalid_categories[category] = invalid_categories.get(category, 0) + 1
            else:
                valid_rows.append(index)
        frame = frame.loc[valid_rows].copy()
        if frame.empty:
            category = next(iter(invalid_categories), "invalid_rows")
            raise TushareRtKValidationError(
                category,
                f"tushare_rt_k no valid rows: {category}",
            )

        market_data_at = max(
            _parse_trade_time(value) for value in frame["trade_time"]
        )
        output_columns = [
            "code",
            "name",
            "close",
            "prev_close",
            "open",
            "high",
            "low",
            "pct_change",
            "provider_pct_change",
            "volume",
            "amount",
            "num",
            "bid_price1",
            "bid_volume1",
            "ask_price1",
            "ask_volume1",
            "trade_time",
            "market_data_at",
            "prev_close_basis",
        ]
        frame = frame[output_columns].reset_index(drop=True)
        frame.attrs.update(
            {
                "market_data_at": market_data_at.isoformat(timespec="seconds"),
                "market_state": market_state,
                "invalid_row_counts": invalid_categories,
                "source_row_count": len(raw),
            }
        )
        return frame

    def _market_state(self, now: datetime) -> str:
        phase = str(self._market_phase(now))
        return "market_closed" if phase == "postmarket" else phase

    def _row_error(
        self,
        row: pd.Series,
        trade_time: Optional[datetime],
        now: datetime,
    ) -> Optional[str]:
        phase = str(self._market_phase(now))
        if phase == "premarket":
            return "premarket"
        if phase == "non_trading":
            return "non_trading"
        if trade_time is None:
            return "trade_time_invalid"
        prior_trade_time = self._last_quote_times.get(str(row.get("code") or ""))
        if prior_trade_time is not None and trade_time < prior_trade_time:
            return "time_rollback"
        if trade_time.date() != now.date():
            return "trade_date_mismatch"
        if (trade_time - now).total_seconds() > 5.0:
            return "future_timestamp"
        if phase == "intraday" and (now - trade_time).total_seconds() > self._max_stale_seconds:
            return "stale_quote"
        if phase == "lunch_break" and not (
            clock_time(11, 29) <= trade_time.time().replace(tzinfo=None) <= clock_time(11, 31)
        ):
            return "stale_lunch_snapshot"
        if phase in {"postmarket", "market_closed"} and trade_time.time().replace(tzinfo=None) < clock_time(14, 55):
            return "stale_close_snapshot"

        values = {name: _number(row.get(name)) for name in ("prev_close", "open", "high", "low", "close", "volume", "amount")}
        if any(values[name] is None or values[name] <= 0 for name in ("prev_close", "open", "high", "low", "close")):
            return "invalid_ohlc"
        if values["volume"] is None or values["volume"] < 0:
            return "invalid_volume"
        if values["amount"] is None or values["amount"] < 0:
            return "invalid_amount"
        if values["low"] > min(values["open"], values["close"]):
            return "invalid_ohlc"
        if values["high"] < max(values["open"], values["close"]):
            return "invalid_ohlc"
        return None
