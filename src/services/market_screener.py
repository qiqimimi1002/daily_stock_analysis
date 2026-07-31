# -*- coding: utf-8 -*-
"""A-share main-board market screener.

The screener deliberately separates cheap, market-wide filters from the more
expensive per-symbol history lookup:

1. Fetch one full-market spot snapshot.
2. Keep liquid Shanghai/Shenzhen main-board shares only.
3. Fetch daily history for a small preselection.
4. Rank transparent candidates for follow-up analysis.

The output is an observation list, not a buy recommendation.
"""

from __future__ import annotations

import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from src.services.market_scoring import (
    V21ScoreCard,
    build_v21_scorecard,
    calculate_market_environment,
)


logger = logging.getLogger(__name__)
CN_TZ = ZoneInfo("Asia/Shanghai")

MAIN_BOARD_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
_SPOT_ALIASES: Mapping[str, Sequence[str]] = {
    "code": ("代码", "股票代码", "code", "股票编号"),
    "name": ("名称", "股票名称", "name"),
    "close": ("最新价", "最新", "close", "最新价格", "最新报价"),
    "pct_change": ("涨跌幅", "涨跌幅(%)", "pct_change", "change_percent"),
    "volume": ("成交量", "volume"),
    "amount": ("成交额", "amount", "成交金额"),
    "turnover": (
        "换手率",
        "换手率(%)",
        "turnover",
        "turnover_rate",
        "turnoverratio",
    ),
    "volume_ratio": ("量比", "volume_ratio"),
    "pe_ratio": ("市盈率-动态", "市盈率", "pe_ratio", "pe"),
    "pb_ratio": ("市净率", "pb_ratio", "pb"),
    "industry": ("行业", "所属行业", "industry"),
}

_HISTORY_ALIASES: Mapping[str, Sequence[str]] = {
    "date": ("日期", "date", "时间"),
    "close": ("收盘", "收盘价", "close"),
    "volume": ("成交量", "volume"),
    "amount": ("成交额", "成交金额", "amount"),
}

_SINA_RAW_SPOT_ALIASES: Mapping[str, Sequence[str]] = {
    "code": ("code", "symbol"),
    "name": ("name",),
    "close": ("trade",),
    "pct_change": ("changepercent",),
    "volume": ("volume",),
    "amount": ("amount",),
    "turnover": ("turnoverratio",),
    "pe_ratio": ("per",),
    "pb_ratio": ("pb",),
}


