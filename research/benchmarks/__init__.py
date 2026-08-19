"""Offline contracts for reproducible benchmark-model comparisons."""

from research.benchmarks.base import BenchmarkModel
from research.benchmarks.low_volatility import (
    LowVolatilityResult,
    LowVolatilityStatus,
    create_model_identity as create_low_volatility_model_identity,
    evaluate_history as evaluate_low_volatility_history,
    rank_eligible as rank_low_volatility_eligible,
    simple_daily_returns,
)
from research.benchmarks.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkModelIdentity,
    BenchmarkSignal,
    BenchmarkValidationError,
    serialize_signal_batch,
)
from research.benchmarks.universe import (
    UNIVERSE_CONTRACT_VERSION,
    UniverseDecision,
    UniverseStatus,
    evaluate_v21_universe,
    universe_config_hash,
    universe_config_payload,
)

__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "BenchmarkModel",
    "BenchmarkModelIdentity",
    "BenchmarkSignal",
    "BenchmarkValidationError",
    "LowVolatilityResult",
    "LowVolatilityStatus",
    "UNIVERSE_CONTRACT_VERSION",
    "UniverseDecision",
    "UniverseStatus",
    "create_low_volatility_model_identity",
    "evaluate_low_volatility_history",
    "evaluate_v21_universe",
    "rank_low_volatility_eligible",
    "serialize_signal_batch",
    "simple_daily_returns",
    "universe_config_hash",
    "universe_config_payload",
]
