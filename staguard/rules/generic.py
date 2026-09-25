"""通用阈值型规则。

覆盖了 16 条规则里的大多数：只要一件事能用「关心哪个指标 + 什么幅度算几级」描述清楚，
就不需要为它写一行专门代码。

    业务层  BIZ-01 成功率下跌 / BIZ-02 业务错误率上升 / BIZ-03 流量突增
            BIZ-04 流量突降 / BIZ-05 报错量飙升
    性能层  PERF-01 P99 超时 / PERF-02 P95 超时 / PERF-03 响应抖动
    资源层  RES-01 CPU 高负载 / RES-02 内存过高 / RES-04 连接数打满

**为什么值得这么抽象**：这些规则真正的差异不在算法，而在业务判断——
「成功率跌 5 个百分点算紧急，CPU 到 95% 才算紧急」。把差异全部收进 YAML，
规则代码就退化成一条统一的判定流水线：
    取时序 -> 取基线 -> 算偏离 -> 精确数持续时长 -> 匹配级别 -> 算幅度 -> 组装 finding
新增一条同类规则的成本因此接近零，而判定质量完全一致。
"""

from __future__ import annotations

from ..config import LevelSpec
from ..models import AnomalyFinding, MetricName
from .base import Rule, SeriesFacts, build_facts, resolve_duration
from .context import EvalContext
from .leveling import (
    MatchedLevel,
    amplitude_from_value,
    amplitude_from_z,
    banded_amplitude,
    bounds_for_key,
    match_level_series,
    scale_levels,
)

# 这些条件不是「指标值本身」，不参与幅度计算
NON_AMPLITUDE_CONDITIONS = frozenset({
    "duration_minutes", "min_absolute", "instances", "below_slo", "health_below",
})


class GenericThresholdRule(Rule):
    """单一指标 + 分级阈值。"""

    def evaluate(self, ctx: EvalContext) -> list[AnomalyFinding]:
        target_metric = self.metric_param()
        direction = self.params.get("direction")
        findings: list[AnomalyFinding] = []
        for service, instance in ctx.instances():
            facts = build_facts(ctx, service, instance, target_metric, direction=direction)
            if facts is None:
                continue
            levels = self.levels_for(ctx, target_metric, service)
            matched = match_level_series(levels, facts.facts)
            if matched is None or not self.qualifies(facts, matched):
                continue
            facts.duration_minutes = resolve_duration(levels, matched, facts)
            amplitude = self.amplitude_for(facts, matched, levels)
            findings.append(self.make_finding(ctx, facts, matched, amplitude=amplitude, levels=levels))
        return findings

    def levels_for(self, ctx: EvalContext, metric: MetricName, service: str) -> list[LevelSpec]:
        """取该服务适用的分级阈值（含按服务红线缩放）。"""
        configured = ctx.threshold(metric, service)
        if metric.higher_is_better:
            # 成功率这类「下限型」指标不做等比缩放：把 99.0 按比例放大在语义上是错的
            return self.levels
        reference = self.params.get("default_threshold")
        default = (
            float(reference)
            if reference is not None
            else float(self.default_threshold(ctx, metric) or 0.0)
        )
        if not default or configured.max is None:
            return self.levels
        return scale_levels(self.levels, float(configured.max) / default)

    @staticmethod
    def default_threshold(ctx: EvalContext, metric: MetricName) -> float | None:
        spec = ctx.settings.thresholds.defaults.get(metric.value)
        return None if spec is None else spec.max

    def qualifies(self, facts: SeriesFacts, matched: MatchedLevel) -> bool:
        """抑制「采样点太少」导致的伪判定。

        只有 2 个点的窗口算出来的「持续 2 分钟」毫无统计意义。规则宁可漏，
        也不能在数据不足时给出看起来很确定的结论——这是巡检系统可信度的底线。
        """
        min_samples = int(self.params.get("min_samples", 3))
        return len(facts.series.points) >= min_samples

    def amplitude_for(
        self,
        facts: SeriesFacts,
        matched: MatchedLevel,
        levels: list[LevelSpec],
    ) -> float:
        """幅度分数 = 各命中条件「越过预警线多少」的最大值，再取级别下限。

        统一用「该条件在配置里的最宽松阈值 -> 最严格阈值」作为 0~1 的映射区间，
        于是成功率的百分点、延时的毫秒数、QPS 的倍数、抖动的 CV 全部可比。
        这比给每个指标手写一套归一化系数更不容易过时：阈值改了，幅度区间自动跟着改。

        最后再按级别压进对应的幅度区间（见 leveling.LEVEL_AMPLITUDE_BAND），
        保证「刚好踩线的 P1」永远排在「爆表的 P4」前面。
        """
        candidates: list[float] = []
        for key in matched.conditions:
            if key in NON_AMPLITUDE_CONDITIONS:
                continue
            key_warn, key_critical = bounds_for_key(levels, key)
            if key_warn is None:
                continue
            actual = abs(float(facts.facts.get(key, 0.0)))
            candidates.append(amplitude_from_value(actual, key_warn, key_critical or key_warn))
        if facts.has_dynamic_baseline:
            candidates.append(amplitude_from_z(facts.z_score))
        return banded_amplitude(max(candidates) if candidates else 0.5, matched.level)


class SampleGuardedRule(GenericThresholdRule):
    """要求更多样本的规则（趋势/抖动类），避免在短窗口上做统计推断。"""

    def qualifies(self, facts: SeriesFacts, matched: MatchedLevel) -> bool:
        min_samples = int(self.params.get("min_samples", 10))
        return len(facts.series.points) >= min_samples


__all__ = ["NON_AMPLITUDE_CONDITIONS", "GenericThresholdRule", "SampleGuardedRule"]
