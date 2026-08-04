"""Offline research utilities for immutable Daily Stock signal archives."""

from research.archive import (
    ArchiveConflictError,
    ArchiveResult,
    LoadedSourceArtifact,
    SignalValidationError,
    archive_signals,
    build_signal_id,
    load_source_artifact,
)
from research.outcomes import (
    OutcomeConflictError,
    OutcomeResult,
    OutcomeValidationError,
    build_outcome_id,
    calculate_outcomes,
    load_price_artifact,
)

__all__ = [
    "ArchiveConflictError",
    "ArchiveResult",
    "LoadedSourceArtifact",
    "SignalValidationError",
    "archive_signals",
    "build_signal_id",
    "load_source_artifact",
    "OutcomeConflictError",
    "OutcomeResult",
    "OutcomeValidationError",
    "build_outcome_id",
    "calculate_outcomes",
    "load_price_artifact",
]
