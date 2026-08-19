"""Phase 2A contract for the project baseline 60-day Low Volatility model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import math
from statistics import stdev
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from research.benchmarks.schema import (
    BenchmarkModelIdentity,
    BenchmarkValidationError,
)
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
)
from src.services.market_screener import ScreeningConfig


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MODEL_NAME = "low_volatility_daily_60d_v1"
MODEL_FAMILY = "low_volatility"
MODEL_VERSION = "1.0.0"
MODEL_VARIANT = "project_baseline_60d"
CALCULATION_VERSION = "low-volatility-daily-60d-v1"
RETURN_TYPE = "simple"
REQUIRED_RETURN_OBSERVATIONS = 60
REQUIRED_CLOSE_OBSERVATIONS = 61
STD_DDOF = 1
ANNUALIZATION_FACTOR = 252
RANKING_DIRECTION = "ascending"
BENCHMARK_TOP_N = 5
PRICE_BASIS = "raw_unadjusted"
PRICE_BASIS_POLICY = "raw_unadjusted_only"
CORPORATE_ACTION_POLICY = "review_no_signal"


class LowVolatilityStatus(str, Enum):
    ELIGIBLE = "eligible"
    INSUFFICIENT_HISTORY = "insufficient_history"
    CORPORATE_ACTION_REVIEW = "corporate_action_review"
    INVALID_DATA = "invalid_data"


@dataclass(frozen=True)
class LowVolatilityResult:
    stock_code: str
    status: LowVolatilityStatus
    reasons: Tuple[str, ...]
    raw_metric: Optional[Mapping[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_metric": dict(self.raw_metric) if self.raw_metric else None,
            "reasons": list(self.reasons),
            "status": self.status.value,
            "stock_code": self.stock_code,
        }


def create_model_identity(
    *,
    generated_at: datetime | str,
    universe_config: Optional[ScreeningConfig] = None,
) -> BenchmarkModelIdentity:
    """Create the frozen Phase 2A model identity without running a model."""

    return BenchmarkModelIdentity.create(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        model_family=MODEL_FAMILY,
        variant=MODEL_VARIANT,
        calculation_version=CALCULATION_VERSION,
        generated_at=generated_at,
        parameters={
            "annualization_factor": ANNUALIZATION_FACTOR,
            "corporate_action_policy": CORPORATE_ACTION_POLICY,
            "lookback_returns": REQUIRED_RETURN_OBSERVATIONS,
            "price_basis_policy": PRICE_BASIS_POLICY,
            "ranking_direction": RANKING_DIRECTION,
            "required_close_observations": REQUIRED_CLOSE_OBSERVATIONS,
            "return_type": RETURN_TYPE,
            "std_ddof": STD_DDOF,
            "top_n": BENCHMARK_TOP_N,
            "universe_config_hash": universe_config_hash(universe_config),
            "universe_contract_version": UNIVERSE_CONTRACT_VERSION,
        },
    )


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        raise BenchmarkValidationError(f"{field} must be a date")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise BenchmarkValidationError(f"{field} must be an ISO-8601 date") from None


def _datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            raise BenchmarkValidationError(
                f"{field} must be an ISO-8601 datetime"
            ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BenchmarkValidationError(f"{field} must include a timezone offset")
    return parsed.astimezone(SHANGHAI_TZ)


def _stock_code(value: Any) -> str:
    code = str(value or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise BenchmarkValidationError("stock_code must contain exactly six digits")
    return code


def _positive_close(value: Any) -> float:
    try:
        close = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BenchmarkValidationError("close must be a finite positive number") from None
    if not math.isfinite(close) or close <= 0:
        raise BenchmarkValidationError("close must be a finite positive number")
    return close


def _normalized_closes(
    closes: Sequence[Mapping[str, Any]],
    *,
    signal_date: date,
) -> Dict[date, float]:
    normalized: Dict[date, float] = {}
    for item in closes:
        trade_date = _date(item.get("trade_date"), field="trade_date")
        if trade_date >= signal_date:
            raise BenchmarkValidationError(
                "history closes must be strictly earlier than signal_date"
            )
        if trade_date in normalized:
            raise BenchmarkValidationError("history closes cannot contain duplicate dates")
        normalized[trade_date] = _positive_close(item.get("close"))
    return normalized


def _has_reviewable_corporate_action(
    actions: Sequence[Mapping[str, Any]],
    *,
    window_dates: Sequence[date],
    market_data_at: datetime,
) -> bool:
    window = set(window_dates)
    reviewable = False
    for action in actions:
        action_date = _date(action.get("trade_date"), field="corporate_action.trade_date")
        known_at = _datetime(action.get("known_at"), field="corporate_action.known_at")
        if known_at > market_data_at:
            raise BenchmarkValidationError(
                "corporate action knowledge cannot be later than market_data_at"
            )
        action_type = str(action.get("action_type") or "").strip()
        if not action_type:
            raise BenchmarkValidationError("corporate_action.action_type is required")
        if action_date in window:
            reviewable = True
    return reviewable


def simple_daily_returns(closes: Sequence[float]) -> Tuple[float, ...]:
    """Return close-to-close simple returns without filling or interpolation."""

    normalized = tuple(_positive_close(value) for value in closes)
    return tuple(
        normalized[index] / normalized[index - 1] - 1.0
        for index in range(1, len(normalized))
    )


def evaluate_history(
    *,
    model: BenchmarkModelIdentity,
    stock_code: str,
    signal_date: date | str,
    market_data_at: datetime | str,
    source_data_as_of: datetime | str,
    fetched_at: datetime | str | None,
    history_data_as_of: datetime | str,
    history_source: str,
    trade_calendar_source: str,
    previous_completed_trade_date: date | str,
    price_basis: str,
    expected_trade_dates: Sequence[date | str],
    closes: Sequence[Mapping[str, Any]],
    corporate_action_reviewed: bool,
    corporate_action_source: str,
    corporate_action_data_as_of: datetime | str,
    corporate_actions: Sequence[Mapping[str, Any]],
) -> LowVolatilityResult:
    """Validate one exact 61-close window and calculate its frozen metric."""

    code = _stock_code(stock_code)
    local_signal_date = _date(signal_date, field="signal_date")
    local_market_time = _datetime(market_data_at, field="market_data_at")
    local_source_time = _datetime(source_data_as_of, field="source_data_as_of")
    local_history_time = _datetime(history_data_as_of, field="history_data_as_of")
    local_action_time = _datetime(
        corporate_action_data_as_of,
        field="corporate_action_data_as_of",
    )
    local_previous_trade_date = _date(
        previous_completed_trade_date,
        field="previous_completed_trade_date",
    )
    local_fetched_time = (
        _datetime(fetched_at, field="fetched_at") if fetched_at is not None else None
    )
    if local_market_time.date() != local_signal_date:
        raise BenchmarkValidationError(
            "signal_date must match market_data_at in Asia/Shanghai"
        )
    if not local_history_time <= local_source_time <= local_market_time:
        raise BenchmarkValidationError(
            "history_data_as_of <= source_data_as_of <= market_data_at is required"
        )
    if not local_history_time <= local_action_time <= local_market_time:
        raise BenchmarkValidationError(
            "history_data_as_of <= corporate_action_data_as_of <= "
            "market_data_at is required"
        )
    if local_market_time > model.generated_at:
        raise BenchmarkValidationError(
            "market_data_at cannot be later than generated_at"
        )
    if price_basis != PRICE_BASIS:
        raise BenchmarkValidationError("price_basis must be raw_unadjusted")
    if not str(history_source or "").strip():
        raise BenchmarkValidationError("history_source is required")
    if not str(trade_calendar_source or "").strip():
        raise BenchmarkValidationError("trade_calendar_source is required")
    if not str(corporate_action_source or "").strip():
        raise BenchmarkValidationError("corporate_action_source is required")

    window_dates = tuple(
        _date(value, field="expected_trade_dates") for value in expected_trade_dates
    )
    if tuple(sorted(set(window_dates))) != window_dates:
        raise BenchmarkValidationError(
            "expected_trade_dates must be unique and strictly increasing"
        )
    if any(item >= local_signal_date for item in window_dates):
        raise BenchmarkValidationError(
            "history_window_end must be strictly earlier than signal_date"
        )
    if window_dates and window_dates[-1] != local_previous_trade_date:
        raise BenchmarkValidationError(
            "history window must end on previous_completed_trade_date"
        )
    normalized_closes = _normalized_closes(closes, signal_date=local_signal_date)
    if len(window_dates) < REQUIRED_CLOSE_OBSERVATIONS:
        return LowVolatilityResult(
            code,
            LowVolatilityStatus.INSUFFICIENT_HISTORY,
            ("required_61_trade_dates_not_available",),
            None,
        )
    if len(window_dates) > REQUIRED_CLOSE_OBSERVATIONS:
        return LowVolatilityResult(
            code,
            LowVolatilityStatus.INVALID_DATA,
            ("history_window_must_contain_exactly_61_trade_dates",),
            None,
        )
    missing_dates = [item for item in window_dates if item not in normalized_closes]
    if missing_dates:
        return LowVolatilityResult(
            code,
            LowVolatilityStatus.INSUFFICIENT_HISTORY,
            ("required_trade_date_missing",),
            None,
        )
    if not corporate_action_reviewed:
        return LowVolatilityResult(
            code,
            LowVolatilityStatus.CORPORATE_ACTION_REVIEW,
            ("corporate_action_review_not_completed",),
            None,
        )
    if _has_reviewable_corporate_action(
        corporate_actions,
        window_dates=window_dates,
        market_data_at=local_market_time,
    ):
        return LowVolatilityResult(
            code,
            LowVolatilityStatus.CORPORATE_ACTION_REVIEW,
            ("corporate_action_in_history_window",),
            None,
        )

    ordered_closes = [normalized_closes[item] for item in window_dates]
    returns = simple_daily_returns(ordered_closes)
    volatility_daily = stdev(returns)
    raw_metric = {
        "close_observations": len(ordered_closes),
        "corporate_action_data_as_of": local_action_time.isoformat(
            timespec="seconds"
        ),
        "corporate_action_source": str(corporate_action_source).strip(),
        "corporate_action_status": "reviewed_clear",
        "history_data_as_of": local_history_time.isoformat(timespec="seconds"),
        "history_source": str(history_source).strip(),
        "lookback_return_observations": REQUIRED_RETURN_OBSERVATIONS,
        "price_basis": PRICE_BASIS,
        "return_observations": len(returns),
        "trade_calendar_source": str(trade_calendar_source).strip(),
        "universe_config_hash": model.parameters["universe_config_hash"],
        "universe_contract_version": model.parameters[
            "universe_contract_version"
        ],
        "volatility_annualized": volatility_daily * math.sqrt(
            ANNUALIZATION_FACTOR
        ),
        "volatility_daily_60d": volatility_daily,
        "window_end": window_dates[-1].isoformat(),
        "window_start": window_dates[0].isoformat(),
    }
    if local_fetched_time is not None:
        raw_metric["fetched_at"] = local_fetched_time.isoformat(timespec="seconds")
    return LowVolatilityResult(
        code,
        LowVolatilityStatus.ELIGIBLE,
        (),
        raw_metric,
    )


def rank_eligible(
    results: Sequence[LowVolatilityResult],
) -> Sequence[LowVolatilityResult]:
    """Return the complete eligible ranking; Top 5 selection belongs to Phase 2B."""

    eligible = [
        item
        for item in results
        if item.status is LowVolatilityStatus.ELIGIBLE and item.raw_metric
    ]
    return sorted(
        eligible,
        key=lambda item: (
            item.raw_metric["volatility_daily_60d"],
            item.stock_code,
        ),
    )
