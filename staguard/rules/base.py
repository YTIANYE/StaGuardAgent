"""规则基类与公共装配逻辑。

把「每条规则都要做一遍的脏活」收敛到这里：
取时序、取基线、算偏离、算持续时长、匹配级别、组装 finding。

这样具体规则只需要写「我关心哪个指标、怎么算幅度」——
`RES-01 CPU 高负载` 的实现因此只有十来行，而它的分级、理由、归因证据、
评分权重全都是完整的、和其它规则一致的。
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import LevelSpec, RuleConfig
from ..models import (
    AnomalyFinding,
    Baseline,
    BaselineMode,
    Confidence,
    Dimension,
    MetricName,
    MetricSeries,
    Severity,
    TimeWindow,
)
from ..utils.text import render_template
from .context import EvalContext
from .leveling import (
    LEVEL_AMPLITUDE_BAND,  # noqa: F401  (对外暴露统一的幅度区间常量)
    MatchedLevel,
    build_reason_context,
    compute_deviation_score,
)

logger = logging.getLogger(__name__)

SLO_METRIC_MAP: dict[MetricName, str] = {
    MetricName.SUCCESS_RATE: "success_rate",
    MetricName.BUSINESS_ERROR_RATE: "business_error_rate",
    MetricName.LATENCY_P95: "latency_p95_ms",
    MetricName.LATENCY_P99: "latency_p99_ms",
}
"""指标到 SLO 字段的映射。SLO 是「业务红线」，与动态基线是两套独立的标准：
动态基线回答「和过去比是否异常」，SLO 回答「和承诺比是否达标」，缺一不可。"""


@dataclass
class SeriesFacts:
    """一条序列在巡检窗口内的全部事实。规则判定与原因渲染都从这里取数。"""

    service: str
    instance: str
    metric: MetricName
    series: MetricSeries
    baseline: Baseline
    observed: float
    """窗口内「最坏」的值（延时/负载取最大，成功率取最小）。"""
    baseline_value: float | None
    deviation_pct: float
    z_score: float
    duration_minutes: float
    first_seen: datetime | None = None
    """异常真正的起始时刻（由最长命中区间回填），用于和变更时间对齐做归因。"""
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def has_dynamic_baseline(self) -> bool:
        return self.baseline.mode is BaselineMode.DYNAMIC

    @property
    def baseline_confidence(self) -> Confidence:
        return self.baseline.confidence

    @property
    def values(self) -> list[float]:
        return self.series.values()


def build_facts(
    ctx: EvalContext,
    service: str,
    instance: str,
    metric: MetricName,
    window: TimeWindow | None = None,
    direction: str | None = None,
) -> SeriesFacts | None:
    """装配一条序列的事实集合。序列为空时返回 None（该规则对该实例不适用）。

    `direction` 决定「取哪个极端值作为观测值」，这是 QPS 这类**双向指标**的关键：
    「突增」要看窗口内的最大值，「突降」要看最小值。用同一个方向去判两个方向，
    必然有一半的规则永远报不出来——而且不会报错，只会静默失效。
    """
    series = ctx.series(service, instance, metric, window)
    if series.is_empty:
        return None
    baseline = ctx.baseline(service, instance, metric)
    values = series.values()
    if direction == "down":
        observed = min(values)
    elif direction == "up":
        observed = max(values)
    else:
        observed = series.worst()
    if math.isnan(observed):
        return None

    threshold = ctx.threshold(metric, service)
    slo_value = _slo_value(ctx, service, metric)
    baseline_value = _reference_value(baseline, metric, threshold)
    deviation_pct = baseline.deviation_pct(observed, ctx.window.end.hour) if baseline_value else 0.0
    z_score = baseline.z_score(observed, ctx.window.end.hour) if baseline_value else 0.0

    red_line = threshold.min if metric.higher_is_better else threshold.max
    below_slo = bool(slo_value is not None and (
        observed < slo_value if metric.higher_is_better else observed > slo_value
    ))

    facts: dict[str, Any] = {
        "observed": observed,
        "value": observed,
        "baseline": baseline_value,
        "deviation_pct": deviation_pct,
        "z_score": z_score,
        "scale": baseline.mad_scaled,
        "drop_points": max(0.0, (baseline_value - observed)) if baseline_value else 0.0,
        "rise_points": max(0.0, (observed - baseline_value)) if baseline_value else 0.0,
        "down_pct": (
            max(0.0, (baseline_value - observed) / abs(baseline_value) * 100.0) if baseline_value else 0.0
        ),
        "multiplier": (observed / baseline_value) if baseline_value else 1.0,
        "threshold": red_line if red_line is not None else 0.0,
        "slo": slo_value if slo_value is not None else 0.0,
        "below_slo": below_slo,
        "cv": round(series.cv(), 4),
        "_higher_is_better": metric.higher_is_better,
        "_series_values": series.values(),
        "_step_minutes": series_step_minutes(series),
        "_window_minutes": (window or ctx.window).duration_minutes,
    }
    b = baseline_value if baseline_value else 0.0
    facts["deviation_signed"] = deviation_pct
    facts["baseline_ratio"] = (observed / b) if b else 1.0
    return SeriesFacts(
        service=service,
        instance=instance,
        metric=metric,
        series=series,
        baseline=baseline,
        observed=observed,
        baseline_value=baseline_value,
        deviation_pct=deviation_pct,
        z_score=z_score,
        duration_minutes=0.0,
        facts=facts,
    )


def series_step_minutes(series: MetricSeries) -> float:
    from ..utils.timeutil import to_epoch

    if len(series.points) < 2:
        return 1.0
    deltas = [
        to_epoch(b) - to_epoch(a)
        for a, b in zip(series.timestamps(), series.timestamps()[1:], strict=False)
        if to_epoch(b) > to_epoch(a)
    ]
    if not deltas:
        return 1.0
    deltas.sort()
    return max(1.0, deltas[(len(deltas) - 1) // 2] / 60.0)


def _reference_value(baseline: Baseline, metric: MetricName, threshold) -> float | None:  # noqa: ANN001
    """取用于比较的参考值。

    动态基线可用时取历史同时段中位数；不可用时回退到静态红线，
    这样「冷启动期仍然能按绝对标准判定」——但报告里会标成 static_fallback，
    读报告的人知道这个结论的成色。
    """
    if baseline.mode is BaselineMode.DYNAMIC and not math.isnan(baseline.median):
        return baseline.median
    if metric.higher_is_better:
        return threshold.min
    return threshold.max


def _slo_value(ctx: EvalContext, service: str, metric: MetricName) -> float | None:
    field_name = SLO_METRIC_MAP.get(metric)
    if not field_name:
        return None
    return float(getattr(ctx.slo(service), field_name))


def value_bounds(levels: list[LevelSpec]) -> tuple[float | None, float | None]:
    """从分级配置里推出 (预警线, 红线)。

    预警线 = 最宽松级别的 value 阈值，红线 = 最strict级别的 value 阈值。
    用它们把观测值映射到 0~1 的幅度分数，这样「刚越线」和「远超红线」在排序上能拉开。
    """
    values = [float(spec.when["value"]) for spec in levels if "value" in spec.when]
    if not values:
        return None, None
    return min(values), max(values)


class Rule(ABC):
    """规则基类。"""

    def __init__(self, config: RuleConfig) -> None:
        self.config = config
        self.id = config.id
        self.name = config.name
        self.dimension: Dimension = config.dimension
        self.levels: list[LevelSpec] = config.levels
        self.params: dict[str, Any] = config.params

    @abstractmethod
    def evaluate(self, ctx: EvalContext) -> list[AnomalyFinding]:
        """执行一次判定，返回命中的原始发现。"""

    # ------------------------------------------------------------------ 公共工具
    def metric_param(self, key: str = "metric") -> MetricName:
        raw = self.params.get(key)
        if raw is None:
            raise ValueError(f"规则 {self.id} 缺少必填参数 `{key}`")
        return MetricName(raw) if not isinstance(raw, MetricName) else raw

    def make_finding(
        self,
        ctx: EvalContext,
        facts: SeriesFacts,
        matched: MatchedLevel,
        *,
        amplitude: float,
        duration_minutes: float | None = None,
        extra_facts: dict[str, Any] | None = None,
        extra_evidence: dict[str, Any] | None = None,
        metric_label: str | None = None,
        unit: str | None = None,
        levels: list[LevelSpec] | None = None,
    ) -> AnomalyFinding:
        duration = facts.duration_minutes if duration_minutes is None else duration_minutes
        instance_count = max(1, len(ctx.instances_of(facts.service)))
        criticality = ctx.criticality(facts.service)
        score = compute_deviation_score(
            amplitude=amplitude,
            duration_minutes=duration,
            affected_ratio=1.0 / instance_count,
            criticality=criticality,
        )

        render_facts = dict(facts.facts)
        # 持续时长是算出来的，不在 facts 里，必须显式注入渲染上下文，
        # 否则模板里的 {duration} 会静默变成 0——「持续 0 分钟」这种结论比不写还糟。
        render_facts["duration_minutes"] = duration
        if extra_facts:
            render_facts.update(extra_facts)
        context = build_reason_context(
            render_facts,
            metric_label=metric_label or facts.metric.label,
            unit=unit if unit is not None else facts.metric.unit,
            service=facts.service,
            instance=facts.instance,
        )
        reason = render_template(matched.reason, context) if matched.reason else self._default_reason(
            facts, matched
        )

        evidence: dict[str, Any] = {
            "amplitude": round(amplitude, 4),
            "matched_conditions": matched.conditions,
            "instance_count": instance_count,
            "owner": ctx.owner(facts.service),
            "samples": len(facts.series.points),
        }
        if extra_evidence:
            evidence.update(extra_evidence)

        last_seen = facts.series.latest()
        started_at = facts.first_seen or facts.series.timestamps()[0]
        return AnomalyFinding(
            rule_id=self.id,
            rule_name=self.name,
            dimension=self.dimension,
            service=facts.service,
            instance=facts.instance,
            metric=facts.metric,
            level=matched.level,
            observed=facts.observed,
            baseline_value=facts.baseline_value,
            deviation_pct=facts.deviation_pct,
            z_score=facts.z_score,
            deviation_score=score,
            amplitude=round(amplitude, 4),
            affected_ratio=1.0 / instance_count,
            duration_minutes=duration,
            window=TimeWindow(
                start=started_at,
                end=max(last_seen[0], started_at) if last_seen else ctx.window.end,
            ),
            level_reason=reason,
            baseline_mode=facts.baseline.mode,
            baseline_confidence=facts.baseline_confidence,
            evidence=evidence,
        )

    def _default_reason(self, facts: SeriesFacts, matched: MatchedLevel) -> str:
        return (
            f"{facts.metric.label} 实测 {facts.observed:.2f}{facts.metric.unit}，"
            f"基线 {facts.baseline_value if facts.baseline_value is not None else '-'}"
            f"（{matched.level.display()}）"
        )

    def qualifies(self, facts: SeriesFacts, matched: MatchedLevel) -> bool:
        """额外准入条件。默认全部放行；子类可覆盖以抑制噪声（如样本量不足）。"""
        return True


def resolve_duration(levels: list[LevelSpec], matched: MatchedLevel, facts: SeriesFacts) -> float:
    """回填匹配级别的真实持续时长与起始时刻。"""
    from .leveling import POINTWISE_CONDITIONS, longest_run, point_predicate

    predicates = []
    for key, value in matched.conditions.items():
        if key == "duration_minutes" or key not in POINTWISE_CONDITIONS:
            continue
        predicate = point_predicate(key, value, facts.facts)
        if predicate is not None:
            predicates.append(predicate)

    timestamps = facts.series.timestamps()
    if not predicates:
        if timestamps:
            facts.first_seen = timestamps[0]
        return facts.facts.get("_window_minutes", 30.0)

    duration, start_index = longest_run(
        facts.facts.get("_series_values", []),
        predicates,
        facts.facts.get("_step_minutes", 1.0),
    )
    if 0 <= start_index < len(timestamps):
        facts.first_seen = timestamps[start_index]
    elif timestamps:
        facts.first_seen = timestamps[0]
    return duration


__all__ = [
    "SLO_METRIC_MAP",
    "LevelSpec",
    "Rule",
    "SeriesFacts",
    "Severity",
    "build_facts",
    "resolve_duration",
    "series_step_minutes",
    "value_bounds",
]
