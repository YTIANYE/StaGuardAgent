"""动态基线测试。

重点是验证「稳健」这两个字：历史样本里混着故障点时，基线不能被带偏。
这是动态阈值能不能用的分水岭——一个会被一次大故障永久抬高的基线，
比固定阈值更危险，因为它会让人以为「已经动态适配了」。
"""

from __future__ import annotations

import statistics

import pytest

from staguard.models import Baseline, BaselineMode, Confidence, MetricName
from staguard.rules.provider import RELATIVE_FLOOR


def _baseline(values: list[float], k: float = 4.0) -> Baseline:
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values])
    scale = max(1.4826 * mad, abs(median) * RELATIVE_FLOOR)
    return Baseline(
        service="order-svc", instance="order-svc-0", metric=MetricName.LATENCY_P99,
        median=median, mad=mad, mad_scaled=scale, sample_size=len(values),
        mode=BaselineMode.DYNAMIC, static_k=k, static_max=800.0,
    )


def test_mad_is_robust_against_outliers():
    """即使 5% 的历史样本是故障点，基线也不应被显著抬高。

    对比一下均值口径：同样一组数据，均值会被那 5% 的离群点拉高一大截，
    于是「下次再出同样的故障」就检测不出来了。
    """
    normal = [100.0 + (i % 5) for i in range(190)]
    outages = [3000.0] * 10  # 5% 的历史故障点
    values = normal + outages

    robust = _baseline(values)
    mean_based = statistics.fmean(values)

    assert robust.median == pytest.approx(102.0, abs=1.0)
    assert robust.upper < 200  # 阈值仍在可比区间
    assert mean_based > 240  # 均值口径已经被离群点带偏


def test_relative_floor_prevents_zero_scale_explosion():
    """极稳定序列的 MAD 趋近于 0，必须加相对下限。

    否则一个 0.01 的抖动会被算成几十个标准差，规则会疯狂误报。
    """
    values = [99.97] * 100
    baseline = _baseline(values)
    assert baseline.mad == 0.0
    assert baseline.mad_scaled >= abs(99.97) * RELATIVE_FLOOR
    assert abs(baseline.z_score(99.98)) < 1.0


def test_z_score_and_deviation_direction():
    baseline = _baseline([100.0 + (i % 3) for i in range(200)])
    assert baseline.z_score(90.0) < 0
    assert baseline.deviation_pct(90.0) < 0
    assert baseline.deviation_pct(110.0) > 0


def test_confidence_grades_by_sample_size():
    assert _baseline([100.0] * 250).confidence is Confidence.HIGH
    assert _baseline([100.0] * 80).confidence is Confidence.MEDIUM
    assert _baseline([100.0] * 10).confidence is Confidence.LOW


def test_static_fallback_is_not_usable_as_dynamic():
    """回退静态阈值时必须显式标记，且不允许被当作动态基线使用。

    这个标记会一路传到报告里。读报告的人必须能分清
    「这是和历史比出来的」和「这是拿固定红线卡出来的」——两者的可信度完全不同。
    """
    fallback = Baseline(
        service="new-svc", instance="new-svc-0", metric=MetricName.LATENCY_P99,
        mode=BaselineMode.STATIC_FALLBACK, sample_size=5, static_max=800.0,
    )
    assert not fallback.is_usable
    assert fallback.confidence is Confidence.LOW
    assert fallback.upper == 800.0
