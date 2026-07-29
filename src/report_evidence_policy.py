# -*- coding: utf-8 -*-
"""Strict evidence rules shared by report renderers."""

from __future__ import annotations

from collections.abc import Mapping
from numbers import Integral
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.schemas.decision_action import display_decision_type_for_result


_INTRADAY_PHASES = {"intraday", "lunch_break", "closing_auction"}
_BUY_WORDS_ZH = re.compile(
    r"(维持|继续)?\s*"
    r"(买入评级|逢低布局|分批布局|分批介入|买入|加仓|建仓|低吸|布局|介入|抄底)"
)


def _language_bucket(report_language: Any) -> str:
    value = str(report_language or "zh").lower()
    if value.startswith("en"):
        return "en"
    if value.startswith("ko"):
        return "ko"
    return "zh"


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def is_actionable_buy_result(result: Any, report_language: str = "zh") -> bool:
    """Return True only when the final canonical decision is buy."""
    return display_decision_type_for_result(
        result,
        report_language=report_language,
    ) == "buy"


def news_evidence_count(result: Any) -> Optional[int]:
    """Return the authoritative retrieved-record count when available."""
    count = _as_count(getattr(result, "news_result_count", None))
    if count is not None:
        return count

    snapshot = _as_mapping(getattr(result, "diagnostic_context_snapshot", None))
    count = _as_count(snapshot.get("news_result_count"))
    if count is not None:
        return count

    overview = _as_mapping(getattr(result, "analysis_context_pack_overview", None))
    metadata = _as_mapping(overview.get("metadata"))
    count = _as_count(metadata.get("news_result_count"))
    if count is not None:
        return count

    nested_overview = _as_mapping(snapshot.get("analysis_context_pack_overview"))
    nested_metadata = _as_mapping(nested_overview.get("metadata"))
    return _as_count(nested_metadata.get("news_result_count"))


def has_verified_news_evidence(result: Any) -> bool:
    """Return whether report-visible news claims are backed by retrieved records.

    ``search_performed`` only records that a search path was attempted. It is
    not proof that any article or announcement was successfully retrieved.
    """
    count = news_evidence_count(result)
    return count is not None and count > 0


def news_verification_notice(result: Any, report_language: str = "zh") -> str:
    """Explain that news/announcement claims are unverified when no records exist."""
    if has_verified_news_evidence(result):
        return ""
    lang = _language_bucket(report_language)
    if lang == "en":
        return (
            "News and announcement retrieval did not produce verifiable evidence. "
            "Recent material events cannot be confirmed and require manual review."
        )
    if lang == "ko":
        return (
            "뉴스 및 공시 검색에서 검증 가능한 근거를 확보하지 못했습니다. "
            "최근 중요 사항은 확인할 수 없으므로 수동 검토가 필요합니다."
        )
    return "新闻及公告未完成有效检索，无法确认近期是否存在重大事项，需人工核查。"


def _phase_and_dates(result: Any) -> Tuple[str, str, str, str]:
    snapshot = _as_mapping(getattr(result, "market_snapshot", None))
    phase_summary = _as_mapping(getattr(result, "market_phase_summary", None))
    phase = str(phase_summary.get("phase") or "").strip().lower()
    snapshot_date = str(snapshot.get("date") or "").strip()
    session_date = str(phase_summary.get("session_date") or "").strip()
    effective_date = str(
        phase_summary.get("effective_daily_bar_date") or ""
    ).strip()
    return phase, snapshot_date, session_date, effective_date


def market_snapshot_labels(
    result: Any,
    labels: Dict[str, str],
    report_language: str = "zh",
) -> Tuple[str, str]:
    """Return a phase-accurate market heading and price-column label."""
    phase, snapshot_date, session_date, effective_date = _phase_and_dates(result)
    lang = _language_bucket(report_language)

    if phase in {"premarket", "non_trading"}:
        bar_date = effective_date or snapshot_date
        if lang == "en":
            suffix = f" ({bar_date})" if bar_date else ""
            return f"Previous Complete Trading Day{suffix}", "Previous Complete Close"
        suffix = f"（{bar_date}）" if bar_date else ""
        if lang == "ko":
            return f"직전 완료 거래일 시세{suffix}", "직전 완료 거래일 종가"
        return f"上一完整交易日行情{suffix}", "上一完整交易日收盘价"

    if phase in _INTRADAY_PHASES:
        bar_date = session_date or snapshot_date
        if lang == "en":
            suffix = f" ({bar_date}, not closed)" if bar_date else " (not closed)"
            return f"Intraday Market Snapshot{suffix}", "Intraday Price"
        if lang == "ko":
            suffix = f"（{bar_date}, 미마감）" if bar_date else "（미마감）"
            return f"장중 시세{suffix}", "장중 가격"
        suffix = f"（{bar_date}，未收盘）" if bar_date else "（未收盘）"
        return f"盘中行情{suffix}", "盘中价"

    if phase == "postmarket":
        bar_date = session_date or snapshot_date or effective_date
        if lang == "en":
            suffix = f" ({bar_date})" if bar_date else ""
            return f"Official Close{suffix}", "Close"
        suffix = f"（{bar_date}）" if bar_date else ""
        if lang == "ko":
            return f"당일 마감 시세{suffix}", "종가"
        return f"当日收盘行情{suffix}", labels.get("close_label", "收盘价")

    # Unknown/legacy phase: label stale data conservatively when dates show it
    # predates the active session; otherwise preserve the existing report label.
    reference_date = session_date or effective_date
    if snapshot_date and reference_date and snapshot_date < reference_date:
        if lang == "en":
            return f"Previous Complete Trading Day ({snapshot_date})", "Previous Complete Close"
        suffix = f"（{snapshot_date}）"
        if lang == "ko":
            return f"직전 완료 거래일 시세{suffix}", "직전 완료 거래일 종가"
        return f"上一完整交易日行情{suffix}", "上一完整交易日收盘价"

    bar_date = snapshot_date or effective_date
    heading = labels.get("market_snapshot_heading", "当日行情")
    close_label = labels.get("close_label", "收盘价")
    if bar_date:
        heading = f"{heading} ({bar_date})" if lang == "en" else f"{heading}（{bar_date}）"
    return heading, close_label


