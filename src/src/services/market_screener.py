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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


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
    "turnover": ("换手率", "换手率(%)", "turnover", "turnover_rate"),
}

_HISTORY_ALIASES: Mapping[str, Sequence[str]] = {
    "date": ("日期", "date", "时间"),
    "close": ("收盘", "收盘价", "close"),
    "volume": ("成交量", "volume"),
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
    min_amount_yuan: float = 100_000_000.0
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


@dataclass(frozen=True)
class ScreeningCandidate:
    code: str
    name: str
    score: float
    latest_price: float
    daily_pct: float
    five_day_pct: float
    amount_yi: float
    turnover_pct: float
    ma5: float
    ma10: float
    ma20: float
    volume_ratio_5d: float
    trend_label: str
    reasons: Sequence[str]
    risks: Sequence[str]

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["reasons"] = list(self.reasons)
        value["risks"] = list(self.risks)
        return value


@dataclass(frozen=True)
class ScreeningResult:
    generated_at: str
    universe_count: int
    spot_filtered_count: int
    history_success_count: int
    history_failure_count: int
    candidates: Sequence[ScreeningCandidate]
    analysis_codes: Sequence[str]
    config: ScreeningConfig
    data_source: str
    limitations: Sequence[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "universe_count": self.universe_count,
            "spot_filtered_count": self.spot_filtered_count,
            "history_success_count": self.history_success_count,
            "history_failure_count": self.history_failure_count,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "analysis_codes": list(self.analysis_codes),
            "config": asdict(self.config),
            "data_source": self.data_source,
            "limitations": list(self.limitations),
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
    for column in ("close", "pct_change", "volume", "amount", "turnover"):
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    return normalized


def normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = _normalize_columns(
        frame,
        _HISTORY_ALIASES,
        required=("date", "close", "volume"),
    )
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
    normalized["volume"] = pd.to_numeric(normalized["volume"], errors="coerce")
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
    return {
        "history_close": last_close,
        "five_day_pct": (last_close / base_close - 1.0) * 100.0,
        "ma5": float(close.tail(5).mean()),
        "ma10": float(close.tail(10).mean()),
        "ma20": float(close.tail(20).mean()),
        "volume_ratio_5d": volume_ratio,
        "is_intraday": is_intraday,
    }


def _bounded_score(value: float, low: float, ideal: float, high: float) -> float:
    if value <= low or value >= high:
        return 0.0
    if value <= ideal:
        return (value - low) / (ideal - low)
    return (high - value) / (high - ideal)


def build_candidate(
    spot_row: Mapping[str, Any],
    metrics: Mapping[str, float],
    config: ScreeningConfig,
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
    turnover = float(spot_row["turnover"])
    daily_pct = float(spot_row["pct_change"])

    reasons: List[str] = []
    risks: List[str] = []
    score = 0.0

    # Liquidity (25): amount is objective and available for the whole market.
    amount_yi = amount / 100_000_000.0
    score += min(25.0, 8.0 + math.log10(max(amount_yi, 1.0)) * 12.0)
    reasons.append(f"成交额{amount_yi:.2f}亿元，满足流动性门槛")

    # Five-day movement (25): moderate strength is preferred; chasing is penalized.
    five_day_component = _bounded_score(five_day_pct, -5.0, 4.0, 12.0) * 25.0
    score += five_day_component
    if -1.0 <= five_day_pct <= 8.0:
        reasons.append(f"近5日涨幅{five_day_pct:+.2f}%，未进入追高区间")
    elif five_day_pct > 8.0:
        risks.append(f"近5日已上涨{five_day_pct:.2f}%，接近追高上限")
    else:
        risks.append(f"近5日回撤{abs(five_day_pct):.2f}%，趋势仍需确认")

    # Trend (30): transparent MA relationships, not an AI prediction.
    if close > ma5 > ma10 > ma20:
        score += 30.0
        trend_label = "均线多头"
        if is_intraday:
            reasons.append("最新价高于上一完整交易日MA5/MA10/MA20，均线保持多头排列")
        else:
            reasons.append("收盘价及MA5/MA10/MA20呈多头排列")
    elif close > ma20 and ma5 >= ma10:
        score += 21.0
        trend_label = "趋势偏强"
        reasons.append(
            "最新价位于上一完整交易日MA20上方，短期均线未转弱"
            if is_intraday
            else "价格位于MA20上方，短期均线未转弱"
        )
    elif close > ma20:
        score += 13.0
        trend_label = "站上MA20"
        risks.append("短期均线尚未形成多头排列")
    else:
        score += 3.0
        trend_label = "趋势待确认"
        risks.append("价格仍在MA20下方")

    # Turnover (10): reward a moderate band, not maximal turnover.
    score += _bounded_score(turnover, 0.5, 3.0, 12.0) * 10.0
    if turnover > 8.0:
        risks.append(f"换手率{turnover:.2f}%偏高，短线波动风险较大")

    # Volume (10): this is descriptive only. It never infers accumulation/washout.
    if math.isfinite(volume_ratio):
        score += _bounded_score(volume_ratio, 0.3, 1.15, 2.5) * 10.0
        if 0.7 <= volume_ratio <= 1.6:
            reasons.append(f"最新量能为近5日均量的{volume_ratio:.2f}倍，处于常见区间")
        elif volume_ratio > 2.0:
            risks.append(f"最新量能为近5日均量的{volume_ratio:.2f}倍，需核查放量原因")
        else:
            risks.append(f"最新量能为近5日均量的{volume_ratio:.2f}倍，量能偏低")

    if daily_pct > 4.0:
        risks.append(f"当日涨幅{daily_pct:+.2f}%，不宜依据初筛结果追涨")

    return ScreeningCandidate(
        code=str(spot_row["code"]),
        name=str(spot_row["name"]),
        score=round(score, 2),
        latest_price=round(float(spot_row["close"]), 2),
        daily_pct=round(daily_pct, 2),
        five_day_pct=round(five_day_pct, 2),
        amount_yi=round(amount_yi, 2),
        turnover_pct=round(turnover, 2),
        ma5=round(ma5, 2),
        ma10=round(ma10, 2),
        ma20=round(ma20, 2),
        volume_ratio_5d=round(volume_ratio, 2),
        trend_label=trend_label,
        reasons=tuple(reasons),
        risks=tuple(risks),
    )


class PublicMarketDataSource:
    """Free-data adapter with explicit fallback errors."""

    name = "AKShare/东方财富，失败时尝试 efinance"

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

        try:
            import efinance as ef

            frame = ef.stock.get_realtime_quotes()
            if frame is not None and not frame.empty:
                return frame
            errors.append("efinance 返回空表")
        except Exception as exc:  # pragma: no cover - live provider
            errors.append(f"efinance: {exc}")

        raise RuntimeError("全市场实时行情获取失败；" + "；".join(errors))

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
    ) -> ScreeningResult:
        raw_spot = spot_frame if spot_frame is not None else self.data_source.fetch_spot()
        universe_count = len(raw_spot)
        filtered = apply_spot_filters(raw_spot, self.config)
        fetch_history = history_fetcher or self.data_source.fetch_history

        candidates: List[ScreeningCandidate] = []
        failures = 0
        success = 0
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
                        candidates.append(candidate)
                except Exception as exc:
                    failures += 1
                    logger.warning("历史数据获取或计算失败 %s: %s", row["code"], exc)

        ranked = sorted(
            candidates,
            key=lambda item: (item.score, item.amount_yi),
            reverse=True,
        )[: self.config.top_n]
        analysis_codes = [item.code for item in ranked[: self.config.analysis_limit]]
        limitations: List[str] = [
            "初筛仅使用公开行情、成交与均线数据，尚未核验公告、财务和新闻。",
            "候选名单用于缩小人工复核范围，不代表买入、加仓或建仓建议。",
            "免费行情接口可能延迟或失败；历史数据失败的股票会被跳过并计数。",
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
            candidates=tuple(ranked),
            analysis_codes=tuple(analysis_codes),
            config=self.config,
            data_source=self.data_source.name,
            limitations=tuple(limitations),
        )


def render_markdown(result: ScreeningResult) -> str:
    generated = datetime.fromisoformat(result.generated_at)
    lines = [
        "# A股主板全市场初筛",
        "",
        f"> 生成时间：{generated.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）",
        f"> 数据来源：{result.data_source}",
        "",
        "## 筛选概况",
        "",
        f"- 全市场记录：{result.universe_count}",
        f"- 通过基础过滤并进入历史核验：{result.spot_filtered_count}",
        f"- 历史数据有效：{result.history_success_count}",
        f"- 历史数据失败或不足：{result.history_failure_count}",
        f"- 最终观察候选：{len(result.candidates)}",
        "",
        "## 观察候选",
        "",
    ]
    if not result.candidates:
        lines.extend(
            [
                "本次没有股票同时满足全部门槛。系统不会为了凑数而降低标准。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "| 排名 | 代码 | 名称 | 评分 | 最新价 | 当日涨跌 | 近5日 | 成交额(亿) | 换手率 | 趋势 |",
                "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for index, candidate in enumerate(result.candidates, start=1):
            lines.append(
                "| {rank} | {code} | {name} | {score:.2f} | {price:.2f} | "
                "{daily:+.2f}% | {five:+.2f}% | {amount:.2f} | {turnover:.2f}% | {trend} |".format(
                    rank=index,
                    code=candidate.code,
                    name=candidate.name,
                    score=candidate.score,
                    price=candidate.latest_price,
                    daily=candidate.daily_pct,
                    five=candidate.five_day_pct,
                    amount=candidate.amount_yi,
                    turnover=candidate.turnover_pct,
                    trend=candidate.trend_label,
                )
            )
        lines.append("")
        for candidate in result.candidates:
            lines.extend(
                [
                    f"### {candidate.code} {candidate.name}",
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
