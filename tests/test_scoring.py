"""评分模型测试。

核心要证明的是「分数可解释、可复现」：
同样的输入必须得到同样的分数，扣分必须能逐项对上账，封顶必须真的生效。
"""

from __future__ import annotations

import pytest

from staguard.config import Settings
from staguard.models import (
    Anomaly,
    BaselineMode,
    Dimension,
    MetricName,
    Severity,
    TimeWindow,
)
from staguard.scoring import ScoreCalculator
from staguard.utils.timeutil import from_epoch

from .conftest import BASE_EPOCH


def _anomaly(
    rule_id: str,
    level: Severity,
    amplitude: float,
    service: str = "order-svc",
    instance: str = "order-svc-0",
    metric: MetricName = MetricName.SUCCESS_RATE,
    observed: float = 90.0,
) -> Anomaly:
    window = TimeWindow(start=from_epoch(BASE_EPOCH - 1800), end=from_epoch(BASE_EPOCH))
    dimension = (
        Dimension.BUSINESS if rule_id.startswith("BIZ")
        else Dimension.RESOURCE if rule_id.startswith("RES")
        else Dimension.PERFORMANCE
    )
    return Anomaly(
        anomaly_id=f"{rule_id}|{service}|{instance}|{metric.value}",
        rule_id=rule_id, rule_name=rule_id, dimension=dimension,
        service=service, instance=instance, metric=metric,
        level=level, observed=observed, baseline_value=99.97,
        deviation_pct=-8.0, z_score=-20.0, deviation_score=amplitude,
        amplitude=amplitude, affected_ratio=1.0, duration_minutes=20.0,
        first_seen=window.start, last_seen=window.end,
        level_reason="测试", baseline_mode=BaselineMode.DYNAMIC,
    )


def test_clean_state_scores_near_full_marks(settings: Settings):
    result = ScoreCalculator(settings).compute([], [])
    assert result.total == pytest.approx(100.0)
    assert not result.capped_by
    assert all(item.deduction == 0 for item in result.breakdown)


def test_breakdown_accounts_for_every_deduction(settings: Settings):
    """扣分必须能对账：总分 = 100 - 各维度扣分之和（未触发封顶时）。"""
    anomalies = [
        _anomaly("BIZ-01", Severity.P1, 1.0),
        _anomaly("RES-01", Severity.P2, 0.6, metric=MetricName.CPU_USAGE, observed=92.0),
    ]
    result = ScoreCalculator(settings).compute(anomalies, [])
    total_deduction = round(sum(item.deduction for item in result.breakdown), 1)
    assert result.rule_score == pytest.approx(100.0 - total_deduction, abs=0.1)


def test_p1_caps_the_score(settings: Settings):
    """存在 P1 时的封顶必须生效，不能被其它维度的高分掩盖。"""
    anomalies = [_anomaly("BIZ-02", Severity.P1, 0.4)]
    result = ScoreCalculator(settings).compute(anomalies, [])
    assert result.rule_score <= 60.0
    assert result.capped_by is not None


def test_strictest_cap_wins(settings: Settings):
    """多个封顶条件同时命中时取最严的那个。

    配置里 `has_p1`（60 分）排在 `p1_on_critical`（45 分）前面，
    如果实现成「首个匹配即返回」，核心链路的 P1 就永远只能封到 60 分——
    而它恰恰是更需要拉低分数的情形。
    """
    anomalies = [_anomaly("BIZ-01", Severity.P1, 1.0, service="gateway")]
    result = ScoreCalculator(settings).compute(anomalies, [])
    assert result.capped_by is not None
    assert "核心链路" in result.capped_by


def test_ai_adjust_is_clamped(settings: Settings):
    anomalies = [_anomaly("RES-01", Severity.P2, 0.5, metric=MetricName.CPU_USAGE, observed=91.0)]
    base = ScoreCalculator(settings).compute(anomalies, [], ai_adjust=0)
    boosted = ScoreCalculator(settings).compute(anomalies, [], ai_adjust=99)
    assert boosted.ai_adjust == settings.scoring.ai_adjust_limit
    assert boosted.total <= min(100.0, base.rule_score + settings.scoring.ai_adjust_limit)


def test_score_is_deterministic(settings: Settings):
    """同一个输入必须得到同一个分数，否则「多次巡检对比」毫无意义。"""
    anomalies = [_anomaly("BIZ-01", Severity.P2, 0.7)]
    calculator = ScoreCalculator(settings)
    first = calculator.compute(anomalies, []).total
    second = calculator.compute(anomalies, []).total
    assert first == second


def test_error_budget_ratio_uses_slo(settings: Settings):
    """错误预算口径：99.9% 的服务跌到 99.0% 和第 99.0% 的服务跌到 99.0% 不该同罪。"""
    calculator = ScoreCalculator(settings)
    strict = calculator.compute([_anomaly("BIZ-01", Severity.P1, 1.0, observed=99.0)], [])
    assert strict.breakdown[0].details["budget_ratio"] > 1.0
    assert strict.breakdown[0].deduction > 0
