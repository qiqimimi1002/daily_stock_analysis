# -*- coding: utf-8 -*-
"""Transparent V2.1 scoring for the A-share market screener.

V2.1 deliberately scores only evidence that is available and records the
coverage separately. Missing fundamentals or capital-flow data must never be
silently treated as a positive signal.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd


V21_COMPONENT_MAX: Mapping[str, float] = {
    "fundamental": 30.0,
    "industry_catalyst": 20.0,
    "capital": 20.0,
    "technical": 20.0,
    "valuation": 10.0,
}
V21_MAX_SCORE = sum(V21_COMPONENT_MAX.values())


@dataclass(frozen=True)
class ScoreComponent:
    score: float
    available_max: float
    max_score: float
    status: str
    evidence: Sequence[str]

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["evidence"] = list(self.evidence)
        return value


@dataclass(frozen=True)
class V21ScoreCard:
    score: float
    raw_score: float
    available_max: float
    coverage_pct: float
    confidence: str
    components: Mapping[str, ScoreComponent]
    reasons: Sequence[str]
    risks: Sequence[str]
    evidence_gaps: Sequence[str]
    trigger_conditions: Sequence[str]
    abandon_conditions: Sequence[str]
    hard_reject: bool
    reject_reasons: Sequence[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "raw_score": self.raw_score,
            "available_max": self.available_max,
            "coverage_pct": self.coverage_pct,
            "confidence": self.confidence,
            "components": {
                key: component.as_dict()
                for key, component in self.components.items()
            },
            "reasons": list(self.reasons),
            "risks": list(self.risks),
            "evidence_gaps": list(self.evidence_gaps),
            "trigger_conditions": list(self.trigger_conditions),
            "abandon_conditions": list(self.abandon_conditions),
            "hard_reject": self.hard_reject,
            "reject_reasons": list(self.reject_reasons),
        }


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _triangular(value: float, low: float, ideal: float, high: float) -> float:
    if value <= low or value >= high:
        return 0.0
    if value <= ideal:
        return (value - low) / (ideal - low)
    return (high - value) / (high - ideal)


def _block_data(evidence: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    block = evidence.get(key, {})
    if not isinstance(block, Mapping):
        return {}
    data = block.get("data")
    if isinstance(data, Mapping):
        return data
    return block


def _metric_score(
    value: Any,
    *,
    maximum: float,
    scorer,
    evidence: List[str],
    label: str,
    suffix: str = "",
) -> Tuple[float, float]:
    number = _number(value)
    if number is None:
        return 0.0, 0.0
    evidence.append(f"{label}{number:.2f}{suffix}")
    return _clip(float(scorer(number)), 0.0, maximum), maximum


def score_fundamental(evidence: Mapping[str, Any]) -> ScoreComponent:
    growth = _block_data(evidence, "growth")
    earnings = _block_data(evidence, "earnings")
    report = earnings.get("financial_report", {})
    if not isinstance(report, Mapping):
        report = {}

    details: List[str] = []
    score = 0.0
    available = 0.0

    earned, maximum = _metric_score(
        growth.get("roe"),
        maximum=8.0,
        scorer=lambda value: _clip(value / 15.0, 0.0, 1.0) * 8.0,
        evidence=details,
        label="ROE ",
        suffix="%",
    )
    score += earned
    available += maximum

    earned, maximum = _metric_score(
        growth.get("net_profit_yoy"),
        maximum=6.0,
        scorer=lambda value: _clip((value + 10.0) / 30.0, 0.0, 1.0) * 6.0,
        evidence=details,
        label="净利润同比 ",
        suffix="%",
    )
    score += earned
    available += maximum

    earned, maximum = _metric_score(
        growth.get("revenue_yoy"),
        maximum=5.0,
        scorer=lambda value: _clip((value + 5.0) / 25.0, 0.0, 1.0) * 5.0,
        evidence=details,
        label="营收同比 ",
        suffix="%",
    )
    score += earned
    available += maximum

    earned, maximum = _metric_score(
        growth.get("gross_margin"),
        maximum=2.0,
        scorer=lambda value: _clip(value / 30.0, 0.0, 1.0) * 2.0,
        evidence=details,
        label="毛利率 ",
        suffix="%",
    )
    score += earned
    available += maximum

    earned, maximum = _metric_score(
        growth.get("debt_ratio"),
        maximum=4.0,
        scorer=lambda value: (
            4.0
            if value <= 45.0
            else _clip((80.0 - value) / 35.0, 0.0, 1.0) * 4.0
        ),
        evidence=details,
        label="资产负债率 ",
        suffix="%",
    )
    score += earned
    available += maximum

    cash_flow = _number(report.get("operating_cash_flow"))
    net_profit = _number(report.get("net_profit_parent"))
    if cash_flow is not None:
        available += 5.0
        if cash_flow > 0:
            cash_score = 3.0
            if net_profit is not None and net_profit > 0:
                cash_score = min(5.0, 3.0 + (cash_flow / net_profit))
            score += cash_score
            details.append("经营现金流为正")
        else:
            details.append("经营现金流为负")

    status = "ok" if available >= 20.0 else "partial" if available > 0 else "unavailable"
    return ScoreComponent(
        score=round(score, 2),
        available_max=round(available, 2),
        max_score=30.0,
        status=status,
        evidence=tuple(details),
    )


def score_capital(
    spot: Mapping[str, Any],
    metrics: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> ScoreComponent:
    details: List[str] = []
    score = 0.0
    available = 0.0

    turnover = _number(spot.get("turnover"))
    if turnover is not None:
        available += 5.0
        score += _triangular(turnover, 0.5, 3.0, 12.0) * 5.0
        details.append(f"换手率 {turnover:.2f}%")

    amount = _number(spot.get("amount"))
    if amount is not None:
        available += 3.0
        amount_yi = amount / 100_000_000.0
        score += _clip(math.log10(max(amount_yi, 1.0)) / 1.3, 0.0, 1.0) * 3.0
        details.append(f"成交额 {amount_yi:.2f}亿元")

    spot_volume_ratio = _number(spot.get("volume_ratio"))
    history_volume_ratio = _number(metrics.get("volume_ratio_5d"))
    volume_ratio = spot_volume_ratio if spot_volume_ratio is not None else history_volume_ratio
    if volume_ratio is not None:
        available += 6.0
        score += _triangular(volume_ratio, 0.3, 1.2, 2.5) * 6.0
        details.append(f"量比/量能比 {volume_ratio:.2f}")

    flow = _block_data(evidence, "capital_flow")
    stock_flow = flow.get("stock_flow", {})
    if not isinstance(stock_flow, Mapping):
        stock_flow = {}
    for key, label, maximum in (
        ("main_net_inflow", "主力净流入", 3.0),
        ("inflow_5d", "5日资金流", 2.0),
        ("inflow_10d", "10日资金流", 1.0),
    ):
        value = _number(stock_flow.get(key))
        if value is None:
            continue
        available += maximum
        if value > 0:
            score += maximum
        elif value == 0:
            score += maximum * 0.5
        details.append(f"{label}{'为正' if value > 0 else '不为正'}")

    status = "ok" if available >= 16.0 else "partial" if available > 0 else "unavailable"
    return ScoreComponent(
        score=round(score, 2),
        available_max=round(available, 2),
        max_score=20.0,
        status=status,
        evidence=tuple(details),
    )


def score_technical(
    spot: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> ScoreComponent:
    details: List[str] = []
    close = _number(metrics.get("history_close")) or 0.0
    ma5 = _number(metrics.get("ma5")) or 0.0
    ma10 = _number(metrics.get("ma10")) or 0.0
    ma20 = _number(metrics.get("ma20")) or 0.0
    five_day_pct = _number(metrics.get("five_day_pct")) or 0.0
    daily_pct = _number(spot.get("pct_change")) or 0.0

    score = 0.0
    if close > ma5 > ma10 > ma20 > 0:
        score += 8.0
        details.append("价格与MA5/MA10/MA20呈多头结构")
    elif close > ma20 > 0 and ma5 >= ma10:
        score += 6.0
        details.append("价格位于MA20上方且短期均线未转弱")
    elif close > ma20 > 0:
        score += 4.0
        details.append("价格站上MA20")
    else:
        score += 1.0
        details.append("价格仍需确认能否站稳MA20")

    score += _triangular(five_day_pct, -5.0, 3.0, 12.0) * 6.0
    details.append(f"近5日涨跌 {five_day_pct:+.2f}%")

    if ma20 > 0:
        distance = (close / ma20 - 1.0) * 100.0
        score += _triangular(distance, -5.0, 3.0, 18.0) * 3.0
        details.append(f"相对MA20偏离 {distance:+.2f}%")

    score += _triangular(daily_pct, -4.0, 1.0, 6.0) * 3.0
    details.append(f"当日涨跌 {daily_pct:+.2f}%")
    return ScoreComponent(
        score=round(score, 2),
        available_max=20.0,
        max_score=20.0,
        status="ok",
        evidence=tuple(details),
    )


def score_valuation(
    spot: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> ScoreComponent:
    valuation = _block_data(evidence, "valuation")
    pe = _number(spot.get("pe_ratio"))
    pb = _number(spot.get("pb_ratio"))
    if pe is None:
        pe = _number(valuation.get("pe_ratio"))
    if pb is None:
        pb = _number(valuation.get("pb_ratio"))

    details: List[str] = []
    score = 0.0
    available = 0.0
    if pe is not None:
        available += 6.0
        if pe > 0:
            score += _triangular(pe, 3.0, 18.0, 80.0) * 6.0
        details.append(f"动态PE {pe:.2f}")
    if pb is not None:
        available += 4.0
        if pb > 0:
            score += _triangular(pb, 0.3, 2.0, 12.0) * 4.0
        details.append(f"PB {pb:.2f}")

    status = "ok" if available == 10.0 else "partial" if available > 0 else "unavailable"
    return ScoreComponent(
        score=round(score, 2),
        available_max=available,
        max_score=10.0,
        status=status,
        evidence=tuple(details),
    )


def _risk_gate(
    spot: Mapping[str, Any],
    metrics: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> Tuple[bool, Sequence[str], Sequence[str]]:
    reject: List[str] = []
    warnings: List[str] = []
    growth = _block_data(evidence, "growth")
    earnings = _block_data(evidence, "earnings")
    report = earnings.get("financial_report", {})
    if not isinstance(report, Mapping):
        report = {}

    consecutive_losses = _number(
        evidence.get("consecutive_loss_years", growth.get("consecutive_loss_years"))
    )
    if consecutive_losses is not None and consecutive_losses >= 2:
        reject.append(f"已核实连续亏损{int(consecutive_losses)}年")
    if evidence.get("major_risk_announcement") is True:
        reject.append("已核实存在重大风险公告")
    if evidence.get("regulatory_penalty") is True:
        reject.append("已核实存在重大监管处罚")

    net_profit = _number(report.get("net_profit_parent"))
    cash_flow = _number(report.get("operating_cash_flow"))
    if net_profit is not None and net_profit < 0:
        reject.append("最新已核实归母净利润为负")
    if net_profit is not None and net_profit > 0 and cash_flow is not None and cash_flow < 0:
        warnings.append("盈利为正但经营现金流为负，盈利质量需核查")

    profit_yoy = _number(growth.get("net_profit_yoy"))
    if profit_yoy is not None and profit_yoy <= -50:
        warnings.append(f"净利润同比下降{abs(profit_yoy):.2f}%")

    daily_pct = _number(spot.get("pct_change")) or 0.0
    five_day_pct = _number(metrics.get("five_day_pct")) or 0.0
    turnover = _number(spot.get("turnover")) or 0.0
    if daily_pct >= 5.0:
        warnings.append(f"当日已上涨{daily_pct:.2f}%，存在追高风险")
    if five_day_pct >= 10.0:
        warnings.append(f"近5日已上涨{five_day_pct:.2f}%，位置偏高")
    if turnover >= 8.0:
        warnings.append(f"换手率{turnover:.2f}%偏高，短线波动风险较大")
    if evidence.get("abnormal_volatility") is True:
        reject.append("已核实存在异常波动风险")

    return bool(reject), tuple(reject), tuple(warnings)


def build_v21_scorecard(
    spot: Mapping[str, Any],
    metrics: Mapping[str, Any],
    evidence: Optional[Mapping[str, Any]] = None,
) -> V21ScoreCard:
    evidence = evidence or {}
    components = {
        "fundamental": score_fundamental(evidence),
        # V2.1 reserves the architecture slot but never invents catalyst evidence.
        "industry_catalyst": ScoreComponent(
            score=0.0,
            available_max=0.0,
            max_score=20.0,
            status="unavailable",
            evidence=(),
        ),
        "capital": score_capital(spot, metrics, evidence),
        "technical": score_technical(spot, metrics),
        "valuation": score_valuation(spot, evidence),
    }
    raw_score = sum(component.score for component in components.values())
    available_max = sum(component.available_max for component in components.values())
    coverage_pct = available_max / V21_MAX_SCORE * 100.0
    normalized = raw_score / available_max * 100.0 if available_max > 0 else 0.0
    # Low evidence coverage cannot yield a high-confidence headline score.
    coverage_factor = min(1.0, coverage_pct / 75.0)
    score = normalized * coverage_factor

    hard_reject, reject_reasons, risk_warnings = _risk_gate(spot, metrics, evidence)
    gaps: List[str] = []
    gaps.append("行业催化结构化证据尚未接入，预留20分不计分")
    gaps.append("最近3年净利润与毛利率趋势尚未完成结构化核验")
    if (
        evidence.get("consecutive_loss_years") is None
        and _block_data(evidence, "growth").get("consecutive_loss_years") is None
    ):
        gaps.append("连续亏损年数未取得，不能据此断言企业不存在连续亏损")
    if components["fundamental"].status != "ok":
        gaps.append("基本面数据不完整")
    if components["capital"].available_max < 16.0:
        gaps.append("主力资金或多日资金流数据不完整")
    if components["valuation"].status != "ok":
        gaps.append("估值数据不完整，尚未完成同行业比较")
    else:
        gaps.append("PE/PB已取得，但同行业估值分位尚未接入")
    gaps.append("历史类似信号胜率将在V2.2积累后提供")
    gaps.append("重大公告与监管风险仍需由后续深度分析复核")

    confidence = "高" if coverage_pct >= 75.0 else "中" if coverage_pct >= 55.0 else "低"
    reasons: List[str] = []
    for key in ("fundamental", "industry_catalyst", "capital", "technical", "valuation"):
        component = components[key]
        if component.evidence:
            reasons.append(
                f"{key}得分{component.score:.2f}/{component.available_max:.2f}"
            )

    close = _number(metrics.get("history_close")) or 0.0
    ma20 = _number(metrics.get("ma20")) or 0.0
    volume_ratio = _number(spot.get("volume_ratio"))
    triggers = [
        "保持在上一完整交易日MA20上方" if close >= ma20 > 0 else "重新站上并稳定于MA20上方",
        "盘中量价关系保持温和，不出现巨量冲高回落",
        "深度分析补齐公告、财务和资金流证据后仍未触发风险降级",
    ]
    if volume_ratio is not None:
        triggers.insert(1, f"量比维持在合理区间（当前{volume_ratio:.2f}）")
    abandon = [
        "跌破MA20且无法快速收复",
        "出现重大风险公告、监管处罚或最新财务亏损证据",
        "高开急拉、巨量冲高回落或近5日涨幅突破追高上限",
    ]

    return V21ScoreCard(
        score=round(score, 2),
        raw_score=round(raw_score, 2),
        available_max=round(available_max, 2),
        coverage_pct=round(coverage_pct, 2),
        confidence=confidence,
        components=components,
        reasons=tuple(reasons),
        risks=tuple(risk_warnings),
        evidence_gaps=tuple(gaps),
        trigger_conditions=tuple(triggers),
        abandon_conditions=tuple(abandon),
        hard_reject=hard_reject,
        reject_reasons=tuple(reject_reasons),
    )


def calculate_market_environment(frame: pd.DataFrame) -> Dict[str, Any]:
    """Calculate a transparent market breadth score from the spot snapshot."""
    if frame is None or frame.empty or "pct_change" not in frame.columns:
        return {
            "score": None,
            "strategy": "市场数据不足，保持谨慎",
            "coverage": "unavailable",
            "coverage_note": "未取得有效的全市场涨跌快照",
            "advance_ratio_pct": None,
            "median_pct_change": None,
            "limit_up_count": None,
            "limit_down_count": None,
        }
    changes = pd.to_numeric(frame["pct_change"], errors="coerce").dropna()
    if changes.empty:
        return {
            "score": None,
            "strategy": "市场数据不足，保持谨慎",
            "coverage": "unavailable",
            "coverage_note": "未取得有效的全市场涨跌快照",
            "advance_ratio_pct": None,
            "median_pct_change": None,
            "limit_up_count": None,
            "limit_down_count": None,
        }
    advance_ratio = float((changes > 0).mean())
    median_change = float(changes.median())
    limit_up_count = int((changes >= 9.5).sum())
    limit_down_count = int((changes <= -9.5).sum())
    breadth = (advance_ratio - 0.5) * 70.0
    median_component = _clip(median_change * 6.0, -18.0, 18.0)
    extreme_component = _clip(
        (limit_up_count - limit_down_count) / max(len(changes) * 0.01, 1.0) * 4.0,
        -12.0,
        12.0,
    )
    score = round(_clip(50.0 + breadth + median_component + extreme_component, 0.0, 100.0), 2)
    if score >= 70:
        strategy = "积极观察，但仍需等待个股触发条件"
    elif score >= 50:
        strategy = "中性观察，控制候选数量"
    else:
        strategy = "偏谨慎，降低候选优先级并强化风险过滤"
    return {
        "score": score,
        "strategy": strategy,
        "coverage": "partial",
        "coverage_note": "当前仅使用全市场涨跌广度；指数趋势和历史成交额基线待后续接入",
        "advance_ratio_pct": round(advance_ratio * 100.0, 2),
        "median_pct_change": round(median_change, 2),
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
    }
