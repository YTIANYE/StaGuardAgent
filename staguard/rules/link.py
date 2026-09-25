"""链路层规则：业务链路异常（BIZ-06）。

**为什么单服务指标全绿，链路仍可能已经不可用**：串联系统的可用性是各节点成功率的乘积，
不是最小值。三个 99.9% 的节点串起来只有 99.7%——每个节点单独看都在 SLO 之内，
合起来已经击穿了 SLA。只看单点指标，这类问题永远发现不了。

判定方式：

    链路健康度 = Π(路径上各节点成功率)

对每个服务，枚举它到各叶子节点的路径，逐时间桶求乘积，取全局最差的链路。

**参考值也是乘积**：基线链路健康度 = Π(各节点基线成功率)。
不能拿「95%」这种绝对数字当红线——节点数不同，正常水位就不同，
三跳链路的正常值本来就只有 99.9%³ = 99.7%。用相对劣化（deviation_pct）判定，
才不会被路径长度带偏。这是这条规则唯一需要小心的地方。
"""

from __future__ import annotations

import logging
import statistics

from ..models import AnomalyFinding, MetricName, MetricSeries
from ..utils.timeutil import from_epoch, to_epoch
from .base import Rule, SeriesFacts, series_step_minutes
from .context import EvalContext
from .leveling import amplitude_from_value, match_level_series

logger = logging.getLogger(__name__)

MAX_PATHS = 32
"""拓扑里可能的路径数。真实微服务拓扑会出现路径爆炸，超过上限就只保留最长的几条——
巡检要在有限时间内出结论，不能为了完备性把一次巡检跑成分钟级。"""


class ChainHealthRule(Rule):
    """BIZ-06 业务链路异常。"""

    def evaluate(self, ctx: EvalContext) -> list[AnomalyFinding]:
        metric = MetricName(self.params.get("health_metric", "success_rate"))
        min_path_length = int(self.params.get("min_path_length", 2))
        findings: list[AnomalyFinding] = []

        for head in ctx.all_services():
            best = None
            for path in self._leaf_paths(ctx, head)[:MAX_PATHS]:
                if len(path) < min_path_length:
                    continue
                candidate = self._evaluate_path(ctx, path, metric)
                if candidate is None:
                    continue
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if best is None:
                continue
            health, baseline_health, weakest, series = best
            deviation = (
                (health - baseline_health) / baseline_health * 100.0 if baseline_health else 0.0
            )
            facts = SeriesFacts(
                service=head,
                instance=weakest,
                metric=metric,
                series=series,
                baseline=ctx.baseline(head, weakest, metric),
                observed=health,
                baseline_value=baseline_health,
                deviation_pct=deviation,
                z_score=0.0,
                duration_minutes=ctx.window.duration_minutes,
                facts={
                    "observed": health,
                    "value": health,
                    "baseline": baseline_health,
                    "deviation_pct": deviation,
                    "z_score": 0.0,
                    "scale": 0.0,
                    "threshold": baseline_health,
                    "path": " -> ".join(path),
                    "samples": len(series.points),
                    "_series_values": series.values(),
                    "_step_minutes": series_step_minutes(series),
                    "_window_minutes": ctx.window.duration_minutes,
                },
            )
            matched = match_level_series(self.levels, facts.facts)
            if matched is None:
                continue
            amplitude = amplitude_from_value(
                abs(deviation), *self._deviation_bounds()
            )
            findings.append(
                self.make_finding(
                    ctx,
                    facts,
                    matched,
                    amplitude=amplitude,
                    extra_evidence={
                        "chain": path,
                        "hop_count": len(path),
                        "baseline_chain_health": round(baseline_health, 3),
                        "weakest_node": weakest,
                    },
                    metric_label="链路健康度",
                    unit="%",
                )
            )
        return findings

    # ------------------------------------------------------------------ 内部
    def _leaf_paths(self, ctx: EvalContext, head: str) -> list[list[str]]:
        """枚举 head 到各叶子（无下游的服务）的所有路径。拓扑很小，直接 DFS。"""
        paths: list[list[str]] = []

        def walk(node: str, trail: list[str]) -> None:
            if len(paths) >= MAX_PATHS:
                return
            children = [c for c in ctx.topology.downstream(node)]
            if not children:
                paths.append(list(trail))
                return
            for child in children:
                if child in trail:  # 防御环依赖
                    continue
                walk(child, trail + [child])

        walk(head, [head])
        return paths

    def _evaluate_path(
        self, ctx: EvalContext, path: list[str], metric: MetricName
    ) -> tuple[float, float, str, MetricSeries] | None:
        """计算一条路径的链路健康度曲线与基线健康度。"""
        node_series: list[tuple[str, dict[int, float]]] = []
        baseline_health = 1.0
        weakest = path[0]
        weakest_value = 101.0

        for node in path:
            instances = ctx.instances_of(node)
            if not instances:
                return None
            per_instance = {}
            for instance in instances:
                series = ctx.series(node, instance, metric)
                if series.is_empty:
                    continue
                per_instance[instance] = {to_epoch(ts): value for ts, value in series.points}
            if not per_instance:
                return None
            merged = _service_level(per_instance)
            node_series.append((node, merged))
            # 节点基线成功率 = 各实例基线中位数的均值；和上面服务级聚合的口径保持一致
            node_baselines = [
                b.median for b in (ctx.baseline(node, inst, metric) for inst in instances)
                if b.median == b.median
            ]
            baseline_health *= (statistics.fmean(node_baselines) if node_baselines else 100.0) / 100.0
            instance_mean = sum(merged.values()) / len(merged) if merged else 100.0
            if instance_mean < weakest_value:
                weakest_value = instance_mean
                weakest = node

        epochs = set(node_series[0][1])
        for _, mapping in node_series[1:]:
            epochs &= set(mapping)
        if not epochs:
            return None

        curve: dict[int, float] = {}
        for epoch in sorted(epochs):
            health = 1.0
            for _, mapping in node_series:
                health *= mapping[epoch] / 100.0
            curve[epoch] = health * 100.0

        worst_epoch = min(curve, key=lambda e: curve[e])
        series = MetricSeries(
            service=path[0],
            instance=weakest,
            metric=metric,
            points=[(from_epoch(e), curve[e]) for e in sorted(curve)],
            unit="%",
        )
        return curve[worst_epoch], baseline_health * 100.0, weakest, series

    def _deviation_bounds(self) -> tuple[float, float]:
        values = [
            abs(float(spec.when["deviation_pct"]))
            for spec in self.levels
            if "deviation_pct" in spec.when
        ]
        if not values:
            return 0.0, 1.0
        return min(values), max(values)


def _service_level(per_instance: dict[str, dict[int, float]]) -> dict[int, float]:
    """把同一服务的多实例成功率聚合成服务级。

    用均值而不是最小值：服务对外表现是「总请求里有多少成功」，
    一个实例全挂但流量已被摘除，服务整体未必不可用。用最小值会过度告警。
    """
    epochs = set(next(iter(per_instance.values())))
    for mapping in per_instance.values():
        epochs &= set(mapping)
    return {
        epoch: sum(mapping[epoch] for mapping in per_instance.values()) / len(per_instance)
        for epoch in epochs
    }


__all__ = ["MAX_PATHS", "ChainHealthRule"]
