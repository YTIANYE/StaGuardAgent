"""指标域模型：指标目录、时序点、时序视图、数据质量报告。

设计要点：任何数据源（本地文件 / Mock HTTP / 未来的 Prometheus）都必须先归一化成
`MetricPoint`，规则引擎只认这一种形状，从而做到数据源可插拔。
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import Confidence


class MetricName(StrEnum):
    """巡检指标目录。业务层对齐题目「QPS / 成功率 / 错误数 / 延时」，资源层对齐「CPU / 内存 / 连接数」。"""

    # ---- 业务层 ----
    QPS = "qps"
    SUCCESS_RATE = "success_rate"
    ERROR_COUNT = "error_count"
    BUSINESS_ERROR_RATE = "business_error_rate"
    # ---- 性能层 ----
    LATENCY_P50 = "latency_p50"
    LATENCY_P95 = "latency_p95"
    LATENCY_P99 = "latency_p99"
    # ---- 资源层 ----
    CPU_USAGE = "cpu_usage"
    MEM_USAGE = "mem_usage"
    CONN_USAGE = "conn_usage"

    @property
    def unit(self) -> str:
        return METRIC_UNIT[self]

    @property
    def label(self) -> str:
        return METRIC_LABEL[self]

    @property
    def higher_is_better(self) -> bool:
        return self is MetricName.SUCCESS_RATE

    def worse_value(self, values: list[float]) -> float:
        """取「更坏」方向的代表值：延时/负载取最大，成功率取最小。"""
        return min(values) if self.higher_is_better else max(values)


METRIC_UNIT: dict[MetricName, str] = {
    MetricName.QPS: "req/s",
    MetricName.SUCCESS_RATE: "%",
    MetricName.ERROR_COUNT: "count",
    MetricName.BUSINESS_ERROR_RATE: "%",
    MetricName.LATENCY_P50: "ms",
    MetricName.LATENCY_P95: "ms",
    MetricName.LATENCY_P99: "ms",
    MetricName.CPU_USAGE: "%",
    MetricName.MEM_USAGE: "%",
    MetricName.CONN_USAGE: "%",
}

METRIC_LABEL: dict[MetricName, str] = {
    MetricName.QPS: "QPS",
    MetricName.SUCCESS_RATE: "成功率",
    MetricName.ERROR_COUNT: "错误数",
    MetricName.BUSINESS_ERROR_RATE: "业务错误率",
    MetricName.LATENCY_P50: "P50 延时",
    MetricName.LATENCY_P95: "P95 延时",
    MetricName.LATENCY_P99: "P99 延时",
    MetricName.CPU_USAGE: "CPU 使用率",
    MetricName.MEM_USAGE: "内存使用率",
    MetricName.CONN_USAGE: "连接池使用率",
}


class MetricPoint(BaseModel):
    """标准化后的单个指标点。"""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    service: str
    instance: str
    metric: MetricName
    value: float

    @property
    def series_key(self) -> str:
        return f"{self.service}/{self.instance}/{self.metric.value}"


class MetricSeries(BaseModel):
    """一条时序（service + instance + metric）。规则引擎的统计基元。"""

    service: str
    instance: str
    metric: MetricName
    points: list[tuple[datetime, float]] = Field(default_factory=list)
    unit: str = ""

    @property
    def key(self) -> str:
        return f"{self.service}/{self.instance}/{self.metric.value}"

    @property
    def is_empty(self) -> bool:
        return not self.points

    def values(self) -> list[float]:
        return [v for _, v in self.points]

    def timestamps(self) -> list[datetime]:
        return [t for t, _ in self.points]

    def latest(self) -> tuple[datetime, float] | None:
        return self.points[-1] if self.points else None

    def mean(self) -> float:
        vals = self.values()
        return statistics.fmean(vals) if vals else math.nan

    def stdev(self) -> float:
        vals = self.values()
        return statistics.pstdev(vals) if len(vals) > 1 else 0.0

    def percentile(self, q: float) -> float:
        return percentile(self.values(), q)

    def worst(self) -> float:
        """窗口内「最坏」的值，异常判定与报告展示都用它。"""
        vals = self.values()
        if not vals:
            return math.nan
        return self.metric.worse_value(vals)

    def cv(self) -> float:
        """变异系数，用于响应抖动判定；均值为 0 时退化为 0。"""
        m = self.mean()
        if not m or math.isnan(m):
            return 0.0
        return self.stdev() / abs(m)

    def slope(self, per_minute: bool = True) -> float:
        """最小二乘斜率。per_minute=True 时把横轴换算成分钟，便于解读「每小时涨多少」。"""
        n = len(self.points)
        if n < 3:
            return 0.0
        ts0 = self.points[0][0]
        xs = [(t - ts0).total_seconds() / (60.0 if per_minute else 1.0) for t, _ in self.points]
        ys = self.values()
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            return 0.0
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / denom

    def trend_stats(self) -> tuple[float, float, float]:
        """最小二乘趋势的 (斜率(每分钟), t 统计量, R²)。

        趋势型规则**必须**带显著性检验：一段 30 个点的噪声序列，随手算出来的斜率
        很容易「看似」有明显涨幅，但 t 统计量只有 1 出头，说明它完全可能是噪声。
        没有这道闸门的趋势规则在干净数据上会稳定误报——这是最容易踩的坑。
        """
        n = len(self.points)
        if n < 3:
            return 0.0, 0.0, 0.0
        ts0 = self.points[0][0]
        xs = [(t - ts0).total_seconds() / 60.0 for t, _ in self.points]
        ys = self.values()
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx == 0:
            return 0.0, 0.0, 0.0
        sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
        slope = sxy / sxx
        intercept = my - slope * mx
        residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys, strict=True)]
        sse = sum(r * r for r in residuals)
        sst = sum((y - my) ** 2 for y in ys)
        r2 = 0.0 if sst == 0 else max(0.0, 1.0 - sse / sst)
        dof = n - 2
        if dof <= 0 or sse == 0:
            return slope, float("inf") if slope else 0.0, r2
        se_slope = math.sqrt(sse / dof / sxx)
        t_stat = slope / se_slope if se_slope else 0.0
        return slope, t_stat, r2

    def slope_pct_per_hour(self) -> float:
        """相对序列中位数归一化后的每小时涨幅（%）。趋势型规则的可比口径。"""
        slope, _, _ = self.trend_stats()
        reference = statistics.median(self.values()) if self.points else 0.0
        if not reference:
            return 0.0
        return slope * 60.0 / abs(reference) * 100.0

    def first(self) -> tuple[datetime, float] | None:
        return self.points[0] if self.points else None

    def window(self) -> tuple[datetime, datetime] | None:
        if not self.points:
            return None
        return self.points[0][0], self.points[-1][0]

    def digest(self) -> dict[str, Any]:
        """给 AI 证据包用的紧凑统计摘要（不投喂原始时序，省 token）。"""
        vals = self.values()
        if not vals:
            return {"samples": 0}
        return {
            "samples": len(vals),
            "min": round(min(vals), 3),
            "p50": round(percentile(vals, 50), 3),
            "p95": round(percentile(vals, 95), 3),
            "max": round(max(vals), 3),
            "mean": round(statistics.fmean(vals), 3),
            "first": round(vals[0], 3),
            "last": round(vals[-1], 3),
            "trend": self.trend_label(),
            "unit": self.unit,
        }

    def trend_label(self) -> str:
        slope = self.slope()
        span = max(self.values()) - min(self.values()) if self.values() else 0.0
        # 斜率相对波动幅度过小时视为平稳，避免把噪声说成趋势
        if span == 0 or abs(slope) * 5 < span * 0.2:
            return "stable"
        return "rising" if slope > 0 else "falling"


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数。依赖 statistics.quantiles 会在小样本上失真，故手写。"""
    if not values:
        return math.nan
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    pos = (len(ordered) - 1) * (q / 100.0)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[int(pos)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


class DataQualityIssue(BaseModel):
    """数据质量问题。巡检结论的置信度由它决定——数据不可信时降置信度，而不是硬报异常。"""

    kind: Literal["missing", "out_of_range", "stale", "interpolated", "negative"]
    service: str
    instance: str
    metric: MetricName
    detail: str


class DataQualityReport(BaseModel):
    total_points: int = 0
    expected_points: int = 0
    missing_ratio: float = 0.0
    interpolated_ratio: float = 0.0
    issues: list[DataQualityIssue] = Field(default_factory=list)
    confidence: Confidence = Confidence.HIGH

    @property
    def summary(self) -> str:
        if not self.issues:
            return "数据质量良好，未发现缺失或越界"
        kinds: dict[str, int] = {}
        for issue in self.issues:
            kinds[issue.kind] = kinds.get(issue.kind, 0) + 1
        detail = "、".join(f"{k}×{v}" for k, v in sorted(kinds.items()))
        return f"缺失率 {self.missing_ratio:.2%}，检出问题：{detail}"

    def grade(self) -> Confidence:
        """按缺失率与越界问题数给出置信度。"""
        if self.missing_ratio > 0.15 or len(self.issues) > 40:
            return Confidence.LOW
        if self.missing_ratio > 0.03 or len(self.issues) > 10:
            return Confidence.MEDIUM
        return Confidence.HIGH
