"""资源层专有规则：实例不均衡。

RES-01（CPU）、RES-02（内存）、RES-04（连接数）是通用阈值规则，
真正的独立逻辑在 **RES-05 实例不均衡**——它回答的是一个和数值阈值完全不同的问题：

    这个服务的资源指标确实高了，**是所有实例一起高，还是只有一个实例高？**

两者的处置动作完全相反。全体一起高 → 容量问题，要扩容或优化；
只有一个实例高 → 那个实例有问题（坏节点、宿主机争抢、发布未同步），
正确动作是先把这一个摘掉。分不清这两者，就只能给一句「建议扩容」的废话。

RES-03 内存增长趋势由 `performance.TrendRule` 复用实现（趋势算法与指标无关）。
"""

from __future__ import annotations

import logging
import statistics

from ..models import AnomalyFinding, MetricName
from .base import Rule, SeriesFacts, series_step_minutes
from .context import EvalContext
from .leveling import amplitude_from_value, bounds_for_key, match_level_series

logger = logging.getLogger(__name__)

MIN_RATIO_FLOOR = 1.05
"""低于这个比值就算不上「不均衡」，纯粹是实例间的正常底噪。"""


class InstanceSkewRule(Rule):
    """RES-05 实例不均衡。"""

    def evaluate(self, ctx: EvalContext) -> list[AnomalyFinding]:
        metrics = [MetricName(m) for m in self.params.get("metrics", ["cpu_usage"])]
        min_instances = int(self.params.get("min_instances", 2))
        min_ratio = self._min_ratio()
        findings: list[AnomalyFinding] = []

        for service in ctx.all_services():
            instances = ctx.instances_of(service)
            if len(instances) < min_instances:
                continue
            for metric in metrics:
                series_map = ctx.service_series(service, metric)
                if len(series_map) < min_instances:
                    continue
                finding = self._evaluate_service(ctx, service, metric, series_map, min_ratio)
                if finding is not None:
                    findings.append(finding)
        return findings

    def _evaluate_service(
        self,
        ctx: EvalContext,
        service: str,
        metric: MetricName,
        series_map: dict,
        min_ratio: float,
    ) -> AnomalyFinding | None:
        worst_by_instance = {
            instance: series.worst() for instance, series in series_map.items() if not series.is_empty
        }
        if len(worst_by_instance) < 2:
            return None
        median_value = statistics.median(worst_by_instance.values())
        if median_value <= 0:
            return None

        deviating = max(worst_by_instance, key=lambda inst: worst_by_instance[inst] / median_value)
        observed = worst_by_instance[deviating]
        ratio = observed / median_value
        if ratio < max(min_ratio, MIN_RATIO_FLOOR):
            return None

        series = series_map[deviating]
        step = series_step_minutes(series)
        facts = SeriesFacts(
            service=service,
            instance=deviating,
            metric=metric,
            series=series,
            baseline=ctx.baseline(service, deviating, metric),
            observed=observed,
            baseline_value=median_value,
            deviation_pct=(observed - median_value) / median_value * 100.0,
            z_score=0.0,
            duration_minutes=ctx.window.duration_minutes,
            facts={
                "observed": observed,
                "value": observed,
                "baseline": median_value,
                "ratio": ratio,
                "deviation_pct": (observed - median_value) / median_value * 100.0,
                "z_score": 0.0,
                "scale": 0.0,
                "threshold": median_value,
                "metric": metric.label,
                "instances": len(worst_by_instance),
                "sibling_values": {k: round(v, 2) for k, v in sorted(worst_by_instance.items())},
                "_series_values": series.values(),
                "_step_minutes": step,
                "_window_minutes": ctx.window.duration_minutes,
            },
        )
        matched = match_level_series(self.levels, facts.facts)
        if matched is None:
            return None

        warn, critical = bounds_for_key(self.levels, "ratio")
        amplitude = amplitude_from_value(ratio, warn or 1.0, critical or (warn or 1.0) * 2)
        return self.make_finding(
            ctx,
            facts,
            matched,
            amplitude=amplitude,
            duration_minutes=ctx.window.duration_minutes,
            extra_evidence={
                "median_of_service": round(median_value, 2),
                "sibling_instances": {
                    k: round(v, 2) for k, v in worst_by_instance.items() if k != deviating
                },
            },
        )

    def _min_ratio(self) -> float:
        warn, _ = bounds_for_key(self.levels, "ratio")
        return warn if warn is not None else MIN_RATIO_FLOOR


__all__ = ["MIN_RATIO_FLOOR", "InstanceSkewRule"]
