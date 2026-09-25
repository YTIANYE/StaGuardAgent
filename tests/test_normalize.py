"""标准化清洗测试。

这四个动作每一个都对应一类真实踩坑，所以每一条都有明确的断言理由，
而不是为了覆盖率凑数。
"""

from __future__ import annotations

import pytest

from staguard.models import MetricName, MetricPoint
from staguard.normalize import NormalizeConfig, clean
from staguard.utils.timeutil import from_epoch

from .conftest import BASE_EPOCH, make_window


def _points(values: list[float], metric: MetricName, step: int = 60, start_offset: int = 30) -> list[MetricPoint]:
    start = BASE_EPOCH - start_offset * step
    return [
        MetricPoint(
            ts=from_epoch(start + i * step),
            service="order-svc",
            instance="order-svc-0",
            metric=metric,
            value=value,
        )
        for i, value in enumerate(values)
    ]


def test_out_of_range_values_are_dropped_not_clamped():
    """越界脏值必须剔除，不能裁剪到边界。

    成功率出现 103.7% 这种脏数据时，如果裁剪成 100%，它就会作为一个「完美值」
    进入基线计算，把基线永久抬高——此后真实的成功率下跌全部检测不到。
    这是最隐蔽的一类故障：数据看着干净了，检测能力却没了。
    """
    values = [99.9] * 20 + [103.7] + [99.9] * 9
    result = clean(_points(values, MetricName.SUCCESS_RATE), NormalizeConfig())

    kept = [p.value for p in result.points]
    assert 103.7 not in kept
    assert max(kept) <= 100.0
    assert any(issue.kind == "out_of_range" for issue in result.quality.issues)


def test_unit_conversion_seconds_to_milliseconds():
    """上游给秒、我们按毫秒巡检。不换算就会把 3 秒的故障当成 3 毫秒的正常值放过。"""
    points = _points([3.0] * 10, MetricName.LATENCY_P99)
    key = "order-svc/order-svc-0/latency_p99"
    result = clean(points, NormalizeConfig(), declared_units={key: "s"})
    assert all(p.value == pytest.approx(3000.0) for p in result.points)


def test_unit_conversion_ratio_to_percent():
    points = _points([0.9995] * 10, MetricName.SUCCESS_RATE)
    key = "order-svc/order-svc-0/success_rate"
    result = clean(points, NormalizeConfig(), declared_units={key: "ratio"})
    assert all(p.value == pytest.approx(99.95) for p in result.points)


def _points_at(indices: list[int], total_buckets: int = 12, value: float = 50.0) -> list[MetricPoint]:
    start = BASE_EPOCH - total_buckets * 60
    return [
        MetricPoint(
            ts=from_epoch(start + i * 60),
            service="order-svc",
            instance="order-svc-0",
            metric=MetricName.CPU_USAGE,
            value=value,
        )
        for i in indices
    ]


def test_short_gap_is_interpolated():
    """2 个桶的缺口在线性插值范围内，补齐后趋势判断不会被断点干扰。"""
    points = _points_at(list(range(0, 5)) + list(range(7, 12)))
    result = clean(points, NormalizeConfig(max_gap_buckets=3))
    assert len(result.points) == 12
    assert result.quality.interpolated_ratio > 0


def test_long_gap_is_left_missing():
    """长缺口**不做**插值。

    用插值抹掉一段「监控完全没有数据」的区间，等于把「监控挂了」伪装成
    「一切正常」——比缺失本身更危险。它必须保留为缺失，并被记进质量报告。
    """
    points = _points_at([0, 1, 2, 3, 9, 10, 11])
    result = clean(points, NormalizeConfig(max_gap_buckets=1))
    assert len(result.points) == 7
    assert any(issue.kind == "missing" for issue in result.quality.issues)
    assert result.quality.missing_ratio > 0


def test_clean_keeps_history_outside_inspection_window():
    """默认不裁剪：趋势型规则需要 24 小时的观察窗。

    如果这里按 30 分钟的巡检窗口裁剪，内存泄漏这类慢故障会**静默失效**——
    规则拿不到足够样本，行为退化成「什么都没发现」，而不是报错。
    """
    points = _points([50.0] * 100, MetricName.MEM_USAGE)
    result = clean(points, NormalizeConfig())
    assert len(result.points) == 100

    clipped = clean(points, NormalizeConfig(), clip_window=make_window(minutes=10))
    assert len(clipped.points) < 100


def test_duplicate_timestamps_are_merged():
    """同一时间桶内的重复采样要合并，否则同一时刻会被算成两个数据点。"""
    points = _points([10.0] * 6, MetricName.QPS)
    duplicate = points[3].model_copy(update={"value": 20.0})
    result = clean([*points, duplicate], NormalizeConfig())
    values = sorted(p.value for p in result.points)
    assert values.count(15.0) == 1  # mean(10, 20) = 15