def _strict_json_value(value: Any) -> Any:
    """Replace non-finite floats so saved artifacts are valid strict JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {
            str(key): _strict_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_strict_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ScreeningConfig:
    """User-facing screening thresholds."""

    top_n: int = 5
    analysis_limit: int = 3
    preselect_limit: int = 60
    history_workers: int = 4
    enrichment_limit: int = 8
    evidence_workers: int = 2
    evidence_budget_seconds: float = 12.0
    min_amount_yuan: float = 200_000_000.0
    min_turnover_pct: float = 0.5
    max_turnover_pct: float = 12.0
    min_price: float = 3.0
    max_price: float = 3_000.0
    min_daily_pct: float = -4.0
    max_daily_pct: float = 6.0
    min_five_day_pct: float = -5.0
    max_five_day_pct: float = 12.0
    min_history_rows: int = 20

    def __post_init__(self) -> None:
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1")
        if self.analysis_limit < 1:
            raise ValueError("analysis_limit must be at least 1")
        if self.preselect_limit < self.top_n:
            raise ValueError("preselect_limit must be >= top_n")
        if self.history_workers < 1:
            raise ValueError("history_workers must be at least 1")
        if self.enrichment_limit < self.top_n:
            raise ValueError("enrichment_limit must be >= top_n")
        if self.evidence_workers < 1:
            raise ValueError("evidence_workers must be at least 1")
        if self.evidence_budget_seconds <= 0:
            raise ValueError("evidence_budget_seconds must be positive")


@dataclass(frozen=True)
class ScreeningCandidate:
    code: str
    name: str
    score: float
    raw_score: float
    available_max_score: float
    score_coverage_pct: float
    confidence_label: str
    score_breakdown: Mapping[str, Any]
    latest_price: float
    daily_pct: float
    five_day_pct: float
    amount_yi: float
    avg_amount_20d_yi: Optional[float]
    turnover_pct: float
    ma5: float
    ma10: float
    ma20: float
    volume_ratio_5d: float
    trend_label: str
    watch_zone: str
    industry: str
    pe_ratio: Optional[float]
    pb_ratio: Optional[float]
    historical_win_rate: Optional[float]
    risk_gate: str
    reasons: Sequence[str]
    risks: Sequence[str]
    evidence_gaps: Sequence[str]
    trigger_conditions: Sequence[str]
    abandon_conditions: Sequence[str]

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        for key in (
            "reasons",
            "risks",
            "evidence_gaps",
            "trigger_conditions",
            "abandon_conditions",
        ):
            value[key] = list(value[key])
        return value


@dataclass(frozen=True)
class ScreeningResult:
    generated_at: str
    universe_count: int
    spot_filtered_count: int
    history_success_count: int
    history_failure_count: int
    evidence_success_count: int
    evidence_failure_count: int
    candidates: Sequence[ScreeningCandidate]
    analysis_codes: Sequence[str]
    config: ScreeningConfig
    data_source: str
    limitations: Sequence[str]
    model_version: str
    market_environment: Mapping[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "universe_count": self.universe_count,
            "spot_filtered_count": self.spot_filtered_count,
            "history_success_count": self.history_success_count,
            "history_failure_count": self.history_failure_count,
            "evidence_success_count": self.evidence_success_count,
            "evidence_failure_count": self.evidence_failure_count,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "analysis_codes": list(self.analysis_codes),
            "config": asdict(self.config),
            "data_source": self.data_source,
            "limitations": list(self.limitations),
            "model_version": self.model_version,
            "market_environment": dict(self.market_environment),
        }


def normalize_stock_code(value: Any) -> str:
    """Return a six-digit A-share code without spreadsheet decimal suffixes."""

    raw = str(value or "").strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    digits = "".join(char for char in raw if char.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def is_main_board_code(code: Any) -> bool:
    normalized = normalize_stock_code(code)
    return len(normalized) == 6 and normalized.startswith(MAIN_BOARD_PREFIXES)


def is_excluded_name(name: Any) -> bool:
    normalized = str(name or "").strip().upper().replace(" ", "")
    return (
        not normalized
        or "ST" in normalized
        or "退" in normalized
        or normalized.startswith(("N", "C"))
    )


def _first_column(frame: pd.DataFrame, aliases: Iterable[str]) -> Optional[str]:
    columns = {str(column).strip().lower(): str(column) for column in frame.columns}
    for alias in aliases:
        match = columns.get(str(alias).strip().lower())
        if match is not None:
            return match
    return None


def _normalize_columns(
    frame: pd.DataFrame,
    aliases: Mapping[str, Sequence[str]],
    required: Sequence[str],
) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("数据源返回空表")

    rename: Dict[str, str] = {}
    for canonical, options in aliases.items():
        source = _first_column(frame, options)
        if source is not None:
            rename[source] = canonical

    normalized = frame.rename(columns=rename).copy()
    missing = [column for column in required if column not in normalized.columns]
    if missing:
        raise ValueError(f"数据缺少必要字段: {', '.join(missing)}")
    return normalized


def normalize_spot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = _normalize_columns(
        frame,
        _SPOT_ALIASES,
        required=("code", "name", "close", "pct_change", "volume", "amount", "turnover"),
    )
    normalized["code"] = normalized["code"].map(normalize_stock_code)
    normalized["name"] = normalized["name"].astype(str).str.strip()
    for column in (
        "close",
        "pct_change",
        "volume",
        "amount",
        "turnover",
        "volume_ratio",
        "pe_ratio",
        "pb_ratio",
    ):
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    for column in ("volume_ratio", "pe_ratio", "pb_ratio"):
        if column not in normalized.columns:
            normalized[column] = math.nan
    if "industry" not in normalized.columns:
        normalized["industry"] = ""
    normalized["industry"] = normalized["industry"].fillna("").astype(str).str.strip()
    return normalized


def normalize_sina_raw_spot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Preserve fields that AKShare drops from its public Sina wrapper."""

    normalized = _normalize_columns(
        frame,
        _SINA_RAW_SPOT_ALIASES,
        required=("code", "name", "close", "pct_change", "volume", "amount", "turnover"),
    )
    return normalized[
        [
            "code",
            "name",
            "close",
            "pct_change",
            "volume",
            "amount",
            "turnover",
            "pe_ratio",
            "pb_ratio",
        ]
    ].copy()


def normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = _normalize_columns(
        frame,
        _HISTORY_ALIASES,
        required=("date", "close", "volume"),
    )
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
    normalized["volume"] = pd.to_numeric(normalized["volume"], errors="coerce")
    if "amount" in normalized.columns:
        normalized["amount"] = pd.to_numeric(normalized["amount"], errors="coerce")
    normalized = (
        normalized.dropna(subset=["date", "close", "volume"])
        .sort_values("date")
        .drop_duplicates(subset=["date"], keep="last")
    )
    return normalized.reset_index(drop=True)


def apply_spot_filters(frame: pd.DataFrame, config: ScreeningConfig) -> pd.DataFrame:
    """Apply deterministic, explainable market-wide hard filters."""

    spot = normalize_spot_frame(frame)
    mask = (
        spot["code"].map(is_main_board_code)
        & ~spot["name"].map(is_excluded_name)
        & spot["close"].between(config.min_price, config.max_price, inclusive="both")
        & spot["pct_change"].between(
            config.min_daily_pct,
            config.max_daily_pct,
            inclusive="both",
        )
        & spot["turnover"].between(
            config.min_turnover_pct,
            config.max_turnover_pct,
            inclusive="both",
        )
        & (spot["volume"] > 0)
        & (spot["amount"] >= config.min_amount_yuan)
    )
    filtered = spot.loc[mask].copy()
    if filtered.empty:
        return filtered

    # Prefer liquid, normally traded shares before making expensive history calls.
    amount_rank = filtered["amount"].rank(pct=True)
    turnover_center = 1.0 - ((filtered["turnover"] - 3.0).abs() / 9.0).clip(0, 1)
    daily_center = 1.0 - ((filtered["pct_change"] - 1.0).abs() / 7.0).clip(0, 1)
    filtered["pre_score"] = amount_rank * 60.0 + turnover_center * 25.0 + daily_center * 15.0
    return filtered.sort_values(
        ["pre_score", "amount"],
        ascending=[False, False],
    ).head(config.preselect_limit)


