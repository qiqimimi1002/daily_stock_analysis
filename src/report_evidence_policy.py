# -*- coding: utf-8 -*-
"""Strict evidence rules shared by report renderers."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from src.schemas.decision_action import display_decision_type_for_result


def _language_bucket(report_language: Any) -> str:
    value = str(report_language or "zh").lower()
    if value.startswith("en"):
        return "en"
    if value.startswith("ko"):
        return "ko"
    return "zh"


def is_actionable_buy_result(result: Any, report_language: str = "zh") -> bool:
    """Return True only when the final canonical decision is buy."""
    return display_decision_type_for_result(
        result,
        report_language=report_language,
    ) == "buy"


def news_verification_notice(result: Any, report_language: str = "zh") -> str:
    """Explain that news/announcement claims are unverified when search has no evidence."""
    if bool(getattr(result, "search_performed", False)):
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


def market_snapshot_labels(
    result: Any,
    labels: Dict[str, str],
    report_language: str = "zh",
) -> Tuple[str, str]:
    """Return an evidence-safe market snapshot heading and close-column label."""
    snapshot = getattr(result, "market_snapshot", None)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    phase_summary = getattr(result, "market_phase_summary", None)
    phase_summary = phase_summary if isinstance(phase_summary, dict) else {}

    phase = str(phase_summary.get("phase") or "").strip().lower()
    bar_date = (
        phase_summary.get("effective_daily_bar_date")
        or snapshot.get("date")
        or ""
    )
    date_suffix_zh = f"（{bar_date}）" if bar_date else ""
    date_suffix_en = f" ({bar_date})" if bar_date else ""
    lang = _language_bucket(report_language)

    if phase in {"premarket", "non_trading"}:
        if lang == "en":
            return f"Previous Complete Trading Day{date_suffix_en}", "Previous Complete Close"
        if lang == "ko":
            return f"직전 완료 거래일 시세{date_suffix_zh}", "직전 완료 거래일 종가"
        return f"上一完整交易日行情{date_suffix_zh}", "上一完整交易日收盘价"

    heading = labels.get("market_snapshot_heading", "当日行情")
    close_label = labels.get("close_label", "收盘价")
    if bar_date:
        if lang == "en":
            heading = f"{heading}{date_suffix_en}"
        else:
            heading = f"{heading}{date_suffix_zh}"
    return heading, close_label


def market_snapshot_heading(result: Any, labels: Dict[str, str], report_language: str = "zh") -> str:
    return market_snapshot_labels(result, labels, report_language)[0]


def market_snapshot_close_label(result: Any, labels: Dict[str, str], report_language: str = "zh") -> str:
    return market_snapshot_labels(result, labels, report_language)[1]
