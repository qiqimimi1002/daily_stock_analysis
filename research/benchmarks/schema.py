"""Deterministic identities and strict JSON contracts for benchmark signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
import re
from typing import Any, Dict, Mapping, Optional, Sequence
import uuid
from zoneinfo import ZoneInfo

from research.archive import (
    SignalValidationError,
    canonical_json_bytes as archive_canonical_json_bytes,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
BENCHMARK_SCHEMA_VERSION = "1.0"
MODEL_ID_NAMESPACE = uuid.UUID("2a5a9f2c-d106-54ee-83f1-f5704b0a7e4c")
SIGNAL_ID_NAMESPACE = uuid.UUID("76b018e1-7caf-59c6-951f-54e5395a5c32")
_STOCK_CODE_RE = re.compile(r"^[0-9]{6}$")


class BenchmarkValidationError(SignalValidationError):
    """Raised when benchmark metadata could introduce ambiguity or leakage."""


def _required_text(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BenchmarkValidationError(f"{field} is required")
    return text


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip() or None


def _strict_json_value(value: Any, *, field: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BenchmarkValidationError(f"{field} cannot contain NaN or Infinity")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _strict_json_value(item, field=f"{field}.{key}")
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [
            _strict_json_value(item, field=f"{field}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise BenchmarkValidationError(
        f"{field} must contain only strict JSON-compatible values"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable strict-JSON representation used by all identities."""

    return archive_canonical_json_bytes(_strict_json_value(value))