def _active_spot_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Return rows whose intraday quote has actually started trading."""

    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    amount = pd.to_numeric(frame["amount"], errors="coerce")
    return frame.loc[(close > 0) & (volume > 0) & (amount > 0)].copy()


def calculate_history_metrics(
    frame: pd.DataFrame,
    *,
    min_rows: int = 20,
    reference_price: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    history = normalize_history_frame(frame)
    as_of = now or datetime.now(CN_TZ)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=CN_TZ)
    else:
        as_of = as_of.astimezone(CN_TZ)

    # Many free A-share history APIs expose today's unfinished bar during the
    # trading session. It must not be treated as a completed daily candle.
    last_date = history["date"].iloc[-1].date() if not history.empty else None
    is_intraday = (
        last_date == as_of.date()
        and time(9, 15) <= as_of.time().replace(tzinfo=None) < time(15, 5)
    )
    if is_intraday:
        history = history.loc[history["date"].dt.date < as_of.date()].copy()

    if len(history) < max(min_rows, 20):
        return None

    close = history["close"]
    volume = history["volume"]
    amount = history["amount"] if "amount" in history.columns else None
    last_close = (
        float(reference_price)
        if is_intraday and reference_price is not None and reference_price > 0
        else float(close.iloc[-1])
    )
    base_index = -5 if is_intraday else -6
    base_close = float(close.iloc[base_index])
    if base_close <= 0:
        return None

    if is_intraday:
        volume_ratio = math.nan
    else:
        previous_volume_mean = float(volume.iloc[-6:-1].mean())
        volume_ratio = (
            float(volume.iloc[-1]) / previous_volume_mean
            if previous_volume_mean > 0
            else math.nan
        )
    avg_amount_20d = None
    if amount is not None:
        amount_window = amount.tail(20).dropna()
        if len(amount_window) >= 10:
            avg_amount_20d = float(amount_window.mean())

    return {
        "history_close": last_close,
        "five_day_pct": (last_close / base_close - 1.0) * 100.0,
        "ma5": float(close.tail(5).mean()),
        "ma10": float(close.tail(10).mean()),
        "ma20": float(close.tail(20).mean()),
        "volume_ratio_5d": volume_ratio,
        "avg_amount_20d": avg_amount_20d,
        "is_intraday": is_intraday,
    }


def _bounded_score(value: float, low: float, ideal: float, high: float) -> float:
    if value <= low or value >= high:
        return 0.0
    if value <= ideal:
        return (value - low) / (ideal - low)
    return (high - value) / (high - ideal)


def _optional_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def build_candidate(
    spot_row: Mapping[str, Any],
    metrics: Mapping[str, float],
    config: ScreeningConfig,
    evidence: Optional[Mapping[str, Any]] = None,
) -> Optional[ScreeningCandidate]:
    five_day_pct = float(metrics["five_day_pct"])
    if not config.min_five_day_pct <= five_day_pct <= config.max_five_day_pct:
        return None

    close = float(metrics["history_close"])
    ma5 = float(metrics["ma5"])
    ma10 = float(metrics["ma10"])
    ma20 = float(metrics["ma20"])
    volume_ratio = float(metrics["volume_ratio_5d"])
    is_intraday = bool(metrics.get("is_intraday", False))
    amount = float(spot_row["amount"])
    avg_amount_20d = _optional_number(metrics.get("avg_amount_20d"))
    if avg_amount_20d is not None and avg_amount_20d < config.min_amount_yuan:
        return None
    turnover = float(spot_row["turnover"])
    daily_pct = float(spot_row["pct_change"])
    amount_yi = amount / 100_000_000.0
    scorecard: V21ScoreCard = build_v21_scorecard(
        spot_row,
        metrics,
        evidence=evidence,
    )
    if scorecard.hard_reject:
        logger.info(
            "V2.1 风险门禁剔除 %s: %s",
            spot_row.get("code", ""),
            "；".join(scorecard.reject_reasons),
        )
        return None

    if close > ma5 > ma10 > ma20:
        trend_label = "均线多头"
    elif close > ma20 and ma5 >= ma10:
        trend_label = "趋势偏强"
    elif close > ma20:
        trend_label = "站上MA20"
    else:
        trend_label = "趋势待确认"

    reasons = [
        f"V2.1综合评分{scorecard.score:.2f}，证据覆盖率{scorecard.coverage_pct:.2f}%",
        f"成交额{amount_yi:.2f}亿元，满足流动性门槛",
        *scorecard.reasons,
    ]
    if avg_amount_20d is not None:
        reasons.append(
            f"近20日平均成交额{avg_amount_20d / 100_000_000.0:.2f}亿元，满足流动性门槛"
        )
    if -1.0 <= five_day_pct <= 8.0:
        reasons.append(f"近5日涨幅{five_day_pct:+.2f}%，未进入追高区间")
    risks = list(scorecard.risks)
    evidence_gaps = list(scorecard.evidence_gaps)
    if avg_amount_20d is None:
        evidence_gaps.append("历史数据未提供成交额，暂以当日成交额完成流动性初筛")
    if is_intraday:
        risks.append("盘中未完成K线不参与日线量能计算")

    pe_ratio = _optional_number(spot_row.get("pe_ratio"))
    pb_ratio = _optional_number(spot_row.get("pb_ratio"))

    return ScreeningCandidate(
        code=str(spot_row["code"]),
        name=str(spot_row["name"]),
        score=scorecard.score,
        raw_score=scorecard.raw_score,
        available_max_score=scorecard.available_max,
        score_coverage_pct=scorecard.coverage_pct,
        confidence_label=scorecard.confidence,
        score_breakdown={
            key: component.as_dict()
            for key, component in scorecard.components.items()
        },
        latest_price=round(float(spot_row["close"]), 2),
        daily_pct=round(daily_pct, 2),
        five_day_pct=round(five_day_pct, 2),
        amount_yi=round(amount_yi, 2),
        avg_amount_20d_yi=(
            round(avg_amount_20d / 100_000_000.0, 2)
            if avg_amount_20d is not None
            else None
        ),
        turnover_pct=round(turnover, 2),
        ma5=round(ma5, 2),
        ma10=round(ma10, 2),
        ma20=round(ma20, 2),
        volume_ratio_5d=round(volume_ratio, 2),
        trend_label=trend_label,
        watch_zone=(
            f"{min(ma20, ma5):.2f}—{max(ma20, ma5):.2f}"
            if ma20 > 0 and ma5 > 0
            else "无法确认"
        ),
        industry=str(spot_row.get("industry", "") or ""),
        pe_ratio=round(pe_ratio, 2) if pe_ratio is not None else None,
        pb_ratio=round(pb_ratio, 2) if pb_ratio is not None else None,
        historical_win_rate=None,
        risk_gate="需深度复核" if scorecard.evidence_gaps else "通过",
        reasons=tuple(reasons),
        risks=tuple(risks),
        evidence_gaps=tuple(evidence_gaps),
        trigger_conditions=tuple(scorecard.trigger_conditions),
        abandon_conditions=tuple(scorecard.abandon_conditions),
    )


class PublicMarketDataSource:
    """Free-data adapter with explicit fallback errors."""

    name = "AKShare/东方财富；失败时使用新浪原始接口或 efinance"

    def __init__(self) -> None:
        self._fundamental_manager = None
        self._fundamental_manager_lock = Lock()

    def _get_fundamental_manager(self):
        if self._fundamental_manager is not None:
            return self._fundamental_manager
        with self._fundamental_manager_lock:
            if self._fundamental_manager is None:
                from data_provider.base import DataFetcherManager

                self._fundamental_manager = DataFetcherManager()
        return self._fundamental_manager

    def fetch_spot(self) -> pd.DataFrame:
        errors: List[str] = []
        try:
            import akshare as ak

            frame = ak.stock_zh_a_spot_em()
            if frame is not None and not frame.empty:
                return frame
            errors.append("AKShare 返回空表")
        except Exception as exc:  # pragma: no cover - live provider
            errors.append(f"AKShare: {exc}")

        # AKShare 的 stock_zh_a_spot() 会解析新浪的 turnoverratio，却在
        # 返回 DataFrame 前删除该列。这里直接读取同一原始接口并保留换手率，
        # 否则基础风险过滤无法执行。
        try:
            frame = self._fetch_sina_spot_with_turnover()
            if not frame.empty:
                return frame
            errors.append("新浪原始接口返回空表")
        except Exception as exc:  # pragma: no cover - live provider
            errors.append(f"新浪原始接口: {exc}")

        try:
            import efinance as ef

            frame = ef.stock.get_realtime_quotes()
            if frame is not None and not frame.empty:
                return frame
            errors.append("efinance 返回空表")
        except Exception as exc:  # pragma: no cover - live provider
            errors.append(f"efinance: {exc}")

        raise RuntimeError("全市场实时行情获取失败；" + "；".join(errors))

    @staticmethod
    def _fetch_sina_spot_with_turnover() -> pd.DataFrame:
        import requests
        from akshare.stock.cons import (
            zh_sina_a_stock_count_url,
            zh_sina_a_stock_payload,
            zh_sina_a_stock_url,
        )
        from akshare.utils import demjson

        with requests.Session() as session:
            count_response = session.get(zh_sina_a_stock_count_url, timeout=15)
            count_response.raise_for_status()
            matches = re.findall(r"\d+", count_response.text)
            if not matches:
                raise ValueError("新浪接口未返回股票数量")
            page_count = math.ceil(int(matches[0]) / 80)

            frames: List[pd.DataFrame] = []
            payload = zh_sina_a_stock_payload.copy()
            for page in range(1, page_count + 1):
                payload["page"] = page
                response = session.get(
                    zh_sina_a_stock_url,
                    params=payload,
                    timeout=15,
                )
                response.raise_for_status()
                rows = demjson.decode(response.text)
                if rows:
                    frames.append(pd.DataFrame(rows))

        if not frames:
            raise ValueError("新浪接口未返回行情记录")
        return normalize_sina_raw_spot_frame(
            pd.concat(frames, ignore_index=True)
        )

    def fetch_history(self, code: str) -> pd.DataFrame:
        end = datetime.now(CN_TZ).date()
        start = end - timedelta(days=120)
        errors: List[str] = []

        try:
            import akshare as ak

            frame = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",
            )
            if frame is not None and not frame.empty:
                return frame
            errors.append("AKShare 日线为空")
        except Exception as exc:  # pragma: no cover - live provider
            errors.append(f"AKShare: {exc}")

        try:
            import efinance as ef

            frame = ef.stock.get_quote_history(
                code,
                beg=start.strftime("%Y%m%d"),
                end=end.strftime("%Y%m%d"),
                klt=101,
                fqt=1,
            )
            if frame is not None and not frame.empty:
                return frame
            errors.append("efinance 日线为空")
        except Exception as exc:  # pragma: no cover - live provider
            errors.append(f"efinance: {exc}")

        raise RuntimeError("；".join(errors))

    def fetch_evidence(
        self,
        code: str,
        *,
        budget_seconds: float,
    ) -> Mapping[str, Any]:
        """Reuse the existing fail-open fundamental and capital-flow pipeline."""
        manager = self._get_fundamental_manager()
        return manager.get_fundamental_context(
            code,
            budget_seconds=budget_seconds,
        )


class MarketScreener:
    def __init__(
        self,
        config: Optional[ScreeningConfig] = None,
        data_source: Optional[PublicMarketDataSource] = None,
    ) -> None:
        self.config = config or ScreeningConfig()
        self.data_source = data_source or PublicMarketDataSource()

    def run(
        self,
        *,
        spot_frame: Optional[pd.DataFrame] = None,
        history_fetcher: Optional[Callable[[str], pd.DataFrame]] = None,
        evidence_fetcher: Optional[Callable[[str], Mapping[str, Any]]] = None,
    ) -> ScreeningResult:
        raw_spot = spot_frame if spot_frame is not None else self.data_source.fetch_spot()
        universe_count = len(raw_spot)
        normalized_spot = normalize_spot_frame(raw_spot)
        active_spot = _active_spot_rows(normalized_spot)
        if active_spot.empty:
            market_environment = {
                "score": None,
                "strategy": "盘前或实时行情尚未形成，等待开盘后重新筛选",
                "coverage": "unavailable",
                "coverage_note": "全市场快照没有有效成交量和成交额，本次结果不能解读为无候选",
                "advance_ratio_pct": None,
                "median_pct_change": None,
                "limit_up_count": None,
                "limit_down_count": None,
                "snapshot_status": "pre_open_or_unavailable",
                "active_quote_count": 0,
            }
        else:
            market_environment = {
                **calculate_market_environment(active_spot),
                "snapshot_status": "active",
                "active_quote_count": len(active_spot),
            }
        filtered = apply_spot_filters(raw_spot, self.config)
        fetch_history = history_fetcher or self.data_source.fetch_history

        candidate_inputs: List[
            tuple[Mapping[str, Any], Mapping[str, Any], ScreeningCandidate]
        ] = []
        failures = 0
        success = 0
        evidence_success = 0
        evidence_failures = 0
        intraday_mode = False

        rows = [row._asdict() for row in filtered.itertuples(index=False)]
        with ThreadPoolExecutor(max_workers=self.config.history_workers) as executor:
            futures = {
                executor.submit(fetch_history, str(row["code"])): row
                for row in rows
            }
            for future in as_completed(futures):
                row = futures[future]
                try:
                    metrics = calculate_history_metrics(
                        future.result(),
                        min_rows=self.config.min_history_rows,
                        reference_price=float(row["close"]),
                    )
                    if metrics is None:
                        failures += 1
                        continue
                    intraday_mode = intraday_mode or bool(
                        metrics.get("is_intraday", False)
                    )
                    success += 1
                    candidate = build_candidate(row, metrics, self.config)
                    if candidate is not None:
                        candidate_inputs.append((row, metrics, candidate))
                except Exception as exc:
                    failures += 1
                    logger.warning("历史数据获取或计算失败 %s: %s", row["code"], exc)

        # Expensive fundamentals and capital-flow requests are limited to the
        # strongest transparent price/volume candidates.
        candidate_inputs = sorted(
            candidate_inputs,
            key=lambda item: (
                item[2].score,
                item[2].score_coverage_pct,
                item[2].amount_yi,
            ),
            reverse=True,
        )[: self.config.enrichment_limit]

        evidence_by_code: Dict[str, Mapping[str, Any]] = {}
        should_fetch_evidence = evidence_fetcher is not None or spot_frame is None
        if should_fetch_evidence and candidate_inputs:
            if evidence_fetcher is None:
                fetch_evidence = lambda code: self.data_source.fetch_evidence(
                    code,
                    budget_seconds=self.config.evidence_budget_seconds,
                )
            else:
                fetch_evidence = evidence_fetcher
            with ThreadPoolExecutor(max_workers=self.config.evidence_workers) as executor:
                evidence_futures = {
                    executor.submit(fetch_evidence, str(row["code"])): str(row["code"])
                    for row, _, _ in candidate_inputs
                }
                for future in as_completed(evidence_futures):
                    code = evidence_futures[future]
                    try:
                        payload = future.result()
                        if not isinstance(payload, Mapping):
                            raise TypeError("evidence payload must be a mapping")
                        evidence_by_code[code] = payload
                        if payload and str(payload.get("status", "")).lower() != "not_supported":
                            evidence_success += 1
                        else:
                            evidence_failures += 1
                    except Exception as exc:
                        evidence_failures += 1
                        logger.warning("V2.1 证据增强失败 %s: %s", code, exc)
                        evidence_by_code[code] = {}

        candidates: List[ScreeningCandidate] = []
        for row, metrics, _ in candidate_inputs:
            candidate = build_candidate(
                row,
                metrics,
                self.config,
                evidence=evidence_by_code.get(str(row["code"]), {}),
            )
            if candidate is not None:
                candidates.append(candidate)

        market_score = _optional_number(market_environment.get("score"))
        if market_environment.get("snapshot_status") != "active":
            observation_limit = 0
        elif market_score is not None and market_score < 40.0:
            observation_limit = min(self.config.top_n, 3)
        elif market_score is not None and market_score < 55.0:
            observation_limit = min(self.config.top_n, 4)
        else:
            observation_limit = self.config.top_n
        market_environment = {
            **market_environment,
            "observation_limit": observation_limit,
        }

        ranked = sorted(
            candidates,
            key=lambda item: (
                item.score,
                item.score_coverage_pct,
                item.amount_yi,
            ),
            reverse=True,
        )[:observation_limit]
        analysis_codes = [item.code for item in ranked[: self.config.analysis_limit]]
        limitations: List[str] = [
            "V2.1综合评分覆盖基本面、资金面、技术面和估值；行业催化评分将在后续阶段补充。",
            "证据覆盖率单独展示；缺失数据不会按正面信号计分，也不会被表述为无风险。",
            "候选名单用于缩小人工复核范围，不代表买入、加仓或建仓建议。",
            "免费行情接口可能延迟或失败；历史数据失败的股票会被跳过并计数。",
            "历史类似信号胜率和5/10/20日表现将在V2.2积累足够样本后展示。",
            "重大公告、监管处罚和异常事项仍由候选股深度分析继续复核。",
        ]
        if intraday_mode:
            limitations.append(
                "盘中运行时，当日未完成K线不参与日线均线和量能计算；"
                "最新价仅用于判断相对上一完整交易日均线的位置。"
            )
        return ScreeningResult(
            generated_at=datetime.now(CN_TZ).isoformat(timespec="seconds"),
            universe_count=universe_count,
            spot_filtered_count=len(filtered),
            history_success_count=success,
            history_failure_count=failures,
            evidence_success_count=evidence_success,
            evidence_failure_count=evidence_failures,
            candidates=tuple(ranked),
            analysis_codes=tuple(analysis_codes),
            config=self.config,
            data_source=self.data_source.name,
            limitations=tuple(limitations),
            model_version="V2.1",
            market_environment=market_environment,
        )


def render_markdown(result: ScreeningResult) -> str:
    generated = datetime.fromisoformat(result.generated_at)
    market = result.market_environment
    market_score = market.get("score")
    lines = [
        f"# A股主板全市场初筛（{result.model_version}）",
        "",
        f"> 生成时间：{generated.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> 数据来源：{result.data_source}",
        "> 定位：观察名单，不代表买入、加仓或建仓建议。",
        "",
        "## 市场环境",
        "",
        f"- 环境评分：{market_score if market_score is not None else '无法确认'}",
        f"- 策略：{market.get('strategy', '保持谨慎')}",
        f"- 本次观察名单上限：{market.get('observation_limit', result.config.top_n)}只",
        f"- 评分覆盖：{market.get('coverage', 'partial')}（{market.get('coverage_note', '数据范围待复核')}）",
        f"- 上涨家数占比：{market.get('advance_ratio_pct') if market.get('advance_ratio_pct') is not None else '无法确认'}%",
        f"- 涨跌幅中位数：{market.get('median_pct_change') if market.get('median_pct_change') is not None else '无法确认'}%",
        f"- 涨停/跌停数量：{market.get('limit_up_count') if market.get('limit_up_count') is not None else '无法确认'} / "
        f"{market.get('limit_down_count') if market.get('limit_down_count') is not None else '无法确认'}",
        "",
        "## 筛选概况",
        "",
        f"- 全市场记录：{result.universe_count}",
        f"- 通过基础过滤并进入历史核验：{result.spot_filtered_count}",
        f"- 历史数据有效：{result.history_success_count}",
        f"- 历史数据失败或不足：{result.history_failure_count}",
        f"- 基本面/资金证据增强成功：{result.evidence_success_count}",
        f"- 基本面/资金证据增强失败或不支持：{result.evidence_failure_count}",
        f"- 最终观察候选：{len(result.candidates)}",
        "",
        "## 观察候选",
        "",
    ]
    if not result.candidates:
        if market.get("snapshot_status") == "pre_open_or_unavailable":
            lines.extend(
                [
                    "盘前行情尚未形成，或实时快照没有有效成交量和成交额。",
                    "本次筛选未取得可用盘中数据，0只候选不能解读为市场没有观察机会；请开盘后重新运行。",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "本次没有股票同时满足全部门槛。系统不会为了凑数而降低标准。",
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "| 排名 | 代码 | 名称 | 综合评分 | 证据覆盖 | 置信度 | 基本面 | 催化 | 资金面 | 技术面 | 估值 | 历史胜率 |",
                "|---:|---|---|---:|---:|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for index, candidate in enumerate(result.candidates, start=1):
            breakdown = candidate.score_breakdown
            lines.append(
                "| {rank} | {code} | {name} | {score:.2f} | {coverage:.2f}% | {confidence} | "
                "{fundamental:.2f}/30 | {catalyst:.2f}/20 | {capital:.2f}/20 | {technical:.2f}/20 | "
                "{valuation:.2f}/10 | {win_rate} |".format(
                    rank=index,
                    code=candidate.code,
                    name=candidate.name,
                    score=candidate.score,
                    coverage=candidate.score_coverage_pct,
                    confidence=candidate.confidence_label,
                    fundamental=float(breakdown["fundamental"]["score"]),
                    catalyst=float(breakdown["industry_catalyst"]["score"]),
                    capital=float(breakdown["capital"]["score"]),
                    technical=float(breakdown["technical"]["score"]),
                    valuation=float(breakdown["valuation"]["score"]),
                    win_rate=(
                        f"{candidate.historical_win_rate:.2f}%"
                        if candidate.historical_win_rate is not None
                        else "待V2.2积累"
                    ),
                )
            )
        lines.append("")
        for candidate in result.candidates:
            lines.extend(
                [
                    f"### {candidate.code} {candidate.name}",
                    "",
                    f"- 综合评分：{candidate.score:.2f}",
                    f"- 证据覆盖率：{candidate.score_coverage_pct:.2f}%（{candidate.confidence_label}置信度）",
                    f"- 最新价/当日涨跌：{candidate.latest_price:.2f} / {candidate.daily_pct:+.2f}%",
                    f"- 近5日涨跌：{candidate.five_day_pct:+.2f}%",
                    f"- 成交额/换手率：{candidate.amount_yi:.2f}亿元 / {candidate.turnover_pct:.2f}%",
                    (
                        f"- 近20日平均成交额：{candidate.avg_amount_20d_yi:.2f}亿元"
                        if candidate.avg_amount_20d_yi is not None
                        else "- 近20日平均成交额：历史接口未提供，需复核"
                    ),
                    f"- 技术结构：{candidate.trend_label}",
                    f"- 技术观察带：{candidate.watch_zone}（MA20—MA5，仅用于复核，不是买入区间）",
                    f"- 风险门禁：{candidate.risk_gate}",
                    "- 历史类似信号：V2.2尚未积累足够样本，不输出虚构胜率",
                    "",
                    "**入选依据**",
                    "",
                    *[f"- {reason}" for reason in candidate.reasons],
                ]
            )
            if candidate.risks:
                lines.extend(
                    [
                        "",
                        "**待核验风险**",
                        "",
                        *[f"- {risk}" for risk in candidate.risks],
                    ]
                )
            if candidate.evidence_gaps:
                lines.extend(
                    [
                        "",
                        "**证据缺口**",
                        "",
                        *[f"- {gap}" for gap in candidate.evidence_gaps],
                    ]
                )
            lines.extend(
                [
                    "",
                    "**关注触发条件**",
                    "",
                    *[f"- {condition}" for condition in candidate.trigger_conditions],
                    "",
                    "**放弃条件**",
                    "",
                    *[f"- {condition}" for condition in candidate.abandon_conditions],
                ]
            )
            lines.append("")

    lines.extend(
        [
            "## 下一步深度分析代码",
            "",
            ", ".join(result.analysis_codes) if result.analysis_codes else "无",
            "",
            "## 证据边界",
            "",
            *[f"- {item}" for item in result.limitations],
            "",
        ]
    )
    return "\n".join(lines)


def save_result(
    result: ScreeningResult,
    *,
    report_path: Path,
    json_path: Path,
    codes_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    codes_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            _strict_json_value(result.as_dict()),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    codes_path.write_text(",".join(result.analysis_codes), encoding="utf-8")
