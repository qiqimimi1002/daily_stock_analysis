"""V2.1-aligned offline universe eligibility adapter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import math
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import pandas as pd

from src.services.market_screener import (
    MAIN_BOARD_PREFIXES,
    ScreeningConfig,
    apply_spot_filters,
    normalize_spot_frame,
)

from research.benchmarks.schema import canonical_json_bytes


UNIVERSE_CONTRACT_VERSION = "v2_1_mainboard_v1"
_UNIVERSE_CONFIG_FIELDS = (
    "min_amount_yuan",
    "min_turnover_pct",
    "max_turnover_pct",
    "min_price",
    "max_price",
    "min_daily_pct",
    "max_daily_pct",
    "min_five_day_pct",
    "max_five_day_pct",
    "min_history_rows",
)


class UniverseStatus(str, Enum):
    ELIGIBLE = "eligible"
    INSUFFICIENT_HISTORY = "insufficient_history"
    SUSPENDED = "suspended"
    UNAVAILABLE = "unavailable"
    INVALID_DATA = "invalid_data"


@dataclass(frozen=True)
class UniverseDecision:
    """One auditable V2.1 universe eligibility decision."""

    stock_code: str
    stock_name: str
    status: UniverseStatus
    reasons: Tuple[str, ...]
    history_rows: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "history_rows": self.history_rows,
        }


def universe_config_payload(
    config: Optional[ScreeningConfig] = None,
) -> Dict[str, Any]:
    """Return the canonical semantic identity of the V2.1 universe contract."""

    selected = config or ScreeningConfig()
    return {
        "contract_version": UNIVERSE_CONTRACT_VERSION,
        "excluded_name_policy": "blank_or_st_or_exit_or_n_or_c",
        "main_board_prefixes": list(MAIN_BOARD_PREFIXES),
        "thresholds": {
            field: getattr(selected, field) for field in _UNIVERSE_CONFIG_FIELDS
        },
    }


def universe_config_hash(config: Optional[ScreeningConfig] = None) -> str:
    """Hash only universe semantics, excluding retry and worker settings."""

    return hashlib.sha256(canonical_json_bytes(universe_config_payload(config))).hexdigest()


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def evaluate_v21_universe(
    spot_frame: pd.DataFrame,
    *,
    history_rows_by_code: Mapping[str, Optional[int]],
    config: Optional[ScreeningConfig] = None,
) -> Sequence[UniverseDecision]:
    """Classify rows while keeping V2.1's hard filter as the eligibility truth.

    `history_rows_by_code` contains completed daily bars available at the same
    research cutoff. Phase 1 performs no network fetch and does not invent a
    stricter listing-age rule than V2.1's existing `min_history_rows` contract.
    """

    selected_config = config or ScreeningConfig()
    normalized = normalize_spot_frame(spot_frame)
    if normalized["code"].duplicated().any():
        raise ValueError("universe input must contain one row per stock_code")

    full_universe_config = replace(
        selected_config,
        preselect_limit=max(
            selected_config.preselect_limit,
            selected_config.top_n,
            len(normalized),
        ),
    )
    filtered = apply_spot_filters(spot_frame, full_universe_config)
    hard_filter_codes = set(filtered["code"].astype(str))

    decisions = []
    numeric_fields = ("close", "pct_change", "volume", "amount", "turnover")
    for row in normalized.sort_values("code", kind="stable").to_dict("records"):
        code = str(row["code"])
        name = str(row["name"])
        history_value = history_rows_by_code.get(code)

        if len(code) != 6 or not code.isdigit() or any(
            not _is_finite_number(row.get(field)) for field in numeric_fields
        ):
            status = UniverseStatus.INVALID_DATA
            reasons = ("required_market_field_invalid",)
        elif float(row["volume"]) <= 0 or float(row["amount"]) <= 0:
            status = UniverseStatus.SUSPENDED
            reasons = ("no_current_market_turnover",)
        elif code not in hard_filter_codes:
            status = UniverseStatus.UNAVAILABLE
            reasons = ("v2_1_hard_filter_rejected",)
        elif history_value is None:
            status = UniverseStatus.UNAVAILABLE
            reasons = ("history_unavailable",)
        elif (
            not isinstance(history_value, int)
            or isinstance(history_value, bool)
            or history_value < 0
        ):
            status = UniverseStatus.INVALID_DATA
            reasons = ("history_row_count_invalid",)
        elif history_value < selected_config.min_history_rows:
            status = UniverseStatus.INSUFFICIENT_HISTORY
            reasons = ("v2_1_min_history_rows_not_met",)
        else:
            status = UniverseStatus.ELIGIBLE
            reasons = ("v2_1_universe_contract_satisfied",)

        decisions.append(
            UniverseDecision(
                stock_code=code,
                stock_name=name,
                status=status,
                reasons=reasons,
                history_rows=history_value,
            )
        )
    return tuple(decisions)
