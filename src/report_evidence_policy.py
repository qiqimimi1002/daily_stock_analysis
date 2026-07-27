# -*- coding: utf-8 -*-
"""Strict evidence rules shared by report renderers."""

from __future__ import annotations

from collections.abc import Mapping
from numbers import Integral
from typing import Any, Dict, Optional, Tuple

from src.schemas.decision_action import display_decision_type_for_result


_INTRADAY_PHASES = {"intraday", "lunch_break", "closing_auction"}


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
