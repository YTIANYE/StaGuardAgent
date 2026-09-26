"""多粒度巡检统计：把异常清单与根因簇折算成服务维度 / 集群维度的统计表。

三处口径需要在实现上固定下来，否则同一个数字在不同读者眼里会有不同含义：

1. **只统计未抑制异常**。被抑制的那条与代表它的那条是同一服务同一指标上的同一类问题，
   计入就会把一个故障数成两遍；
2. **服务维度覆盖全部服务**（含外部依赖）。无异常的服务也要出现在表里，
   「哪些服务是干净的」本身就是统计要回答的问题；它的 `max_level` 为空；
3. **影响面 = 出现异常的实例数 / 实例总数**，实例去重后计数。用占比而不是绝对条数，
   是因为「3 个实例全中招」和「10 个实例里坏了 1 个」是完全不同的两件事。
   分母取配置声明的实例清单（= 巡检范围），分子只认**声明过的实例名**：
   链路健康度（BIZ-06）这类非实例级异常，`instance` 字段里装的是链路上最弱节点的
   *服务名*，直接计数会算出 133% 这种不可能的影响面。它们仍计入服务的异常数，
   只是不占实例影响面——「链路整体劣化」和「某台机器坏了」本来就不是一件事。

统计只读，不参与评分和分级——避免出现第二套互相矛盾的数字口径。
"""

from __future__ import annotations

from ..models import Anomaly, AnomalyCluster, ClusterStat, ServiceStat, Severity, Topology
from ..utils.text import fmt

NO_LEVEL = 99
"""排序用的哨兵：无异常的条目排在最后。"""


def build_service_stats(
    topology: Topology,
    anomalies: list[Anomaly],
    clusters: list[AnomalyCluster],
) -> list[ServiceStat]:
    """服务维度统计。有异常的排在前面，同级按异常数降序。"""
    active = [a for a in anomalies if not a.is_suppressed]
    by_service: dict[str, list[Anomaly]] = {}
    for anomaly in active:
        by_service.setdefault(anomaly.service, []).append(anomaly)

    root_cause_of: dict[str, list[str]] = {}
    in_clusters: dict[str, list[str]] = {}
    for cluster in clusters:
        root_cause_of.setdefault(cluster.primary.service, []).append(cluster.cluster_id)
        for service in cluster.services:
            in_clusters.setdefault(service, []).append(cluster.cluster_id)

    stats: list[ServiceStat] = []
    for service in topology.names():
        node = topology.node(service)
        items = by_service.get(service, [])
        worst = min(items, key=lambda a: (a.level, -a.deviation_score)) if items else None
        declared = set(node.instances) if node else set()
        stats.append(
            ServiceStat(
                service=service,
                cluster=topology.cluster_of(service),
                cluster_label=topology.cluster_label(topology.cluster_of(service)),
                tier=node.tier if node else None,
                owner=node.owner if node else None,
                criticality=node.criticality if node else 0.5,
                anomaly_count=len(items),
                max_level=worst.level if worst else None,
                affected_instances=sorted(i for i in {a.instance for a in items} if i in declared),
                total_instances=len(node.instances) if node else 0,
                rule_ids=sorted({a.rule_id for a in items}),
                worst_metric=worst.metric.label if worst else None,
                worst_summary=_worst_summary(worst),
                root_cause_of=sorted(set(root_cause_of.get(service, []))),
                in_clusters=sorted(set(in_clusters.get(service, []))),
            )
        )
    stats.sort(key=_service_sort_key)
    return stats


def build_cluster_stats(topology: Topology, service_stats: list[ServiceStat]) -> list[ClusterStat]:
    """集群维度统计，由服务维度聚合而来。"""
    groups: dict[str, list[ServiceStat]] = {}
    for stat in service_stats:
        groups.setdefault(stat.cluster, []).append(stat)

    stats: list[ClusterStat] = []
    for cluster, members in groups.items():
        levels = [m.max_level for m in members if m.max_level is not None]
        stats.append(
            ClusterStat(
                cluster=cluster,
                label=topology.cluster_label(cluster),
                service_count=len(members),
                affected_service_count=sum(1 for m in members if not m.is_healthy),
                anomaly_count=sum(m.anomaly_count for m in members),
                max_level=min(levels) if levels else None,
                affected_instances=sum(len(m.affected_instances) for m in members),
                total_instances=sum(m.total_instances for m in members),
                root_cause_count=sum(len(m.root_cause_of) for m in members),
            )
        )
    stats.sort(key=_cluster_sort_key)
    return stats


def build_granularity_stats(
    topology: Topology,
    anomalies: list[Anomaly],
    clusters: list[AnomalyCluster],
) -> tuple[list[ServiceStat], list[ClusterStat]]:
    """一次算两张表，保证两者的口径不可能不一致。"""
    service_stats = build_service_stats(topology, anomalies, clusters)
    return service_stats, build_cluster_stats(topology, service_stats)


# --------------------------------------------------------------------------- 内部
def _worst_summary(anomaly: Anomaly | None) -> str | None:
    if anomaly is None:
        return None
    observed = fmt(anomaly.observed, 2)
    if anomaly.baseline_value is None:
        return f"实测 {observed}（{anomaly.level.display()}）"
    return (
        f"实测 {observed}，基线 {fmt(anomaly.baseline_value, 2)}"
        f"（{anomaly.level.display()}）"
    )


def _level_rank(level: Severity | None) -> int:
    return NO_LEVEL if level is None else int(level)


def _service_sort_key(stat: ServiceStat) -> tuple[int, int, str]:
    return (_level_rank(stat.max_level), -stat.anomaly_count, stat.service)


def _cluster_sort_key(stat: ClusterStat) -> tuple[int, int, str]:
    return (_level_rank(stat.max_level), -stat.anomaly_count, stat.cluster)
