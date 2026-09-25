"""异常聚合：去重、抑制、影响面回填。

规则引擎的输出是「原始发现」，不是「给运维看的结论」。中间必须再做三件事：

1. **去重**：一条规则在一个窗口内可能反复命中（每分钟一次），
   运维要看的是「订单服务成功率下跌，持续 23 分钟」，不是 23 条一样的记录；
2. **抑制**：同一实例同一指标上，P99 超时的 P1 已经说明问题，
   再报一条「抖动偏高」的 P3 只会稀释注意力；
3. **影响面**：异常的影响面天然是聚合量——「3 个实例全部中招」和「只有 1 个」
   是性质不同的两件事。所以它只能在所有规则跑完之后回填，
   再据此重算严重度分数。

一句话：**规则负责「找出来」，聚合负责「说清楚」**。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from ..models import Anomaly, AnomalyFinding, Severity, Topology

logger = logging.getLogger(__name__)


@dataclass
class AggregateResult:
    anomalies: list[Anomaly] = field(default_factory=list)
    """去重后的异常（含被抑制的），按严重度排序。"""
    suppressed: list[Anomaly] = field(default_factory=list)
    """被同指标更严重问题抑制掉的低级别异常。仍会落库，只是不进报告主表。"""
    affected_ratios: dict[str, float] = field(default_factory=dict)

    @property
    def active(self) -> list[Anomaly]:
        return [a for a in self.anomalies if not a.is_suppressed]

    def level_counts(self) -> dict[str, int]:
        counts = {s.code: 0 for s in Severity}
        for anomaly in self.active:
            counts[anomaly.level.code] += 1
        return counts


def aggregate(findings: list[AnomalyFinding], topology: Topology) -> AggregateResult:
    if not findings:
        return AggregateResult()

    merged = _merge(findings)
    affected = _affected_ratios(merged, topology)
    for anomaly in merged:
        anomaly.recompute_score(
            criticality=topology.criticality_of(anomaly.service),
            affected_ratio=affected.get(anomaly.anomaly_id, 1.0),
        )
    _suppress(merged)

    result = AggregateResult(
        anomalies=sorted(merged, key=lambda a: (a.level, -a.deviation_score)),
        suppressed=[a for a in merged if a.is_suppressed],
        affected_ratios=affected,
    )
    logger.info(
        "聚合完成：%d 条原始发现 -> %d 条异常（抑制 %d 条）",
        len(findings), len(result.anomalies), len(result.suppressed),
    )
    return result


def _merge(findings: list[AnomalyFinding]) -> list[Anomaly]:
    """按 dedup_key 融合。保留最严重的那条，并合并观测极值与命中次数。"""
    grouped: dict[str, list[AnomalyFinding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.dedup_key].append(finding)

    merged: list[Anomaly] = []
    for items in grouped.values():
        items.sort(key=lambda f: (f.level, -f.deviation_score))
        head = items[0]
        anomaly = Anomaly.from_finding(head)
        anomaly.merged_count = len(items)
        anomaly.related_rule_ids = sorted({f.rule_id for f in items})
        if len(items) > 1:
            # 同一条规则多次命中时，取最坏的观测值与最长的持续时长
            anomaly.duration_minutes = max(f.duration_minutes for f in items)
            worst = max(items, key=lambda f: f.deviation_score)
            anomaly.observed = worst.observed
            anomaly.amplitude = max(f.amplitude for f in items)
            anomaly.first_seen = min(f.window.start for f in items)
            anomaly.last_seen = max(f.window.end for f in items)
        merged.append(anomaly)
    return merged


def _affected_ratios(anomalies: list[Anomaly], topology: Topology) -> dict[str, float]:
    """同服务内被同一条规则命中的实例占比。

    这个数字是「服务级问题」和「单实例问题」的分水岭：
    3/3 实例中招说明是服务/依赖层面的问题，1/3 说明是那个实例自己的问题。
    """
    buckets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for anomaly in anomalies:
        buckets[(anomaly.rule_id, anomaly.service)].add(anomaly.instance)

    ratios: dict[str, float] = {}
    for anomaly in anomalies:
        instances = topology.node(anomaly.service)
        total = max(1, len(instances.instances) if instances else 1)
        hit = len(buckets[(anomaly.rule_id, anomaly.service)])
        ratios[anomaly.anomaly_id] = min(1.0, hit / total)
    return ratios


def _suppress(anomalies: list[Anomaly]) -> None:
    """同一 (服务, 实例, 指标) 上，低级别异常让位于更严重的那条。

    只在**跨规则**时抑制：同一条规则的不同级别已经在分级阶段收敛成一条了。
    """
    buckets: dict[tuple[str, str, str], list[Anomaly]] = defaultdict(list)
    for anomaly in anomalies:
        buckets[(anomaly.service, anomaly.instance, anomaly.metric.value)].append(anomaly)

    for items in buckets.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda a: (a.level, -a.deviation_score))
        head = items[0]
        for other in items[1:]:
            if other.level > head.level:
                other.is_suppressed = True
                other.suppressed_by = head.anomaly_id