def _aware_shanghai_datetime(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _required_text(value, field=field)
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


def _signal_date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise BenchmarkValidationError("signal_date must be a date, not a datetime")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(_required_text(value, field="signal_date"))
    except ValueError:
        raise BenchmarkValidationError(
            "signal_date must be an ISO-8601 date"
        ) from None


def _stock_code(value: Any) -> str:
    code = _required_text(value, field="stock_code")
    if not _STOCK_CODE_RE.fullmatch(code):
        raise BenchmarkValidationError("stock_code must contain exactly six digits")
    return code


def _finite_number(
    value: Any,
    *,
    field: str,
    nullable: bool = False,
    positive: bool = False,
) -> Optional[float]:
    if value is None and nullable:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BenchmarkValidationError(f"{field} must be a finite number") from None
    if not math.isfinite(number):
        raise BenchmarkValidationError(f"{field} must be a finite number")
    if positive and number <= 0:
        raise BenchmarkValidationError(f"{field} must be positive")
    return number


def _uuid5(namespace: uuid.UUID, payload: Mapping[str, Any]) -> str:
    return str(uuid.uuid5(namespace, canonical_json_bytes(payload).decode("utf-8")))


def _frozen_instance(cls, values: Mapping[str, Any]):
    instance = object.__new__(cls)
    for field, value in values.items():
        object.__setattr__(instance, field, value)
    return instance


def _model_identity_payload(
    *,
    model_version: str,
    model_family: str,
    variant: str,
    calculation_version: str,
    parameters: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "calculation_version": calculation_version,
        "model_family": model_family,
        "model_version": model_version,
        "parameters": _strict_json_value(parameters, field="parameters"),
        "variant": variant,
    }


@dataclass(frozen=True, init=False)
class BenchmarkModelIdentity:
    """Auditable identity for one original model or parameter variant."""

    model_id: str
    model_name: str
    model_version: str
    model_family: str
    variant: str
    calculation_version: str
    parameters: Mapping[str, Any]
    generated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        model_name: str,
        model_version: str,
        model_family: str,
        variant: str,
        calculation_version: str,
        parameters: Mapping[str, Any],
        generated_at: datetime | str,
    ) -> "BenchmarkModelIdentity":
        normalized = {
            "model_name": _required_text(model_name, field="model_name"),
            "model_version": _required_text(model_version, field="model_version"),
            "model_family": _required_text(model_family, field="model_family"),
            "variant": _required_text(variant, field="variant"),
            "calculation_version": _required_text(
                calculation_version, field="calculation_version"
            ),
            "parameters": _strict_json_value(parameters, field="parameters"),
            "generated_at": _aware_shanghai_datetime(
                generated_at, field="generated_at"
            ),
        }
        identity_payload = _model_identity_payload(
            model_version=normalized["model_version"],
            model_family=normalized["model_family"],
            variant=normalized["variant"],
            calculation_version=normalized["calculation_version"],
            parameters=normalized["parameters"],
        )
        return _frozen_instance(
            cls,
            {
                "model_id": _uuid5(MODEL_ID_NAMESPACE, identity_payload),
                **normalized,
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_family": self.model_family,
            "variant": self.variant,
            "calculation_version": self.calculation_version,
            "parameters": _strict_json_value(self.parameters, field="parameters"),
            "generated_at": self.generated_at.isoformat(timespec="seconds"),
        }


def _signal_identity_payload(
    *,
    model_id: str,
    stock_code: str,
    signal_date: date,
    market_data_at: datetime,
    reference_price: float,
    rank: int,
    score: Optional[float],
    raw_metric: Any,
    source_data_as_of: datetime,
    parameters: Mapping[str, Any],
    calculation_version: str,
) -> Dict[str, Any]:
    return {
        "calculation_version": calculation_version,
        "market_data_at": market_data_at.isoformat(timespec="seconds"),
        "model_id": model_id,
        "parameters": _strict_json_value(parameters, field="parameters"),
        "rank": rank,
        "raw_metric": _strict_json_value(raw_metric, field="raw_metric"),
        "reference_price": reference_price,
        "score": score,
        "signal_date": signal_date.isoformat(),
        "source_data_as_of": source_data_as_of.isoformat(timespec="seconds"),
        "stock_code": stock_code,
    }


@dataclass(frozen=True, init=False)
class BenchmarkSignal:
    """One ranked observation signal, independent of future-outcome logic."""

    signal_id: str
    model_id: str
    model_name: str
    model_version: str
    model_family: str
    variant: str
    stock_code: str
    stock_name: Optional[str]
    signal_date: date
    market_data_at: datetime
    reference_price: float
    rank: int
    score: Optional[float]
    raw_metric: Any
    selection_reason: str
    source_data_as_of: datetime
    parameters: Mapping[str, Any]
    calculation_version: str

    @classmethod
    def create(
        cls,
        *,
        model: BenchmarkModelIdentity,
        stock_code: str,
        signal_date: date | str,
        market_data_at: datetime | str,
        reference_price: Any,
        rank: int,
        score: Any,
        raw_metric: Any,
        selection_reason: str,
        source_data_as_of: datetime | str,
        stock_name: Optional[str] = None,
    ) -> "BenchmarkSignal":
        local_market_time = _aware_shanghai_datetime(
            market_data_at, field="market_data_at"
        )
        local_source_time = _aware_shanghai_datetime(
            source_data_as_of, field="source_data_as_of"
        )
        local_signal_date = _signal_date(signal_date)
        if local_market_time.date() != local_signal_date:
            raise BenchmarkValidationError(
                "signal_date must match market_data_at in Asia/Shanghai"
            )
        if local_source_time < local_market_time:
            raise BenchmarkValidationError(
                "source_data_as_of cannot be earlier than market_data_at"
            )
        if local_source_time > model.generated_at:
            raise BenchmarkValidationError(
                "source_data_as_of cannot be later than generated_at"
            )
        if local_market_time > model.generated_at:
            raise BenchmarkValidationError(
                "market_data_at cannot be later than generated_at"
            )
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            raise BenchmarkValidationError("rank must be a positive integer")

        normalized_code = _stock_code(stock_code)
        normalized_price = _finite_number(
            reference_price, field="reference_price", positive=True
        )
        normalized_score = _finite_number(score, field="score", nullable=True)
        normalized_metric = _strict_json_value(raw_metric, field="raw_metric")
        normalized_parameters = _strict_json_value(
            model.parameters, field="parameters"
        )
        normalized_reason = _required_text(
            selection_reason, field="selection_reason"
        )
        payload = _signal_identity_payload(
            model_id=model.model_id,
            stock_code=normalized_code,
            signal_date=local_signal_date,
            market_data_at=local_market_time,
            reference_price=normalized_price,
            rank=rank,
            score=normalized_score,
            raw_metric=normalized_metric,
            source_data_as_of=local_source_time,
            parameters=normalized_parameters,
            calculation_version=model.calculation_version,
        )
        return _frozen_instance(
            cls,
            {
                "signal_id": _uuid5(SIGNAL_ID_NAMESPACE, payload),
                "model_id": model.model_id,
                "model_name": model.model_name,
                "model_version": model.model_version,
                "model_family": model.model_family,
                "variant": model.variant,
                "stock_code": normalized_code,
                "stock_name": _optional_text(stock_name),
                "signal_date": local_signal_date,
                "market_data_at": local_market_time,
                "reference_price": normalized_price,
                "rank": rank,
                "score": normalized_score,
                "raw_metric": normalized_metric,
                "selection_reason": normalized_reason,
                "source_data_as_of": local_source_time,
                "parameters": normalized_parameters,
                "calculation_version": model.calculation_version,
            },
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_family": self.model_family,
            "variant": self.variant,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "signal_date": self.signal_date.isoformat(),
            "market_data_at": self.market_data_at.isoformat(timespec="seconds"),
            "reference_price": self.reference_price,
            "rank": self.rank,
            "score": self.score,
            "raw_metric": _strict_json_value(self.raw_metric, field="raw_metric"),
            "selection_reason": self.selection_reason,
            "source_data_as_of": self.source_data_as_of.isoformat(timespec="seconds"),
            "parameters": _strict_json_value(self.parameters, field="parameters"),
            "calculation_version": self.calculation_version,
        }

    def to_outcome_signal_core(self) -> Dict[str, Any]:
        """Return only the stable fields a future outcome adapter may consume."""

        return {
            "signal_id": self.signal_id,
            "stock_code": self.stock_code,
            "signal_date": self.signal_date.isoformat(),
            "market_data_at": self.market_data_at.isoformat(timespec="seconds"),
            "reference_price": self.reference_price,
        }


def serialize_signal_batch(
    model: BenchmarkModelIdentity,
    signals: Sequence[BenchmarkSignal],
) -> bytes:
    """Serialize one batch with a deterministic signal order and strict JSON."""

    for signal in signals:
        if signal.model_id != model.model_id:
            raise BenchmarkValidationError(
                "all signals in a batch must reference the supplied model_id"
            )
    ordered = sorted(
        signals,
        key=lambda item: (item.rank, item.stock_code, item.signal_id),
    )
    payload = {
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
        "model": model.to_dict(),
        "signals": [signal.to_dict() for signal in ordered],
    }
    return canonical_json_bytes(payload) + b"\n"
