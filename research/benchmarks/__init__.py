"""Offline contracts for reproducible benchmark-model comparisons."""

from research.benchmarks.base import BenchmarkModel
from research.benchmarks.schema import (
    BENCHMARK_SCHEMA_VERSION,
    BenchmarkModelIdentity,
    BenchmarkSignal,
    BenchmarkValidationError,
    serialize_signal_batch,
)
from research.benchmarks.universe import (
    UniverseDecision,
    UniverseStatus,
    evaluate_v21_universe,
)

__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "BenchmarkModel",
    "BenchmarkModelIdentity",
    "BenchmarkSignal",
    "BenchmarkValidationError",
    "UniverseDecision",
    "UniverseStatus",
    "evaluate_v21_universe",
    "serialize_signal_batch",
]
