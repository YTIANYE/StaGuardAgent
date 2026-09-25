"""标准化清洗。

巡检链路里最容易被跳过、也最容易出事的一环。四大清洗动作，每一个都对应一类真实踩坑：

1. **单位统一**  —— 上游给的是秒、比例（0~1）、微秒，不统一就会把 3 秒当成 3 毫秒放过；
2. **越界剔除**  —— 成功率 103.7% 这种脏值如果直接进统计，基线会被永久抬高，
                    此后所有真实下跌都检测不到（这是最隐蔽的一类故障）；
3. **时间对齐**  —— 不同来源的采样时刻不会正好落在整分钟上，不对齐就无法跨源比较；
4. **缺失处理**  —— 短缺口线性插值（避免曲线断掉影响趋势判定），
                    长缺口保留为缺失并计入质量报告（**绝不插值**，否则凭空造出不存在的趋势）。

清洗结果不改变「事实」，只改变「可用性」，并且全部记入数据质量报告，
最终体现为巡检结论置信度的下调——数据不可信时，正确动作是降低置信度，而不是硬报异常。
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from ..models import (
    Confidence,
    DataQualityIssue,
    DataQualityReport,
    MetricName,
    MetricPoint,
    TimeWindow,
)
from ..models.common import floor_to_bucket
from ..utils.timeutil import from_epoch, to_epoch

logger = logging.getLogger(__name__)

UNIT_FACTOR: dict[tuple[str, str], float] = {
    # (源单位, 目标单位) -> 系数
    ("s", "ms"): 1000.0,
    ("second", "ms"): 1000.0,
    ("us", "ms"): 0.001,
    ("µs", "ms"): 0.001,
    ("ratio", "%"): 100.0,
    ("fraction", "%"): 100.0,
    ("req/min", "req/s"): 1 / 60.0,
}

# 各指标的合法取值范围。超出即为脏数据，直接剔除。
VALID_RANGE: dict[MetricName, tuple[float, float]] = {
    MetricName.QPS: (0.0, 5_000_000.0),
    MetricName.SUCCESS_RATE: (0.0, 100.0),
    MetricName.ERROR_COUNT: (0.0, 1e9),
    MetricName.BUSINESS_ERROR_RATE: (0.0, 100.0),
    MetricName.LATENCY_P50: (0.0, 3_600_000.0),
    MetricName.LATENCY_P95: (0.0, 3_600_000.0),
    MetricName.LATENCY_P99: (0.0, 3_600_000.0),
    MetricName.CPU_USAGE: (0.0, 100.0),
    MetricName.MEM_USAGE: (0.0, 100.0),
    MetricName.CONN_USAGE: (0.0, 100.0),
}

MAX_GAP_BUCKETS = 3
"""短缺口阈值。超过这个长度的缺口不做插值——用插值抹掉一段长时间没数据的区间，
等于把「监控挂了」伪装成「一切正常」，比缺失本身更危险。"""


class NormalizeConfig(BaseModel):
    granularity_seconds: int = 60
    max_gap_buckets: int = MAX_GAP_BUCKETS
    dedup: str = "mean"
    """同一时间桶内多个采样点的合并策略：mean / max / last。"""


class SeriesStats(BaseModel):
    service: str
    instance: str
    metric: MetricName
    raw_points: int
    kept_points: int
    interpolated: int
    dropped_out_of_range: int
    max_gap_buckets: int


class NormalizeResult(BaseModel):
    points: list[MetricPoint] = Field(default_factory=list)
    quality: DataQualityReport = Field(default_factory=DataQualityReport)
    stats: list[SeriesStats] = Field(default_factory=list)
    window: TimeWindow | None = None

    def series_keys(self) -> list[str]:
        return [f"{s.service}/{s.instance}/{s.metric.value}" for s in self.stats]


def clean(
    raw_points: list[MetricPoint],
    config: NormalizeConfig,
    declared_units: dict[str, str] | None = None,
    clip_window: TimeWindow | None = None,
) -> NormalizeResult:
    """执行四步清洗。

    `clip_window` 决定「保留哪些点」。**默认不裁剪，保留采集回来的全部历史**——
    这一点很关键：趋势型规则（内存增长、性能劣化）需要 24 小时观察窗，
    如果这里按 30 分钟的巡检窗口把历史裁掉，那些规则将永远拿不到足够样本，
    而且失败方式是静默的（看起来「一切正常」）。要裁就只裁到采集范围本身。
    """
    declared_units = declared_units or {}
    issues: list[DataQualityIssue] = []

    converted: list[MetricPoint] = []
    for point in raw_points:
        key = f"{point.service}/{point.instance}/{point.metric.value}"
        source_unit = declared_units.get(key) or point.metric.unit
        value = _convert_unit(point.metric, point.value, source_unit, issues, point)
        if value is None:
            continue
        converted.append(point.model_copy(update={"value": value}))

    bucketed, dropped_by_series = _align_and_validate(converted, config, issues)
    filled, interpolated_by_series, gap_issues = _fill_gaps(bucketed, clip_window, config)
    issues.extend(gap_issues)

    stats = _build_stats(converted, bucketed, filled, dropped_by_series, interpolated_by_series)
    kept = sum(s.kept_points for s in stats)
    total_expected = _expected_points(len(stats), clip_window, filled, config.granularity_seconds)
    report = DataQualityReport(
        total_points=kept,
        expected_points=total_expected,
        missing_ratio=_safe_ratio(max(0, total_expected - kept), total_expected),
        interpolated_ratio=_safe_ratio(sum(s.interpolated for s in stats), max(kept, 1)),
        issues=issues[:200],
    )
    report.confidence = report.grade()
    dropped_total = sum(dropped_by_series.values())
    if dropped_total:
        logger.warning("越界脏数据已剔除 %d 个点（不参与基线，避免污染阈值）", dropped_total)
    observed = _observed_window(filled)
    return NormalizeResult(points=filled, quality=report, stats=stats, window=observed)


# --------------------------------------------------------------------------- 单位
def _convert_unit(
    metric: MetricName,
    value: float,
    source_unit: str,
    issues: list[DataQualityIssue],
    point: MetricPoint,
) -> float | None:
    target = metric.unit
    if not source_unit or source_unit == target:
        return value
    factor = UNIT_FACTOR.get((source_unit, target))
    if factor is None:
        issues.append(
            DataQualityIssue(
                kind="out_of_range",
                service=point.service,
                instance=point.instance,
                metric=metric,
                detail=f"单位 {source_unit} 无法换算到 {target}，按原值处理",
            )
        )
        return value
    return value * factor


# --------------------------------------------------------------------------- 对齐与越界
SeriesKey = tuple[str, str, MetricName]


def _align_and_validate(
    points: list[MetricPoint], config: NormalizeConfig, issues: list[DataQualityIssue]
) -> tuple[dict[SeriesKey, dict[int, list[float]]], dict[SeriesKey, int]]:
    grouped: dict[SeriesKey, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    dropped: dict[SeriesKey, int] = defaultdict(int)
    for point in points:
        low, high = VALID_RANGE[point.metric]
        if math.isnan(point.value) or not (low <= point.value <= high):
            key = (point.service, point.instance, point.metric)
            dropped[key] += 1
            issues.append(
                DataQualityIssue(
                    kind="out_of_range",
                    service=point.service,
                    instance=point.instance,
                    metric=point.metric,
                    detail=f"{point.metric.label} 出现越界值 {point.value:g}（合法区间 {low:g}~{high:g}），已剔除",
                )
            )
            continue
        bucket = floor_to_bucket(point.ts, config.granularity_seconds)
        grouped[(point.service, point.instance, point.metric)][to_epoch(bucket)].append(point.value)
    return grouped, dropped


def _merge(values: list[float], strategy: str) -> float:
    if len(values) == 1:
        return values[0]
    if strategy == "max":
        return max(values)
    if strategy == "last":
        return values[-1]
    return sum(values) / len(values)


# --------------------------------------------------------------------------- 缺失
def _fill_gaps(
    grouped: dict[SeriesKey, dict[int, list[float]]],
    clip_window: TimeWindow | None,
    config: NormalizeConfig,
) -> tuple[list[MetricPoint], dict[SeriesKey, int], list[DataQualityIssue]]:
    issues: list[DataQualityIssue] = []
    out: list[MetricPoint] = []
    interpolated: dict[SeriesKey, int] = defaultdict(int)
    step = config.granularity_seconds
    window_start = to_epoch(floor_to_bucket(clip_window.start, step)) if clip_window else None
    window_end = to_epoch(floor_to_bucket(clip_window.end, step)) if clip_window else None
    max_gap_seconds = config.max_gap_buckets * step

    for (service, instance, metric), buckets in grouped.items():
        epochs = sorted(buckets)
        if not epochs:
            continue
        values = {epoch: _merge(buckets[epoch], config.dedup) for epoch in epochs}
        first, last = epochs[0], epochs[-1]

        # 缺口只补在「有数据的区间内部」，两端不补：
        # 巡检窗口开头就断流，那是事实，不是缺失。
        cursor = first + step
        while cursor < last:
            if cursor not in values:
                gap_end = cursor
                while gap_end < last and gap_end not in values:
                    gap_end += step
                gap_length = gap_end - cursor
                left_epoch = cursor - step
                right_epoch = gap_end
                if gap_length <= max_gap_seconds:
                    span = right_epoch - left_epoch
                    for epoch in range(cursor, gap_end, step):
                        ratio = (epoch - left_epoch) / span
                        values[epoch] = values[left_epoch] + (values[right_epoch] - values[left_epoch]) * ratio
                        interpolated[(service, instance, metric)] += 1
                else:
                    issues.append(
                        DataQualityIssue(
                            kind="missing",
                            service=service,
                            instance=instance,
                            metric=metric,
                            detail=(
                                f"存在 {gap_length // step} 个连续采样点缺失"
                                f"（{from_epoch(cursor):%H:%M}~{from_epoch(gap_end):%H:%M}），超过插值阈值未补齐"
                            ),
                        )
                    )
                cursor = gap_end
            cursor += step

        for epoch in sorted(values):
            if window_start is not None and epoch < window_start:
                continue
            if window_end is not None and epoch >= window_end:
                continue
            out.append(
                MetricPoint(
                    ts=from_epoch(epoch),
                    service=service,
                    instance=instance,
                    metric=metric,
                    value=round(values[epoch], 6),
                )
            )
    out.sort(key=lambda p: (p.service, p.instance, p.metric.value, p.ts))
    return out, interpolated, issues


# --------------------------------------------------------------------------- 统计
def _build_stats(
    converted: list[MetricPoint],
    bucketed: dict[SeriesKey, dict[int, list[float]]],
    filled: list[MetricPoint],
    dropped: dict[SeriesKey, int],
    interpolated: dict[SeriesKey, int],
) -> list[SeriesStats]:
    raw_counts: dict[SeriesKey, int] = defaultdict(int)
    for point in converted:
        raw_counts[(point.service, point.instance, point.metric)] += 1
    kept: dict[SeriesKey, int] = defaultdict(int)
    for point in filled:
        kept[(point.service, point.instance, point.metric)] += 1

    stats: list[SeriesStats] = []
    for (service, instance, metric), buckets in bucketed.items():
        stats.append(
            SeriesStats(
                service=service,
                instance=instance,
                metric=metric,
                raw_points=raw_counts[(service, instance, metric)],
                kept_points=kept[(service, instance, metric)],
                interpolated=interpolated[(service, instance, metric)],
                dropped_out_of_range=dropped[(service, instance, metric)],
                max_gap_buckets=_max_gap(sorted(buckets)),
            )
        )
    return stats


def _max_gap(epochs: list[int]) -> int:
    """最长连续缺失桶数。用相邻间隔的中位数推断步长，避免被单点稀疏数据带偏。"""
    if len(epochs) < 2:
        return 0
    deltas = sorted(b - a for a, b in zip(epochs, epochs[1:], strict=False) if b > a)
    if not deltas:
        return 0
    step = deltas[len(deltas) // 2]
    if step <= 0:
        return 0
    return max(0, max(int((b - a) // step) - 1 for a, b in zip(epochs, epochs[1:], strict=False)))


def _expected_points(
    series_count: int,
    clip_window: TimeWindow | None,
    points: list[MetricPoint],
    granularity_seconds: int,
) -> int:
    """应有样本数。用于计算缺失率——缺失率是数据可信度的主要依据。"""
    if clip_window is not None:
        buckets = max(1, int(clip_window.duration.total_seconds() // granularity_seconds))
        return series_count * buckets
    observed = _observed_window(points)
    if observed is None:
        return 0
    buckets = max(1, int(observed.duration.total_seconds() // granularity_seconds) + 1)
    return series_count * buckets


def _observed_window(points: list[MetricPoint]) -> TimeWindow | None:
    if not points:
        return None
    times = [p.ts for p in points]
    return TimeWindow(start=min(times), end=max(times))


def _safe_ratio(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return max(0.0, min(1.0, numerator / denominator))


def grade_confidence(report: DataQualityReport) -> Confidence:
    return report.grade()


def window_buckets(window: TimeWindow, granularity_seconds: int) -> list[datetime]:
    step = timedelta(seconds=granularity_seconds)
    cur = floor_to_bucket(window.start, granularity_seconds)
    end = to_epoch(window.end)
    buckets: list[datetime] = []
    while to_epoch(cur) < end:
        buckets.append(cur)
        cur = cur + step
    return buckets
