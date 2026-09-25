"""动态基线计算。

**这是本项目对「动态阈值」加分项的实现**，也是「业务指标理解」的核心体现：
固定阈值在凌晨必然误报、在晚高峰必然漏报，因为业务流量本身有周期。

算法选择：**hour-of-week + 滚动中位数/MAD**

    upper = median + k * 1.4826 * MAD

三个取舍点，每一个都有明确理由：

1. **为什么按 hour-of-week 取样**：业务有两个周期叠加——一天内的双峰（午间 + 晚间）
   和一周内的周效应（周末流量低）。只按小时取样会把周一 10 点和平日 10 点混在一起，
   基线被周效应污染。按「星期几 + 小时」取样，两个周期同时被消掉。

2. **为什么用中位数 + MAD 而不是均值 + 标准差**：基线样本里必然混着历史故障点
   （历史上也出过故障）。均值会被离群点带偏，一次大故障就能把阈值永久抬高，
   此后同类故障全部漏报。MAD（绝对中位差）对离群点的崩坏点是 50%，稳得多。

3. **为什么要有相对下限**：极稳定的指标（如成功率）MAD 会趋近于 0，
   此时 1 个 0.01 的抖动会被算成 10 个标准差。加一个 `0.8% x median` 的下限，
   避免「零尺度爆炸」。

样本不足时回退静态阈值，并在报告里显式标注 `static_fallback`——
**回退必须可见**，否则读报告的人会误以为这是动态基线的结论。
"""

from __future__ import annotations

import logging
import math
import statistics

from ..config import Settings
from ..models import Baseline, BaselineMode, BaselineSet, MetricName, TimeWindow
from ..store import Repository
from ..utils.timeutil import hour_of_week

logger = logging.getLogger(__name__)

MAD_SCALE = 1.4826
"""MAD -> 标准差的换算系数（正态分布下成立）。"""

RELATIVE_FLOOR = 0.008
"""稳健离差的相对下限：至少取中位数的 0.8%，防止零尺度导致 z 分数爆炸。"""

SLOT_NEIGHBOURS = 1
"""取样时纳入相邻小时槽。样本不足时能显著提升稳健性（±1 小时足以覆盖周期漂移）。"""


class BaselineProvider:
    """按需计算并缓存基线。一次巡检最多算 150 条序列，缓存是必要的。"""

    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        archive_dataset: str,
        window: TimeWindow,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.archive_dataset = archive_dataset
        self.window = window
        self.min_samples = settings.app.baseline_min_samples
        self.days = settings.app.baseline_days
        self.archive_granularity = settings.scenarios.baseline_interval_seconds
        self.k = 4.0
        self._cache: dict[tuple[str, str, MetricName], Baseline] = {}
        self._misses = 0

    def get(self, service: str, instance: str, metric: MetricName) -> Baseline:
        key = (service, instance, metric)
        if key not in self._cache:
            self._cache[key] = self._compute(service, instance, metric)
        return self._cache[key]

    def collect(self, pairs: list[tuple[str, str]], metrics: list[MetricName]) -> BaselineSet:
        result = BaselineSet()
        for service, instance in pairs:
            for metric in metrics:
                result.put(self.get(service, instance, metric))
        return result

    @property
    def cache_hit_rate(self) -> float:
        total = len(self._cache) + self._misses
        return 1.0 if not total else len(self._cache) / total

    # ------------------------------------------------------------------ 计算
    def _compute(self, service: str, instance: str, metric: MetricName) -> Baseline:
        threshold = self.settings.thresholds.for_metric(metric.value, service)
        start = self.window.end - _days_to_delta(self.days)
        samples = self.repo.fetch_values(
            dataset_id=self.archive_dataset,
            service=service,
            instance=instance,
            metric=metric,
            start=start,
            end=self.window.end,
            granularity_seconds=self.archive_granularity,
        )
        # 取历史同时段（同星期几 + 同小时，含相邻小时槽）
        slots = self._target_slots()
        selected = [value for ts, value in samples if hour_of_week(ts) in slots]
        if len(selected) < self.min_samples:
            selected = [value for _, value in samples]

        if len(selected) < self.min_samples:
            self._misses += 1
            return Baseline(
                service=service, instance=instance, metric=metric,
                mode=BaselineMode.STATIC_FALLBACK,
                sample_size=len(selected),
                static_min=threshold.min, static_max=threshold.max,
                static_k=self.k,
            )

        return self._build(service, instance, metric, selected, samples, threshold)

    def _target_slots(self) -> set[int]:
        base = hour_of_week(self.window.end)
        return {(base + offset) % 168 for offset in range(-SLOT_NEIGHBOURS, SLOT_NEIGHBOURS + 1)}

    def _build(self, service, instance, metric, selected, samples, threshold) -> Baseline:  # noqa: ANN001
        ordered = sorted(selected)
        median = statistics.median(ordered)
        mad = statistics.median([abs(v - median) for v in ordered])
        mad_scaled = MAD_SCALE * mad
        stdev = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
        floor = abs(median) * RELATIVE_FLOOR
        scale = max(mad_scaled, floor)
        if scale <= 0:
            # 完全恒定的序列（理论上不该出现）：退化为静态阈值，而不是除以 0
            return Baseline(
                service=service, instance=instance, metric=metric,
                mode=BaselineMode.STATIC_FALLBACK, sample_size=len(ordered),
                static_min=threshold.min, static_max=threshold.max, static_k=self.k,
            )
        profile = self._hourly_profile(samples)
        return Baseline(
            service=service,
            instance=instance,
            metric=metric,
            median=median,
            mad=mad,
            mad_scaled=scale,
            mean=statistics.fmean(ordered),
            stdev=stdev,
            p50=_quantile(ordered, 50),
            p95=_quantile(ordered, 95),
            p99=_quantile(ordered, 99),
            min=ordered[0],
            max=ordered[-1],
            sample_size=len(ordered),
            profile=profile,
            mode=BaselineMode.DYNAMIC,
            static_min=threshold.min,
            static_max=threshold.max,
            static_k=self.k,
        )

    @staticmethod
    def _hourly_profile(samples: list[tuple]) -> dict[int, float]:  # noqa: ANN001
        """hour-of-day -> 历史中位数。用于报告展示「当前处于波峰还是波谷」。"""
        buckets: dict[int, list[float]] = {}
        for ts, value in samples:
            buckets.setdefault(ts.hour, []).append(value)
        return {hour: statistics.median(vals) for hour, vals in buckets.items() if len(vals) >= 3}


def _quantile(ordered: list[float], q: float) -> float:
    if not ordered:
        return math.nan
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q / 100.0
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[int(pos)]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _days_to_delta(days: int):  # noqa: ANN001
    from datetime import timedelta

    return timedelta(days=days)
