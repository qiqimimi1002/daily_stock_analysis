"""Offline Short-term v1: positive 20-session trend and 5-session rank."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import math
from statistics import stdev
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from research.benchmarks.low_volatility import BENCHMARK_TOP_N
from research.benchmarks.raw_history import (
    ACCEPTANCE_STATUS as RAW_HISTORY_ACCEPTANCE_STATUS,
    CROSS_ADJUSTMENT,
    CROSS_RAW_SOURCE_ID,
    PRICE_BASIS,
    PRIMARY_ADJUSTMENT,
    PRIMARY_RAW_SOURCE_ID,
    RawDailyBar,
    RawHistoryAcceptance,
    RawHistoryObservation,
)
from research.benchmarks.schema import (
    SHANGHAI_TZ,
    BenchmarkModelIdentity,
    BenchmarkSignal,
    BenchmarkValidationError,
    canonical_json_bytes,
)
from research.benchmarks.trade_calendar import HistoryWindowContract
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    universe_config_hash,
)
from src.services.market_screener import ScreeningConfig


MODEL_NAME = "short_term_relative_strength_daily_v1"
MODEL_FAMILY = "short_term_relative_strength"
MODEL_VERSION = "1.0.0"
MODEL_VARIANT = "positive_trend_20d_rank_5d"
CALCULATION_VERSION = "short-term-positive-trend-20d-rank-5d-v1"
RETURN_TYPE = "simple"
TREND_LOOKBACK_SESSIONS = 20
RANK_LOOKBACK_SESSIONS = 5
REQUIRED_CLOSE_OBSERVATIONS = TREND_LOOKBACK_SESSIONS + 1
ABLATION_REQUIRED_RETURN_OBSERVATIONS = 60
ABLATION_REQUIRED_CLOSE_OBSERVATIONS = 61
VOLATILITY_SHORT_SESSIONS = 10
VOLATILITY_LONG_SESSIONS = 60
BREAKOUT_LOOKBACK_SESSIONS = 20
VOLUME_LOOKBACK_SESSIONS = 5
STD_DDOF = 1
RANKING_DIRECTION = "descending"
RANKING_TIE_BREAK = "stock_code_ascending"
PRICE_BASIS_POLICY = "raw_unadjusted_only"
CORPORATE_ACTION_POLICY = "review_no_signal"
AMOUNT_ROLE = "v2_1_universe_liquidity_gate_only"
OUTCOME_HORIZONS = ("1d", "3d", "5d", "10d", "20d")

_PROSPECTIVE_ACQUISITION_MODE = "prospective_cutoff"
_PUBLIC_PAYLOAD_POLICY = "metadata_and_hashes_only_no_raw_rows"
_LICENSE_BOUNDARY = "software_license_does_not_grant_raw_data_redistribution"


class ShortTermStatus(str, Enum):
    ELIGIBLE = "eligible"
    TREND_FILTERED = "trend_filtered"
    INSUFFICIENT_HISTORY = "insufficient_history"
    CORPORATE_ACTION_REVIEW = "corporate_action_review"


class AblationFactorStatus(str, Enum):
    ELIGIBLE = "eligible"
    UNDEFINED = "undefined"
    CORPORATE_ACTION_REVIEW = "corporate_action_review"


@dataclass(frozen=True)
class ShortTermResult:
    stock_code: str
    status: ShortTermStatus
    reasons: Tuple[str, ...]
    raw_metric: Optional[Mapping[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw_metric": dict(self.raw_metric) if self.raw_metric else None,
            "reasons": list(self.reasons),
            "status": self.status.value,
            "stock_code": self.stock_code,
        }


@dataclass(frozen=True)
class AblationFactorResult:
    stock_code: str
    factor_name: str
    status: AblationFactorStatus
    value: Optional[float]
    reason: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factor_name": self.factor_name,
            "reason": self.reason,
            "status": self.status.value,
            "stock_code": self.stock_code,
            "value": self.value,
        }


@dataclass(frozen=True)
class _ValidatedWindow:
    stock_code: str
    closes: Tuple[float, ...]
    volumes: Tuple[float, ...]
    dates: Tuple[date, ...]
    corporate_action_review: bool
    corporate_action_reason: Optional[str]
    corporate_action_data_as_of: datetime
    corporate_action_source: str


def create_model_identity(
    *,
    generated_at: datetime | str,
    universe_config: Optional[ScreeningConfig] = None,
) -> BenchmarkModelIdentity:
    """Create the immutable Short-term v1 research identity."""

    return BenchmarkModelIdentity.create(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        model_family=MODEL_FAMILY,
        variant=MODEL_VARIANT,
        calculation_version=CALCULATION_VERSION,
        generated_at=generated_at,
        parameters={
            "amount_role": AMOUNT_ROLE,
            "corporate_action_policy": CORPORATE_ACTION_POLICY,
            "price_basis_policy": PRICE_BASIS_POLICY,
            "ranking_direction": RANKING_DIRECTION,
            "ranking_lookback_sessions": RANK_LOOKBACK_SESSIONS,
            "ranking_tie_break": RANKING_TIE_BREAK,
            "required_close_observations": REQUIRED_CLOSE_OBSERVATIONS,
            "return_type": RETURN_TYPE,
            "top_n": BENCHMARK_TOP_N,
            "trend_gate": "ret_20_strictly_greater_than_zero",
            "trend_lookback_sessions": TREND_LOOKBACK_SESSIONS,
            "universe_config_hash": universe_config_hash(universe_config),
            "universe_contract_version": UNIVERSE_CONTRACT_VERSION,
        },
    )


def _stock_code(value: Any) -> str:
    code = str(value or "").strip()
    if len(code) != 6 or not code.isdigit():
        raise BenchmarkValidationError("stock_code must contain exactly six digits")
    return code


def _aware_datetime(value: datetime | str, *, field: str) -> datetime:
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


def _finite_number(value: Any, *, field: str, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BenchmarkValidationError(f"{field} must be a finite number") from None
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive " if positive else ""
        raise BenchmarkValidationError(f"{field} must be a finite {qualifier}number")
    return number


def _verify_manifest_hash(manifest: Mapping[str, Any]) -> None:
    payload = dict(manifest)
    expected = str(payload.pop("manifest_sha256", ""))
    actual = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if not expected or expected != actual:
        raise BenchmarkValidationError("raw-history acceptance manifest hash mismatch")


def _validate_raw_history_acceptance(
    *,
    stock_code: str,
    history_window: HistoryWindowContract,
    history_observation: RawHistoryObservation,
    history_acceptance: RawHistoryAcceptance,
) -> None:
    manifest = history_acceptance.manifest
    _verify_manifest_hash(manifest)
    if manifest.get("acceptance_status") != RAW_HISTORY_ACCEPTANCE_STATUS:
        raise BenchmarkValidationError("raw-history acceptance must be conditional_pass")
    if manifest.get("acquisition_mode") != _PROSPECTIVE_ACQUISITION_MODE:
        raise BenchmarkValidationError(
            "raw-history model input requires a prospective immutable capture"
        )
    if manifest.get("price_basis") != PRICE_BASIS:
        raise BenchmarkValidationError("price_basis must be raw_unadjusted")
    if manifest.get("public_payload_policy") != _PUBLIC_PAYLOAD_POLICY:
        raise BenchmarkValidationError("raw-history Public payload boundary mismatch")
    if manifest.get("license_boundary") != _LICENSE_BOUNDARY:
        raise BenchmarkValidationError("raw-history license boundary mismatch")
    if manifest.get("symbol") != stock_code:
        raise BenchmarkValidationError("raw-history symbol does not match stock_code")
    if manifest.get("calendar_content_sha256") != history_window.calendar_content_sha256:
        raise BenchmarkValidationError("raw-history and history-window calendars differ")
    if manifest.get("market_data_at") != history_window.market_data_at.isoformat(
        timespec="seconds"
    ):
        raise BenchmarkValidationError("raw-history market_data_at mismatch")
    primary = manifest.get("primary_source")
    cross = manifest.get("cross_source")
    if not isinstance(primary, Mapping) or not isinstance(cross, Mapping):
        raise BenchmarkValidationError("raw-history dual-source evidence is required")
    if primary.get("content_sha256") != history_observation.content_sha256:
        raise BenchmarkValidationError("raw-history primary observation hash mismatch")
    if (
        primary.get("source_id") != PRIMARY_RAW_SOURCE_ID
        or history_observation.source_id != PRIMARY_RAW_SOURCE_ID
    ):
        raise BenchmarkValidationError("raw-history primary source mismatch")
    if primary.get("adjustment") != PRIMARY_ADJUSTMENT:
        raise BenchmarkValidationError("raw-history primary adjustment must be unadjusted")
    if primary.get("fetched_at") != history_observation.fetched_at.isoformat(
        timespec="seconds"
    ):
        raise BenchmarkValidationError("raw-history primary fetched_at mismatch")
    if (
        cross.get("source_id") != CROSS_RAW_SOURCE_ID
        or cross.get("adjustment") != CROSS_ADJUSTMENT
        or not cross.get("content_sha256")
    ):
        raise BenchmarkValidationError("raw-history cross-source evidence is required")


def _validated_bars(
    *,
    history_window: HistoryWindowContract,
    history_observation: RawHistoryObservation,
) -> Tuple[RawDailyBar, ...]:
    if history_observation.price_basis != PRICE_BASIS:
        raise BenchmarkValidationError("price_basis must be raw_unadjusted")
    dates = tuple(item.trade_date for item in history_observation.bars)
    if len(set(dates)) != len(dates):
        raise BenchmarkValidationError("raw-history dates cannot contain duplicates")
    if dates != tuple(sorted(dates)):
        raise BenchmarkValidationError("raw-history dates must be strictly increasing")
    if any(item > history_window.previous_completed_trade_date for item in dates):
        raise BenchmarkValidationError(
            "raw history contains an uncompleted or future trade date"
        )
    bars_by_date = {item.trade_date: item for item in history_observation.bars}
    missing = [item for item in history_window.required_trade_dates if item not in bars_by_date]
    if missing:
        raise BenchmarkValidationError("raw history is missing a required trade date")
    return tuple(bars_by_date[item] for item in history_window.required_trade_dates)


def _corporate_action_gate(
    *,
    history_window: HistoryWindowContract,
    corporate_action_reviewed: bool,
    corporate_action_source: str,
    corporate_action_data_as_of: datetime | str,
    corporate_actions: Sequence[Mapping[str, Any]],
) -> Tuple[bool, Optional[str], datetime, str]:
    source = str(corporate_action_source or "").strip()
    if not source:
        raise BenchmarkValidationError("corporate_action_source is required")
    action_time = _aware_datetime(
        corporate_action_data_as_of,
        field="corporate_action_data_as_of",
    )
    if not history_window.history_data_as_of <= action_time <= history_window.market_data_at:
        raise BenchmarkValidationError(
            "history_data_as_of <= corporate_action_data_as_of <= market_data_at is required"
        )
    if not corporate_action_reviewed:
        return True, "corporate_action_review_not_completed", action_time, source

    window_dates = set(history_window.required_trade_dates)
    for action in corporate_actions:
        raw_date = action.get("effective_date", action.get("trade_date"))
        if isinstance(raw_date, datetime):
            raise BenchmarkValidationError("corporate_action.effective_date must be a date")
        try:
            action_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            raise BenchmarkValidationError(
                "corporate_action.effective_date must be an ISO-8601 date"
            ) from None
        known_at = _aware_datetime(action.get("known_at"), field="corporate_action.known_at")
        if known_at > history_window.market_data_at:
            raise BenchmarkValidationError(
                "corporate action knowledge cannot be later than market_data_at"
            )
        action_type = str(action.get("action_type") or "").strip()
        if not action_type:
            raise BenchmarkValidationError("corporate_action.action_type is required")
        if action_date in window_dates:
            return True, "corporate_action_in_history_window", action_time, source
    return False, None, action_time, source


def _validate_window(
    *,
    model: BenchmarkModelIdentity,
    stock_code: str,
    history_window: HistoryWindowContract,
    history_observation: RawHistoryObservation,
    history_acceptance: RawHistoryAcceptance,
    corporate_action_reviewed: bool,
    corporate_action_source: str,
    corporate_action_data_as_of: datetime | str,
    corporate_actions: Sequence[Mapping[str, Any]],
    required_close_observations: int,
) -> _ValidatedWindow:
    code = _stock_code(stock_code)
    if model.generated_at != history_window.generated_at:
        raise BenchmarkValidationError("model and history window generated_at must match")
    if history_window.market_data_at.date() == history_window.previous_completed_trade_date:
        raise BenchmarkValidationError("Short-term v1 signals may use history only through T-1")
    if len(history_window.required_trade_dates) < required_close_observations:
        return _ValidatedWindow(
            code,
            (),
            (),
            tuple(history_window.required_trade_dates),
            False,
            f"required_{required_close_observations}_trade_dates_not_available",
            history_window.history_data_as_of,
            str(corporate_action_source or "").strip(),
        )
    if len(history_window.required_trade_dates) != required_close_observations:
        raise BenchmarkValidationError(
            "history window must contain exactly "
            f"{required_close_observations} completed trade dates"
        )
    if history_observation.symbol != code:
        raise BenchmarkValidationError("history observation symbol does not match stock_code")
    bars = _validated_bars(
        history_window=history_window,
        history_observation=history_observation,
    )
    _validate_raw_history_acceptance(
        stock_code=code,
        history_window=history_window,
        history_observation=history_observation,
        history_acceptance=history_acceptance,
    )
    review, reason, action_time, action_source = _corporate_action_gate(
        history_window=history_window,
        corporate_action_reviewed=corporate_action_reviewed,
        corporate_action_source=corporate_action_source,
        corporate_action_data_as_of=corporate_action_data_as_of,
        corporate_actions=corporate_actions,
    )
    if any(not item.is_trading for item in bars):
        review = True
        reason = "non_trading_bar_in_history_window"
    closes = tuple(_finite_number(item.close, field="close", positive=True) for item in bars)
    volumes = tuple(_finite_number(item.volume, field="volume") for item in bars)
    if any(item < 0 for item in volumes):
        raise BenchmarkValidationError("volume must be non-negative")
    return _ValidatedWindow(
        code,
        closes,
        volumes,
        tuple(item.trade_date for item in bars),
        review,
        reason,
        action_time,
        action_source,
    )


def period_return(closes: Sequence[float], sessions: int) -> float:
    """Return the simple close-to-close return over exactly ``sessions`` gaps."""

    if not isinstance(sessions, int) or isinstance(sessions, bool) or sessions < 1:
        raise BenchmarkValidationError("sessions must be a positive integer")
    normalized = tuple(_finite_number(item, field="close", positive=True) for item in closes)
    if len(normalized) < sessions + 1:
        raise BenchmarkValidationError("insufficient closes for period return")
    return normalized[-1] / normalized[-sessions - 1] - 1.0


def vol_contraction_10_60(closes: Sequence[float]) -> float:
    """Sample std(last 10 daily returns) / sample std(last 60 returns)."""

    normalized = tuple(_finite_number(item, field="close", positive=True) for item in closes)
    if len(normalized) < ABLATION_REQUIRED_CLOSE_OBSERVATIONS:
        raise BenchmarkValidationError("vol_contraction_10_60 requires 61 closes")
    returns = tuple(
        normalized[index] / normalized[index - 1] - 1.0
        for index in range(1, len(normalized))
    )[-ABLATION_REQUIRED_RETURN_OBSERVATIONS:]
    denominator = stdev(returns)
    if denominator == 0:
        raise BenchmarkValidationError("vol_contraction_10_60 denominator is zero")
    return stdev(returns[-VOLATILITY_SHORT_SESSIONS:]) / denominator


def breakout_strength_20(closes: Sequence[float]) -> float:
    """Latest close / maximum of the prior 20 closes - 1."""

    normalized = tuple(_finite_number(item, field="close", positive=True) for item in closes)
    if len(normalized) < BREAKOUT_LOOKBACK_SESSIONS + 1:
        raise BenchmarkValidationError("breakout_strength_20 requires 21 closes")
    prior = normalized[-BREAKOUT_LOOKBACK_SESSIONS - 1 : -1]
    return normalized[-1] / max(prior) - 1.0


def volume_ratio_5(volumes: Sequence[float]) -> float:
    """Latest volume / arithmetic mean of the prior five volumes."""

    normalized = tuple(_finite_number(item, field="volume") for item in volumes)
    if len(normalized) < VOLUME_LOOKBACK_SESSIONS + 1:
        raise BenchmarkValidationError("volume_ratio_5 requires six volumes")
    if any(item < 0 for item in normalized):
        raise BenchmarkValidationError("volume must be non-negative")
    prior = normalized[-VOLUME_LOOKBACK_SESSIONS - 1 : -1]
    denominator = sum(prior) / VOLUME_LOOKBACK_SESSIONS
    if denominator <= 0:
        raise BenchmarkValidationError("volume_ratio_5 denominator must be positive")
    return normalized[-1] / denominator


def evaluate_history(
    *,
    model: BenchmarkModelIdentity,
    stock_code: str,
    history_window: HistoryWindowContract,
    history_observation: RawHistoryObservation,
    history_acceptance: RawHistoryAcceptance,
    corporate_action_reviewed: bool,
    corporate_action_source: str,
    corporate_action_data_as_of: datetime | str,
    corporate_actions: Sequence[Mapping[str, Any]],
) -> ShortTermResult:
    """Validate one exact T-1 window and apply only the frozen main rule."""

    window = _validate_window(
        model=model,
        stock_code=stock_code,
        history_window=history_window,
        history_observation=history_observation,
        history_acceptance=history_acceptance,
        corporate_action_reviewed=corporate_action_reviewed,
        corporate_action_source=corporate_action_source,
        corporate_action_data_as_of=corporate_action_data_as_of,
        corporate_actions=corporate_actions,
        required_close_observations=REQUIRED_CLOSE_OBSERVATIONS,
    )
    if window.corporate_action_reason == "required_21_trade_dates_not_available":
        return ShortTermResult(
            window.stock_code,
            ShortTermStatus.INSUFFICIENT_HISTORY,
            (window.corporate_action_reason,),
            None,
        )
    if window.corporate_action_review:
        return ShortTermResult(
            window.stock_code,
            ShortTermStatus.CORPORATE_ACTION_REVIEW,
            (window.corporate_action_reason or "corporate_action_review_required",),
            None,
        )

    ret_20 = period_return(window.closes, TREND_LOOKBACK_SESSIONS)
    ret_5 = period_return(window.closes, RANK_LOOKBACK_SESSIONS)
    raw_metric = {
        "close_observations": len(window.closes),
        "corporate_action_data_as_of": window.corporate_action_data_as_of.isoformat(
            timespec="seconds"
        ),
        "corporate_action_source": window.corporate_action_source,
        "corporate_action_status": "reviewed_clear",
        "history_window_content_sha256": history_window.content_sha256,
        "price_basis": PRICE_BASIS,
        "raw_history_manifest_sha256": history_acceptance.manifest["manifest_sha256"],
        "ret_20": ret_20,
        "ret_5": ret_5,
        "universe_config_hash": model.parameters["universe_config_hash"],
        "universe_contract_version": model.parameters["universe_contract_version"],
        "window_end": window.dates[-1].isoformat(),
        "window_start": window.dates[0].isoformat(),
    }
    if ret_20 <= 0:
        return ShortTermResult(
            window.stock_code,
            ShortTermStatus.TREND_FILTERED,
            ("ret_20_not_positive",),
            raw_metric,
        )
    return ShortTermResult(window.stock_code, ShortTermStatus.ELIGIBLE, (), raw_metric)


def evaluate_ablation_factors(
    *,
    model: BenchmarkModelIdentity,
    stock_code: str,
    history_window: HistoryWindowContract,
    history_observation: RawHistoryObservation,
    history_acceptance: RawHistoryAcceptance,
    corporate_action_reviewed: bool,
    corporate_action_source: str,
    corporate_action_data_as_of: datetime | str,
    corporate_actions: Sequence[Mapping[str, Any]],
) -> Tuple[AblationFactorResult, ...]:
    """Evaluate three independent factors without changing the main score or rank."""

    window = _validate_window(
        model=model,
        stock_code=stock_code,
        history_window=history_window,
        history_observation=history_observation,
        history_acceptance=history_acceptance,
        corporate_action_reviewed=corporate_action_reviewed,
        corporate_action_source=corporate_action_source,
        corporate_action_data_as_of=corporate_action_data_as_of,
        corporate_actions=corporate_actions,
        required_close_observations=ABLATION_REQUIRED_CLOSE_OBSERVATIONS,
    )
    names = (
        "vol_contraction_10_60",
        "breakout_strength_20",
        "volume_ratio_5",
    )
    if window.corporate_action_review or not window.closes:
        reason = window.corporate_action_reason or "corporate_action_review_required"
        status = (
            AblationFactorStatus.CORPORATE_ACTION_REVIEW
            if window.corporate_action_review
            else AblationFactorStatus.UNDEFINED
        )
        return tuple(
            AblationFactorResult(window.stock_code, name, status, None, reason)
            for name in names
        )

    calculators = (
        (names[0], lambda: vol_contraction_10_60(window.closes)),
        (names[1], lambda: breakout_strength_20(window.closes)),
        (names[2], lambda: volume_ratio_5(window.volumes)),
    )
    output = []
    for name, calculate in calculators:
        try:
            value = calculate()
        except BenchmarkValidationError as exc:
            output.append(
                AblationFactorResult(
                    window.stock_code,
                    name,
                    AblationFactorStatus.UNDEFINED,
                    None,
                    str(exc),
                )
            )
        else:
            output.append(
                AblationFactorResult(
                    window.stock_code,
                    name,
                    AblationFactorStatus.ELIGIBLE,
                    value,
                    None,
                )
            )
    return tuple(output)


def rank_eligible(results: Sequence[ShortTermResult]) -> Sequence[ShortTermResult]:
    """Rank positive-trend candidates by ret_5 descending, then stock code."""

    eligible = [
        item
        for item in results
        if item.status is ShortTermStatus.ELIGIBLE and item.raw_metric
    ]
    return sorted(
        eligible,
        key=lambda item: (-item.raw_metric["ret_5"], item.stock_code),
    )


def create_signals(
    *,
    model: BenchmarkModelIdentity,
    ranked_results: Sequence[ShortTermResult],
    signal_date: date | str,
    market_data_at: datetime | str,
    source_data_as_of: datetime | str,
    references: Mapping[str, Mapping[str, Any]],
    fetched_at: datetime | str | None = None,
) -> Tuple[BenchmarkSignal, ...]:
    """Create Top-N BenchmarkSignals using caller-supplied unified references."""

    reranked = tuple(rank_eligible(ranked_results))
    if tuple(item.stock_code for item in ranked_results) != tuple(
        item.stock_code for item in reranked
    ):
        raise BenchmarkValidationError("ranked_results must contain the complete stable ranking")
    selected = reranked[: int(model.parameters["top_n"])]
    signals = []
    for rank, result in enumerate(selected, start=1):
        reference = references.get(result.stock_code)
        if not isinstance(reference, Mapping):
            raise BenchmarkValidationError("unified reference is required for every selected stock")
        signals.append(
            BenchmarkSignal.create(
                model=model,
                stock_code=result.stock_code,
                stock_name=reference.get("stock_name"),
                signal_date=signal_date,
                market_data_at=market_data_at,
                reference_price=reference.get("reference_price"),
                rank=rank,
                score=None,
                raw_metric=result.raw_metric,
                selection_reason="ret_20 > 0; ranked by ret_5 descending",
                source_data_as_of=source_data_as_of,
                fetched_at=fetched_at,
            )
        )
    return tuple(signals)


def outcome_handoff(signals: Sequence[BenchmarkSignal]) -> Tuple[Mapping[str, Any], ...]:
    """Expose the existing five-field handoff for 1/3/5/10/20-day research."""

    return tuple(signal.to_outcome_signal_core() for signal in signals)
