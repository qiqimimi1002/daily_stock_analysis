"""Minimal interface implemented by future public benchmark models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Mapping, Sequence

from research.benchmarks.schema import BenchmarkModelIdentity, BenchmarkSignal


class BenchmarkModel(ABC):
    """Offline-only contract; Phase 1 intentionally provides no model."""

    @property
    @abstractmethod
    def identity(self) -> BenchmarkModelIdentity:
        """Return the immutable identity of this model configuration."""

    @abstractmethod
    def generate_signals(
        self,
        *,
        universe: Sequence[Mapping[str, Any]],
        market_data_at: datetime,
        source_data_as_of: datetime,
    ) -> Sequence[BenchmarkSignal]:
        """Generate signals using content no later than the market snapshot.

        Acquisition completion belongs in ``BenchmarkSignal.fetched_at`` and
        does not extend ``source_data_as_of``.
        """