def market_snapshot_heading(result: Any, labels: Dict[str, str], report_language: str = "zh") -> str:
    return market_snapshot_labels(result, labels, report_language)[0]


def market_snapshot_close_label(result: Any, labels: Dict[str, str], report_language: str = "zh") -> str:
    return market_snapshot_labels(result, labels, report_language)[1]


def market_snapshot_price_label(
    result: Any,
    labels: Dict[str, str],
    report_language: str = "zh",
) -> str:
    """Return an accurate label for the provider quote shown below the OHLC table."""
    phase, _, _, _ = _phase_and_dates(result)
    lang = _language_bucket(report_language)
    if phase in _INTRADAY_PHASES:
        if lang == "en":
            return "Latest Intraday Price"
        if lang == "ko":
            return "최근 장중 가격"
        return "最新盘中价"
    if phase in {"premarket", "non_trading"}:
        if lang == "en":
            return "Latest Available Price"
        if lang == "ko":
            return "최근 이용 가능 가격"
        return "最近可用价"
    return labels.get("current_price_label", "当前价")


def market_local_now() -> datetime:
    """Return the report time in the A-share market timezone."""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def market_snapshot_for_report(result: Any) -> Dict[str, Any]:
    """Return a display snapshot with internally consistent price arithmetic."""
    snapshot = _as_mapping(getattr(result, "market_snapshot", None))
    if not snapshot:
        return {}
    normalized = dict(snapshot)
    close = _as_float(snapshot.get("close"))
    prev_close = _as_float(snapshot.get("prev_close"))
    if close is not None and prev_close not in (None, 0):
        change = close - prev_close
        normalized["change_amount"] = f"{change:+.2f}"
        normalized["pct_chg"] = f"{change / prev_close * 100:+.2f}%"
    return normalized


def price_data_for_report(result: Any, price_data: Any) -> Dict[str, Any]:
    """Use the same quote in the price-position table as in the snapshot."""
    normalized = _as_mapping(price_data)
    snapshot = market_snapshot_for_report(result)
    phase, _, _, _ = _phase_and_dates(result)
    if phase in _INTRADAY_PHASES and snapshot.get("close") not in (None, ""):
        normalized["current_price"] = snapshot["close"]
    return normalized


def sanitize_action_text(
    result: Any,
    value: Any,
    report_language: str = "zh",
) -> str:
    """Remove incremental-buy language when the final decision is not buy."""
    text = str(value or "").strip()
    if not text or is_actionable_buy_result(result, report_language):
        return text
    lang = _language_bucket(report_language)
    if lang == "en":
        return re.sub(
            r"\b(maintain|keep|continue)?\s*(buy rating|buy|add|build position|accumulate)\b",
            "hold and observe",
            text,
            flags=re.IGNORECASE,
        )
    if lang == "ko":
        return re.sub(r"(매수 의견 유지|매수|추가 매수|포지션 구축|저가 매수)", "보유 관찰", text)
    return _BUY_WORDS_ZH.sub("持有观察", text)


def sanitize_action_items(
    result: Any,
    values: Any,
    report_language: str = "zh",
) -> List[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return [sanitize_action_text(result, value, report_language) for value in values]


def conservative_volume_meaning(vol_data: Any, report_language: str = "zh") -> str:
    """Describe volume without inferring pressure, washouts, or future direction."""
    ratio = _as_mapping(vol_data).get("volume_ratio")
    ratio_text = str(ratio) if ratio not in (None, "") else "N/A"
    lang = _language_bucket(report_language)
    if lang == "en":
        return (
            f"Volume ratio {ratio_text} only describes relative activity; "
            "it does not by itself establish pressure or future direction."
        )
    if lang == "ko":
        return (
            f"거래량 비율 {ratio_text}은 상대적 거래 활동만 나타내며, "
            "그 자체로 매수·매도 압력이나 향후 방향을 판단할 수 없습니다."
        )
    return f"量比{ratio_text}仅反映相对成交活跃度，不能单独据此判断买卖压力或后续方向。"


def attribution_weights_for_result(
    result: Any,
    signal_attr: Any,
) -> List[Tuple[str, Any]]:
    """Hide attribution percentages when key evidence is incomplete."""
    if not has_verified_news_evidence(result):
        return []
    dashboard = _as_mapping(getattr(result, "dashboard", None))
    perspective = _as_mapping(dashboard.get("data_perspective"))
    has_technical = bool(
        _as_mapping(perspective.get("trend_status"))
        or _as_mapping(perspective.get("price_position"))
    )
    has_fundamentals = bool(
        getattr(result, "financial_summary", None)
        or dashboard.get("fundamental_analysis")
        or dashboard.get("financial_summary")
    )
    if not (has_technical and has_fundamentals):
        return []
    from src.utils.data_processing import signal_attribution_weight_items

    return signal_attribution_weight_items(_as_mapping(signal_attr))
