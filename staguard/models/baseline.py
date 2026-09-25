"""动态基线模型。

阈值不该是拍脑袋的固定数字：业务有波峰波谷，同一接口在凌晨与晚高峰的 QPS 天差地别。
这里用「历史同时段（hour-of-week）稳健统计」构造基线：

    upper = median + k * 1.4826 * MAD

用中位数 + MAD 而不是均值 + 标准差，是因为基线样本里本身混着历史故障点，
均值和标准差会被离群值带偏，MAD 不会。样本不足时回退静态阈值并显式标注。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from .common import BaselineMode, Confidence
from .metric import MetricName

MAD_TO_SIGMA = 1.4826
"""正态分布下 MAD 与标准差的换算系数。"""


class Baseline(BaseModel):
    """一条时序的基线画像。"""

    service: str
    instance: str
    metric: MetricName

    median: float = math.nan
    mad: float = 0.0
    mad_scaled: float = 0.0
    mean: float = math.nan
    stdev: float = 0.0
    p50: float = math.nan
    p95: float = math.nan
    p99: float = math.nan
    min: float = math.nan
    max: float = math.nan

    sample_size: int = 0
    profile: dict[int, float] = Field(default_factory=dict)
    """hour-of-day -> 该时刻历史中位数，用于波峰波谷自适应。"""

    mode: BaselineMode = BaselineMode.MISSING
    static_min: float | None = None
    static_max: float | None = None
    static_k: float = 4.0
    """动态阈值系数：偏离超过 k 倍稳健标准差即判异常。"""

    @property
    def upper(self) -> float | None:
        if math.isnan(self.median) or self.mad_scaled <= 0:
            return self.static_max
        return self.median + self.static_k * self.mad_scaled

    @property
    def lower(self) -> float | None:
        if math.isnan(self.median) or self.mad_scaled <= 0:
            return self.static_min
        return self.median - self.static_k * self.mad_scaled

    @property
    def confidence(self) -> Confidence:
        if self.mode is not BaselineMode.DYNAMIC:
            return Confidence.LOW
        if self.sample_size >= 200:
            return Confidence.HIGH
        if self.sample_size >= 60:
            return Confidence.MEDIUM
        return Confidence.LOW

    @property
    def is_usable(self) -> bool:
        """基线是否可用于「动态」判定（样本量与离差都要够）。"""
        return self.mode is BaselineMode.DYNAMIC and self.sample_size >= 30 and self.mad_scaled > 0

    def median_at_hour(self, hour: int) -> float:
        """取该小时的历史中位数；无 profile 时退回全局中位数。"""
        return self.profile.get(hour, self.median)

    def z_score(self, value: float, hour: int | None = None) -> float:
        """稳健 Z 分数：(value - median) / (1.4826 * MAD)。除以 0 时返回 0。"""
        ref = self.median_at_hour(hour) if hour is not None else self.median
        denom = self.mad_scaled or self.stdev
        if not denom or math.isnan(denom) or math.isnan(ref):
            return 0.0
        return (value - ref) / denom

    def deviation_pct(self, value: float, hour: int | None = None) -> float:
        """相对基线的百分比偏离，报告里展示「实测 vs 基线」。

        成功率这类「越高越好」的指标，下跌要显示为负数（-3.2%），所以直接算差值比。
        """
        ref = self.median_at_hour(hour) if hour is not None else self.median
        if not ref or math.isnan(ref):
            return 0.0
        return (value - ref) / abs(ref) * 100.0

    def label(self) -> str:
        if self.mode is not BaselineMode.DYNAMIC:
            return f"静态阈值 min={self.static_min} max={self.static_max}"
        return (
            f"动态基线 median={self.median:.2f} mad={self.mad:.2f} "
            f"p95={self.p95:.2f} n={self.sample_size}"
        )


class BaselineSet(BaseModel):
    """一次巡检用到的全部基线，按 series key 索引。"""

    baselines: dict[str, Baseline] = Field(default_factory=dict)

    @staticmethod
    def key(service: str, instance: str, metric: MetricName) -> str:
        return f"{service}/{instance}/{metric.value}"

    def get(self, service: str, instance: str, metric: MetricName) -> Baseline:
        return self.baselines.get(self.key(service, instance, metric), Baseline(
            service=service, instance=instance, metric=metric,
        ))

    def put(self, baseline: Baseline) -> None:
        self.baselines[self.key(baseline.service, baseline.instance, baseline.metric)] = baseline

    def dynamic_ratio(self) -> float:
        """动态基线覆盖率，报告里的「基线健康度」指标。"""
        if not self.baselines:
            return 0.0
        dynamic = sum(1 for b in self.baselines.values() if b.mode is BaselineMode.DYNAMIC)
        return dynamic / len(self.baselines)
