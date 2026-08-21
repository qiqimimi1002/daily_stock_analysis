"""Fair, offline evaluation of aligned public Benchmark signals.

The module consumes caller-supplied private evidence and emits aggregates only.
It never fetches data, changes either frozen model, or serializes raw prices.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import math
import re
from statistics import mean, median
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from research.benchmarks.low_volatility import (
    BENCHMARK_TOP_N,
    MODEL_NAME as LOW_VOLATILITY_MODEL,
)
from research.benchmarks.schema import SHANGHAI_TZ, canonical_json_bytes
from research.benchmarks.short_term import MODEL_NAME as SHORT_TERM_MODEL
from src.core.backtest_engine import BacktestEngine, EvaluationConfig


SCHEMA_VERSION = "unified-benchmark-race-v1"
ENGINE_VERSION = "unified-benchmark-outcome-v1"
SUPPORTED_HORIZONS = {"1d": 1, "3d": 3, "5d": 5, "10d": 10}
PENDING_HORIZON = "20d"
ROUND_TRIP_COST_BPS = 30
ROUND_TRIP_COST_PCT = ROUND_TRIP_COST_BPS / 100.0
MIN_COMMON_DATES_FOR_EVIDENCE = 20
MODEL_NAMES = (SHORT_TERM_MODEL, LOW_VOLATILITY_MODEL)
FACTOR_NAMES = (
    "vol_contraction_10_60",
    "breakout_strength_20",
    "volume_ratio_5",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STOCK_CODE_RE = re.compile(r"^[0-9]{6}$")


class UnifiedRaceError(ValueError):
    """Raised when aligned evaluation evidence is ambiguous or unfair."""


@dataclass(frozen=True)
class ForwardBar:
    date: date
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class _Signal:
    model_name: str
    stock_code: str
    signal_date: date
    market_data_at: datetime
    source_data_as_of: datetime
    reference_price: float
    rank: int
    raw_metric: Mapping[str, Any]
    universe_contract_version: str
    universe_config_hash: str


@dataclass(frozen=True)
class _Outcome:
    model_name: str
    stock_code: str
    signal_date: date
    horizon: str
    gross_return_pct: float
    net_return_pct: float
    mfe_pct: float
    mae_pct: float
    hs300_excess_return_pct: float


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        raise UnifiedRaceError(f"{field} must be a date")
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise UnifiedRaceError(f"{field} must be canonical YYYY-MM-DD") from None
    if text != parsed.isoformat():
        raise UnifiedRaceError(f"{field} must be canonical YYYY-MM-DD")
    return parsed


def _time(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            raise UnifiedRaceError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UnifiedRaceError(f"{field} must include a timezone")
    return parsed.astimezone(SHANGHAI_TZ)


def _finite(value: Any, *, field: str, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise UnifiedRaceError(f"{field} must be finite") from None
    if not math.isfinite(number):
        raise UnifiedRaceError(f"{field} must be finite")
    if positive and number <= 0:
        raise UnifiedRaceError(f"{field} must be positive")
    return number


def _code(value: Any, *, field: str = "stock_code") -> str:
    code = str(value or "").strip()
    if not _STOCK_CODE_RE.fullmatch(code):
        raise UnifiedRaceError(f"{field} must contain exactly six digits")
    return code


def _sha256(value: Any, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(text):
        raise UnifiedRaceError(f"{field} must be a lowercase SHA-256")
    return text


def _codes(values: Any, *, field: str) -> Tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise UnifiedRaceError(f"{field} must be a sequence")
    normalized = tuple(_code(value, field=field) for value in values)
    if len(set(normalized)) != len(normalized):
        raise UnifiedRaceError(f"{field} cannot contain duplicates")
    return tuple(sorted(normalized))


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UnifiedRaceError(f"{field} must be an object")
    return value


def _parse_signal(
    value: Any,
    *,
    expected_model: str,
    signal_date: date,
    market_data_at: datetime,
    source_data_as_of: datetime,
    previous_completed_trade_date: date,
    universe_version: str,
    universe_hash: str,
    reference_prices: Mapping[str, float],
) -> _Signal:
    item = _mapping(value, field="signal")
    if str(item.get("model_name") or "") != expected_model:
        raise UnifiedRaceError("signal model_name does not match its model bucket")
    local_signal_date = _date(item.get("signal_date"), field="signal.signal_date")
    local_market_time = _time(item.get("market_data_at"), field="signal.market_data_at")
    local_source_time = _time(
        item.get("source_data_as_of"), field="signal.source_data_as_of"
    )
    if (
        local_signal_date != signal_date
        or local_market_time != market_data_at
        or local_source_time != source_data_as_of
    ):
        raise UnifiedRaceError("both models must use the same signal and data timestamps")
    if local_source_time > local_market_time:
        raise UnifiedRaceError("signal source_data_as_of cannot follow market_data_at")
    code = _code(item.get("stock_code"))
    if code not in reference_prices:
        raise UnifiedRaceError("selected stock is missing from the shared reference snapshot")
    reference_price = _finite(
        item.get("reference_price"), field="signal.reference_price", positive=True
    )
    if reference_price != reference_prices[code]:
        raise UnifiedRaceError("signal reference price differs from the shared snapshot")
    try:
        rank = int(item.get("rank"))
    except (TypeError, ValueError, OverflowError):
        raise UnifiedRaceError("signal.rank must be an integer") from None
    parameters = _mapping(item.get("parameters"), field="signal.parameters")
    if int(parameters.get("top_n", 0)) != BENCHMARK_TOP_N:
        raise UnifiedRaceError("both models must retain the frozen Top-N")
    raw_metric = _mapping(item.get("raw_metric"), field="signal.raw_metric")
    window_end = _date(raw_metric.get("window_end"), field="signal.raw_metric.window_end")
    if window_end != previous_completed_trade_date:
        raise UnifiedRaceError(
            "both models must end their history window on the shared T-1 cutoff"
        )
    metric_version = str(raw_metric.get("universe_contract_version") or "")
    metric_hash = str(raw_metric.get("universe_config_hash") or "")
    if metric_version != universe_version or metric_hash != universe_hash:
        raise UnifiedRaceError("signal Universe evidence differs from the shared Universe")
    return _Signal(
        model_name=expected_model,
        stock_code=code,
        signal_date=local_signal_date,
        market_data_at=local_market_time,
        source_data_as_of=local_source_time,
        reference_price=reference_price,
        rank=rank,
        raw_metric=raw_metric,
        universe_contract_version=metric_version,
        universe_config_hash=metric_hash,
    )


def _parse_signals(
    values: Any,
    *,
    expected_model: str,
    signal_date: date,
    market_data_at: datetime,
    source_data_as_of: datetime,
    previous_completed_trade_date: date,
    universe_version: str,
    universe_hash: str,
    reference_prices: Mapping[str, float],
) -> Tuple[_Signal, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise UnifiedRaceError("model signals must be a sequence")
    parsed = tuple(
        _parse_signal(
            value,
            expected_model=expected_model,
            signal_date=signal_date,
            market_data_at=market_data_at,
            source_data_as_of=source_data_as_of,
            previous_completed_trade_date=previous_completed_trade_date,
            universe_version=universe_version,
            universe_hash=universe_hash,
            reference_prices=reference_prices,
        )
        for value in values
    )
    ordered = tuple(sorted(parsed, key=lambda signal: (signal.rank, signal.stock_code)))
    if len({signal.stock_code for signal in ordered}) != len(ordered):
        raise UnifiedRaceError("model signals cannot contain duplicate stocks")
    if tuple(signal.rank for signal in ordered) != tuple(range(1, len(ordered) + 1)):
        raise UnifiedRaceError("model signal ranks must be consecutive from one")
    if expected_model == SHORT_TERM_MODEL:
        if any(_finite(item.raw_metric.get("ret_20"), field="ret_20") <= 0 for item in ordered):
            raise UnifiedRaceError("Short-term signal violates the frozen ret_20 gate")
        expected = tuple(
            sorted(
                ordered,
                key=lambda item: (
                    -_finite(item.raw_metric.get("ret_5"), field="ret_5"),
                    item.stock_code,
                ),
            )
        )
    else:
        expected = tuple(
            sorted(
                ordered,
                key=lambda item: (
                    _finite(
                        item.raw_metric.get("volatility_daily_60d"),
                        field="volatility_daily_60d",
                    ),
                    item.stock_code,
                ),
            )
        )
    if tuple(item.stock_code for item in ordered) != tuple(
        item.stock_code for item in expected
    ):
        raise UnifiedRaceError("model ranks violate the frozen metric and tie-break order")
    return ordered


def _parse_reference_prices(value: Any) -> Dict[str, float]:
    source = _mapping(value, field="reference_prices")
    return {
        _code(key, field="reference_prices key"): _finite(
            price, field=f"reference_prices.{key}", positive=True
        )
        for key, price in source.items()
    }


def _parse_forward_dates(value: Any, *, signal_date: date) -> Tuple[date, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise UnifiedRaceError("forward_trade_dates must be a sequence")
    dates = tuple(_date(item, field="forward_trade_dates") for item in value)
    if dates != tuple(sorted(set(dates))) or any(item <= signal_date for item in dates):
        raise UnifiedRaceError(
            "forward_trade_dates must be unique, increasing and strictly after signal_date"
        )
    if len(dates) > max(SUPPORTED_HORIZONS.values()):
        raise UnifiedRaceError("forward_trade_dates cannot exceed the supported horizon")
    return dates


def _parse_bars(
    values: Any,
    *,
    expected_dates: Sequence[date],
    field: str,
) -> Tuple[ForwardBar, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise UnifiedRaceError(f"{field} must be a sequence")
    bars = []
    for index, value in enumerate(values):
        item = _mapping(value, field=f"{field}[{index}]")
        bar_date = _date(item.get("trade_date"), field=f"{field}.trade_date")
        high = _finite(item.get("high"), field=f"{field}.high", positive=True)
        low = _finite(item.get("low"), field=f"{field}.low", positive=True)
        close = _finite(item.get("close"), field=f"{field}.close", positive=True)
        if not low <= close <= high:
            raise UnifiedRaceError(f"{field} low/close/high bounds are invalid")
        bars.append(ForwardBar(bar_date, high, low, close))
    dates = tuple(item.date for item in bars)
    if dates != tuple(expected_dates[: len(dates)]):
        raise UnifiedRaceError(f"{field} must be an exact prefix of forward_trade_dates")
    return tuple(bars)


def _evidence_reasons(value: Any) -> Tuple[str, ...]:
    evidence = _mapping(value, field="evidence")
    checks = (
        (evidence.get("calendar_consistency_status") == "pass", "calendar_not_verified"),
        (evidence.get("price_basis") == "raw_unadjusted", "price_basis_not_raw_unadjusted"),
        (evidence.get("acquisition_mode") == "prospective_cutoff", "not_prospective_cutoff"),
        (evidence.get("private_archive") is True, "private_archive_not_proven"),
        (evidence.get("immutable_archive") is True, "immutable_archive_not_proven"),
        (
            evidence.get("raw_history_acceptance_status") == "conditional_pass",
            "raw_history_acceptance_missing",
        ),
        (
            evidence.get("corporate_action_review_status") == "reviewed_clear",
            "corporate_action_review_not_clear",
        ),
        (
            evidence.get("public_payload_policy")
            == "metadata_and_hashes_only_no_raw_rows",
            "public_payload_policy_mismatch",
        ),
    )
    for field in (
        "calendar_content_sha256",
        "raw_history_manifest_sha256",
        "corporate_action_manifest_sha256",
        "reference_snapshot_sha256",
    ):
        try:
            _sha256(evidence.get(field), field=f"evidence.{field}")
        except UnifiedRaceError:
            checks += ((False, f"{field}_invalid"),)
    return tuple(reason for passed, reason in checks if not passed)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 8)


def _max_drawdown(date_returns_pct: Iterable[float]) -> float | None:
    values = tuple(date_returns_pct)
    if not values:
        return None
    equity = 1.0
    peak = 1.0
    maximum = 0.0
    for value in values:
        equity *= 1.0 + value / 100.0
        peak = max(peak, equity)
        if peak > 0:
            maximum = max(maximum, (peak - equity) / peak * 100.0)
    return maximum


def _aggregate(rows: Sequence[_Outcome]) -> Dict[str, Any]:
    ordered = tuple(sorted(rows, key=lambda row: (row.signal_date, row.stock_code)))
    if not ordered:
        return {
            "sample_count": 0,
            "signal_date_count": 0,
            "gross_return_mean_pct": None,
            "gross_return_median_pct": None,
            "net_return_mean_pct": None,
            "net_return_median_pct": None,
            "net_win_rate_pct": None,
            "mfe_mean_pct": None,
            "mae_mean_pct": None,
            "max_drawdown_pct": None,
            "hs300_excess_mean_pct": None,
            "hs300_excess_median_pct": None,
            "hs300_excess_win_rate_pct": None,
            "date_stability": {
                "positive_date_count": 0,
                "negative_date_count": 0,
                "flat_date_count": 0,
                "positive_date_rate_pct": None,
            },
            "per_date": [],
        }
    by_date: Dict[date, list[_Outcome]] = defaultdict(list)
    for row in ordered:
        by_date[row.signal_date].append(row)
    per_date = []
    for signal_date in sorted(by_date):
        bucket = by_date[signal_date]
        per_date.append(
            {
                "signal_date": signal_date.isoformat(),
                "sample_count": len(bucket),
                "gross_return_mean_pct": _rounded(mean(item.gross_return_pct for item in bucket)),
                "net_return_mean_pct": _rounded(mean(item.net_return_pct for item in bucket)),
                "hs300_excess_mean_pct": _rounded(
                    mean(item.hs300_excess_return_pct for item in bucket)
                ),
            }
        )
    date_returns = [float(item["net_return_mean_pct"]) for item in per_date]
    positive_dates = sum(value > 0 for value in date_returns)
    negative_dates = sum(value < 0 for value in date_returns)
    flat_dates = len(date_returns) - positive_dates - negative_dates
    return {
        "sample_count": len(ordered),
        "signal_date_count": len(per_date),
        "gross_return_mean_pct": _rounded(mean(item.gross_return_pct for item in ordered)),
        "gross_return_median_pct": _rounded(median(item.gross_return_pct for item in ordered)),
        "net_return_mean_pct": _rounded(mean(item.net_return_pct for item in ordered)),
        "net_return_median_pct": _rounded(median(item.net_return_pct for item in ordered)),
        "net_win_rate_pct": _rounded(
            sum(item.net_return_pct > 0 for item in ordered) / len(ordered) * 100.0
        ),
        "mfe_mean_pct": _rounded(mean(item.mfe_pct for item in ordered)),
        "mae_mean_pct": _rounded(mean(item.mae_pct for item in ordered)),
        "max_drawdown_pct": _rounded(_max_drawdown(date_returns)),
        "hs300_excess_mean_pct": _rounded(
            mean(item.hs300_excess_return_pct for item in ordered)
        ),
        "hs300_excess_median_pct": _rounded(
            median(item.hs300_excess_return_pct for item in ordered)
        ),
        "hs300_excess_win_rate_pct": _rounded(
            sum(item.hs300_excess_return_pct > 0 for item in ordered)
            / len(ordered)
            * 100.0
        ),
        "date_stability": {
            "positive_date_count": positive_dates,
            "negative_date_count": negative_dates,
            "flat_date_count": flat_dates,
            "positive_date_rate_pct": _rounded(positive_dates / len(per_date) * 100.0),
        },
        "per_date": per_date,
    }


def _average_ranks(values: Sequence[float]) -> Tuple[float, ...]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average_rank = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            ranks[indexed[position][0]] = average_rank
        cursor = end
    return tuple(ranks)


def _spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    x = _average_ranks(left)
    y = _average_ranks(right)
    x_mean = mean(x)
    y_mean = mean(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
    denominator = math.sqrt(
        sum((a - x_mean) ** 2 for a in x) * sum((b - y_mean) ** 2 for b in y)
    )
    return None if denominator == 0 else numerator / denominator


def _factor_summary(
    rows: Sequence[Tuple[date, str, float, float]],
) -> Dict[str, Any]:
    if not rows:
        return {
            "sample_count": 0,
            "signal_date_count": 0,
            "global_spearman_ic": None,
            "per_date_ic_mean": None,
            "per_date_ic_median": None,
            "positive_date_ic_rate_pct": None,
        }
    global_ic = _spearman([row[2] for row in rows], [row[3] for row in rows])
    by_date: Dict[date, list[Tuple[date, str, float, float]]] = defaultdict(list)
    for row in rows:
        by_date[row[0]].append(row)
    date_ics = []
    for signal_date in sorted(by_date):
        bucket = by_date[signal_date]
        ic = _spearman([row[2] for row in bucket], [row[3] for row in bucket])
        if ic is not None:
            date_ics.append(ic)
    return {
        "sample_count": len(rows),
        "signal_date_count": len(by_date),
        "global_spearman_ic": _rounded(global_ic),
        "per_date_ic_mean": _rounded(mean(date_ics)) if date_ics else None,
        "per_date_ic_median": _rounded(median(date_ics)) if date_ics else None,
        "positive_date_ic_rate_pct": (
            _rounded(sum(value > 0 for value in date_ics) / len(date_ics) * 100.0)
            if date_ics
            else None
        ),
    }


def _inventory(value: Any) -> Dict[str, Any]:
    inventory = _mapping(value or {}, field="inventory")
    try:
        prospective_count = int(
            inventory.get("prospective_private_immutable_batch_count", 0)
        )
    except (TypeError, ValueError, OverflowError):
        raise UnifiedRaceError("inventory prospective count must be an integer") from None
    if prospective_count < 0:
        raise UnifiedRaceError("inventory prospective count cannot be negative")
    excluded = inventory.get("excluded_evidence", [])
    if not isinstance(excluded, Sequence) or isinstance(excluded, (str, bytes)):
        raise UnifiedRaceError("inventory.excluded_evidence must be a sequence")
    reasons: Counter[str] = Counter()
    sample_count = 0
    for value in excluded:
        item = _mapping(value, field="inventory.excluded_evidence")
        try:
            sample_count += int(item.get("sample_count", 0))
        except (TypeError, ValueError, OverflowError):
            raise UnifiedRaceError("excluded evidence sample_count must be an integer") from None
        reason_codes = item.get("reason_codes", [])
        if not isinstance(reason_codes, Sequence) or isinstance(reason_codes, (str, bytes)):
            raise UnifiedRaceError("excluded evidence reason_codes must be a sequence")
        for reason in reason_codes:
            text = str(reason or "").strip()
            if not text:
                raise UnifiedRaceError("excluded evidence reason code is required")
            reasons[text] += 1
        content_hash = item.get("content_sha256")
        if content_hash is not None:
            _sha256(content_hash, field="excluded evidence content_sha256")
    return {
        "prospective_private_immutable_batch_count": prospective_count,
        "excluded_evidence_record_count": len(excluded),
        "excluded_evidence_sample_count": sample_count,
        "excluded_evidence_reason_counts": dict(sorted(reasons.items())),
    }


def evaluate_unified_race(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Evaluate aligned batches and return a raw-row-free deterministic summary."""

    source = _mapping(payload, field="payload")
    if source.get("schema_version") != SCHEMA_VERSION:
        raise UnifiedRaceError(f"schema_version must be {SCHEMA_VERSION}")
    base_sha = str(source.get("base_sha") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise UnifiedRaceError("base_sha must be a lowercase Git commit SHA")
    batches = source.get("batches", [])
    if not isinstance(batches, Sequence) or isinstance(batches, (str, bytes)):
        raise UnifiedRaceError("batches must be a sequence")
    input_hash = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    inventory = _inventory(source.get("inventory"))
    outcomes: Dict[Tuple[str, str], list[_Outcome]] = defaultdict(list)
    factor_rows: Dict[Tuple[str, str], list[Tuple[date, str, float, float]]] = defaultdict(list)
    exclusions: Counter[str] = Counter()
    exclusion_records = []
    candidate_counts = Counter({model: 0 for model in MODEL_NAMES})
    seen_dates = set()
    evaluated_dates = set()

    for batch_value in sorted(
        batches,
        key=lambda item: str(_mapping(item, field="batch").get("signal_date") or ""),
    ):
        batch = _mapping(batch_value, field="batch")
        signal_date = _date(batch.get("signal_date"), field="batch.signal_date")
        if signal_date in seen_dates:
            raise UnifiedRaceError("batches cannot repeat a signal_date")
        seen_dates.add(signal_date)
        market_data_at = _time(batch.get("market_data_at"), field="batch.market_data_at")
        source_data_as_of = _time(
            batch.get("source_data_as_of"), field="batch.source_data_as_of"
        )
        if market_data_at.date() != signal_date or source_data_as_of > market_data_at:
            raise UnifiedRaceError("batch timestamps violate signal-date no-lookahead")
        previous_completed_trade_date = _date(
            batch.get("previous_completed_trade_date"),
            field="batch.previous_completed_trade_date",
        )
        if previous_completed_trade_date >= signal_date:
            raise UnifiedRaceError(
                "previous_completed_trade_date must be strictly earlier than signal_date"
            )
        universe_version = str(batch.get("universe_contract_version") or "").strip()
        universe_hash = _sha256(
            batch.get("universe_config_hash"), field="batch.universe_config_hash"
        )
        shared_universe = _codes(batch.get("shared_universe"), field="shared_universe")
        model_universes = _mapping(batch.get("model_universes"), field="model_universes")
        for model in MODEL_NAMES:
            if _codes(model_universes.get(model), field=f"model_universes.{model}") != shared_universe:
                raise UnifiedRaceError("both models must use the exact shared Universe")
        reference_prices = _parse_reference_prices(batch.get("reference_prices"))
        if any(code not in reference_prices for code in shared_universe):
            raise UnifiedRaceError("every shared-Universe stock needs one reference price")
        signal_buckets = _mapping(batch.get("signals"), field="signals")
        parsed_signals = {
            model: _parse_signals(
                signal_buckets.get(model),
                expected_model=model,
                signal_date=signal_date,
                market_data_at=market_data_at,
                source_data_as_of=source_data_as_of,
                previous_completed_trade_date=previous_completed_trade_date,
                universe_version=universe_version,
                universe_hash=universe_hash,
                reference_prices=reference_prices,
            )
            for model in MODEL_NAMES
        }
        for model, signals in parsed_signals.items():
            candidate_counts[model] += len(signals)
            if any(signal.stock_code not in shared_universe for signal in signals):
                raise UnifiedRaceError("selected signal is outside the shared Universe")
        evidence_reasons = list(_evidence_reasons(batch.get("evidence")))
        for model, signals in parsed_signals.items():
            if len(signals) != BENCHMARK_TOP_N:
                evidence_reasons.append(f"{model}_top_n_unavailable")
        if evidence_reasons:
            for reason in sorted(set(evidence_reasons)):
                exclusions[reason] += 1
            exclusion_records.append(
                {
                    "signal_date": signal_date.isoformat(),
                    "horizons": list(SUPPORTED_HORIZONS),
                    "reason_codes": sorted(set(evidence_reasons)),
                }
            )
            continue

        forward_dates = _parse_forward_dates(
            batch.get("forward_trade_dates"), signal_date=signal_date
        )
        raw_forward = _mapping(batch.get("forward_bars"), field="forward_bars")
        parsed_forward = {
            _code(code, field="forward_bars key"): _parse_bars(
                values, expected_dates=forward_dates, field=f"forward_bars.{code}"
            )
            for code, values in raw_forward.items()
        }
        hs300 = _mapping(batch.get("hs300"), field="hs300")
        hs300_reference = _finite(
            hs300.get("reference_price"), field="hs300.reference_price", positive=True
        )
        hs300_bars = _parse_bars(
            hs300.get("forward_bars"),
            expected_dates=forward_dates,
            field="hs300.forward_bars",
        )
        factor_values = _mapping(batch.get("factor_values", {}), field="factor_values")
        normalized_factors: Dict[str, Dict[str, float]] = {}
        for code, values in factor_values.items():
            stock_code = _code(code, field="factor_values key")
            if stock_code not in shared_universe:
                raise UnifiedRaceError("factor observation is outside the shared Universe")
            factor_map = _mapping(values, field=f"factor_values.{stock_code}")
            normalized_factors[stock_code] = {
                name: _finite(factor_map.get(name), field=f"factor_values.{stock_code}.{name}")
                for name in FACTOR_NAMES
            }

        selected_codes = sorted(
            {
                signal.stock_code
                for model in MODEL_NAMES
                for signal in parsed_signals[model]
            }
        )
        for horizon, days in SUPPORTED_HORIZONS.items():
            missing = [
                code for code in selected_codes if len(parsed_forward.get(code, ())) < days
            ]
            if len(hs300_bars) < days:
                missing.append("hs300")
            if len(forward_dates) < days:
                missing.append("forward_calendar")
            if missing:
                reason = "incomplete_shared_forward_window"
                exclusions[reason] += 1
                exclusion_records.append(
                    {
                        "signal_date": signal_date.isoformat(),
                        "horizons": [horizon],
                        "reason_codes": [reason],
                        "missing_count": len(missing),
                    }
                )
                continue
            evaluated_dates.add(signal_date)
            hs300_return = (
                hs300_bars[days - 1].close / hs300_reference - 1.0
            ) * 100.0
            for model in MODEL_NAMES:
                for signal in parsed_signals[model]:
                    evaluation = BacktestEngine.evaluate_decision_signal(
                        direction_expected="up",
                        anchor_date=signal_date,
                        start_price=signal.reference_price,
                        forward_bars=parsed_forward[signal.stock_code],
                        config=EvaluationConfig(
                            eval_window_days=days,
                            neutral_band_pct=0.0,
                            engine_version=ENGINE_VERSION,
                        ),
                    )
                    if evaluation.get("eval_status") != "completed":
                        raise UnifiedRaceError("validated forward window did not complete")
                    gross = _finite(
                        evaluation.get("stock_return_pct"), field="stock_return_pct"
                    )
                    max_high = _finite(evaluation.get("max_high"), field="max_high", positive=True)
                    min_low = _finite(evaluation.get("min_low"), field="min_low", positive=True)
                    net = gross - ROUND_TRIP_COST_PCT
                    outcomes[(model, horizon)].append(
                        _Outcome(
                            model,
                            signal.stock_code,
                            signal_date,
                            horizon,
                            gross,
                            net,
                            max(0.0, (max_high / signal.reference_price - 1.0) * 100.0),
                            max(0.0, (signal.reference_price - min_low) / signal.reference_price * 100.0),
                            net - hs300_return,
                        )
                    )
            for stock_code, values in normalized_factors.items():
                bars = parsed_forward.get(stock_code, ())
                if len(bars) < days:
                    continue
                net = (bars[days - 1].close / reference_prices[stock_code] - 1.0) * 100.0
                net -= ROUND_TRIP_COST_PCT
                for name, factor_value in values.items():
                    factor_rows[(name, horizon)].append(
                        (signal_date, stock_code, factor_value, net)
                    )

    model_results = {
        model: {
            horizon: _aggregate(outcomes[(model, horizon)])
            for horizon in SUPPORTED_HORIZONS
        }
        for model in MODEL_NAMES
    }
    comparison = {}
    for horizon in SUPPORTED_HORIZONS:
        short = model_results[SHORT_TERM_MODEL][horizon]
        low = model_results[LOW_VOLATILITY_MODEL][horizon]
        short_dates = {
            item["signal_date"]: item for item in short["per_date"]
        }
        low_dates = {item["signal_date"]: item for item in low["per_date"]}
        common_dates = sorted(set(short_dates) & set(low_dates))
        date_deltas = [
            float(short_dates[item]["net_return_mean_pct"])
            - float(low_dates[item]["net_return_mean_pct"])
            for item in common_dates
        ]
        comparison[horizon] = {
            "common_signal_date_count": len(common_dates),
            "common_evaluable_signal_samples_per_model": min(
                int(short["sample_count"]), int(low["sample_count"])
            ),
            "short_term_net_mean_delta_pct": (
                _rounded(float(short["net_return_mean_pct"]) - float(low["net_return_mean_pct"]))
                if short["net_return_mean_pct"] is not None
                and low["net_return_mean_pct"] is not None
                else None
            ),
            "short_term_net_median_delta_pct": (
                _rounded(float(short["net_return_median_pct"]) - float(low["net_return_median_pct"]))
                if short["net_return_median_pct"] is not None
                and low["net_return_median_pct"] is not None
                else None
            ),
            "short_term_net_win_rate_delta_pct_points": (
                _rounded(float(short["net_win_rate_pct"]) - float(low["net_win_rate_pct"]))
                if short["net_win_rate_pct"] is not None
                and low["net_win_rate_pct"] is not None
                else None
            ),
            "short_term_hs300_excess_mean_delta_pct": (
                _rounded(
                    float(short["hs300_excess_mean_pct"])
                    - float(low["hs300_excess_mean_pct"])
                )
                if short["hs300_excess_mean_pct"] is not None
                and low["hs300_excess_mean_pct"] is not None
                else None
            ),
            "short_term_better_date_rate_pct": (
                _rounded(sum(value > 0 for value in date_deltas) / len(date_deltas) * 100.0)
                if date_deltas
                else None
            ),
        }
    factor_results = {
        name: {
            horizon: _factor_summary(factor_rows[(name, horizon)])
            for horizon in SUPPORTED_HORIZONS
        }
        for name in FACTOR_NAMES
    }
    common_date_counts = [
        int(comparison[horizon]["common_signal_date_count"])
        for horizon in SUPPORTED_HORIZONS
    ]
    evidence_status = (
        "evaluation_available"
        if common_date_counts
        and min(common_date_counts) >= MIN_COMMON_DATES_FOR_EVIDENCE
        else "insufficient_evidence"
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "base_sha": base_sha,
        "input_content_sha256": input_hash,
        "evidence_status": evidence_status,
        "conclusion_class": "表现接近/证据不足" if evidence_status == "insufficient_evidence" else None,
        "cost_bps": ROUND_TRIP_COST_BPS,
        "minimum_common_dates_for_evidence": MIN_COMMON_DATES_FOR_EVIDENCE,
        "top_n": BENCHMARK_TOP_N,
        "supported_horizons": list(SUPPORTED_HORIZONS),
        "pending_horizons": {
            PENDING_HORIZON: "merged Benchmark 20d execution chain is unavailable"
        },
        "signal_date_range": {
            "start": min(evaluated_dates).isoformat() if evaluated_dates else None,
            "end": max(evaluated_dates).isoformat() if evaluated_dates else None,
        },
        "candidate_signal_count": dict(sorted(candidate_counts.items())),
        "comparison": comparison,
        "models": model_results,
        "ablation_factors": factor_results,
        "exclusions": {
            "reason_counts": dict(sorted(exclusions.items())),
            "records": sorted(
                exclusion_records,
                key=lambda item: (item["signal_date"], tuple(item["horizons"])),
            ),
        },
        "inventory": inventory,
        "metric_definitions": {
            "gross_return": "end_close / shared_reference_price - 1",
            "net_return": "gross_return - 30bps round-trip cost",
            "mfe": "max(0, max_forward_high / shared_reference_price - 1)",
            "mae": "max(0, (shared_reference_price - min_forward_low) / shared_reference_price)",
            "max_drawdown": "peak-to-trough drawdown of date-level equal-weight net returns",
            "hs300_excess": "model net return - HS300 gross return",
            "ablation_diagnostic": "Spearman IC only; no threshold search or model weighting",
        },
    }
    output_hash = hashlib.sha256(canonical_json_bytes(summary)).hexdigest()
    return {**summary, "output_content_sha256": output_hash}
