"""根因簇：把一堆孤立异常收敛成「一个根因 + 一片影响面」。

**为什么这一步决定了 AI 归因的质量**：如果直接把 24 条异常丢给大模型，
它只能给出 24 条彼此独立的猜测——因为输入本身就没有告诉它这些异常之间有关系。
先按拓扑把它们聚成簇，再让 AI 针对「簇」做一次归因，它才可能说出
「根因在银行渠道，其余都是它传导上来的影响」。顺带还省掉大量 token。

聚簇规则（两条，简单但有效）：
    1. 同一服务上的异常必在同一簇；
    2. 两个服务若在依赖拓扑上互为上下游，其异常进同一簇。

主根因的选取不是拍脑袋，而是四个信号加权：
    - **信号性质**：资源/性能类异常（瓶颈）更像「因」，业务类（成功率、链路、流量）更像「果」；
    - **严重度**：P1 优先；
    - **方向**：瓶颈类越靠下游越可能是根因（下游慢拖垮上游）；
             症状类越靠上游越可能是根因（入口最先看到影响）；
    - **时序**：最先出现症状的那个。

外加一条**流量类专项**：如果全链路出现同向的流量变化（突增/突降），
根因指向入口，而不是把某台数据库的 CPU 高当成根因——
那条 CPU 高只是流量灌进来的结果，修它没有任何意义。
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ..models import Anomaly, AnomalyCluster, Dimension, Severity, TimeWindow, Topology

logger = logging.getLogger(__name__)

CAUSAL_AFFINITY: dict[Dimension, float] = {
    Dimension.RESOURCE: 1.00,
    Dimension.PERFORMANCE: 0.70,
    Dimension.BUSINESS: 0.30,
    Dimension.LINK: 0.20,
}
"""信号性质权重：这条异常本身有多像「原因」。"""

LEVEL_SCORE: dict[Severity, float] = {Severity.P1: 1.0, Severity.P2: 0.70, Severity.P3: 0.45, Severity.P4: 0.20}

SCORE_WEIGHTS = {"affinity": 0.40, "level": 0.25, "direction": 0.20, "earliest": 0.15}

VOLUME_RULES = frozenset({"BIZ-03", "BIZ-04"})
"""流量类规则。全链路同向变化时，根因在入口流量，不在任何单个服务。"""

CHAIN_RULE = "BIZ-06"
"""链路健康度。它是跨节点的聚合指标，只能当「症状」，不能当「根因」。"""

CAUSAL_THRESHOLD = 0.6
"""亲和度高于它按「瓶颈类」判定方向（越下游越像根因），否则按「症状类」（越上游越像）。"""


def build_clusters(
    anomalies: list[Anomaly],
    topology: Topology,
    window: TimeWindow,
) -> list[AnomalyCluster]:
    active = [a for a in anomalies if not a.is_suppressed]
    if not active:
        return []

    groups = _group_by_topology(active, topology)
    clusters: list[AnomalyCluster] = []
    for index, group in enumerate(groups, start=1):
        cluster = _build_cluster(f"CLUS-{index:02d}", group, topology, window)
        for anomaly in cluster.all_anomalies():
            anomaly.cluster_id = cluster.cluster_id
        clusters.append(cluster)

    clusters.sort(key=lambda c: (c.max_level, -c.primary.deviation_score))
    logger.info(
        "聚簇完成：%d 条异常 -> %d 个根因簇（最大簇 %d 条）",
        len(active), len(clusters), max((c.size for c in clusters), default=0),
    )
    return clusters


# --------------------------------------------------------------------------- 聚簇
def _group_by_topology(anomalies: list[Anomaly], topology: Topology) -> list[list[Anomaly]]:
    """并查集：按「同服务 + 拓扑相邻」把异常连成连通分量。"""
    parents = list(range(len(anomalies)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parents[root_r] = root_l

    by_service: dict[str, list[int]] = defaultdict(list)
    for index, anomaly in enumerate(anomalies):
        by_service[anomaly.service].append(index)

    related = _related_service_pairs(topology)
    for services in _connected_service_sets(topology, related):
        indices = [i for service in services for i in by_service.get(service, [])]
        for other in indices[1:]:
            union(indices[0], other)

    groups: dict[int, list[Anomaly]] = defaultdict(list)
    for index, anomaly in enumerate(anomalies):
        groups[find(index)].append(anomaly)
    return list(groups.values())


def _related_service_pairs(topology: Topology) -> dict[str, set[str]]:
    related: dict[str, set[str]] = {name: set() for name in topology.names()}
    for name in topology.names():
        neighbours = set(topology.ancestors(name)) | set(topology.descendants(name))
        related[name] = neighbours
    return related


def _connected_service_sets(topology: Topology, related: dict[str, set[str]]) -> list[set[str]]:
    """把服务按「是否在同一条依赖链上」分成若干连通分量。"""
    seen: set[str] = set()
    components: list[set[str]] = []
    for name in topology.names():
        if name in seen:
            continue
        stack = [name]
        component: set[str] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            seen.add(current)
            stack.extend(related.get(current, set()) - component)
        components.append(component)
    return components


# --------------------------------------------------------------------------- 主根因
def _build_cluster(
    cluster_id: str,
    group: list[Anomaly],
    topology: Topology,
    window: TimeWindow,
) -> AnomalyCluster:
    services = sorted({a.service for a in group})
    primary = _pick_primary(group, topology, services)
    members = [a for a in group if a.anomaly_id != primary.anomaly_id]
    max_level = min((a.level for a in group), default=Severity.P4)
    return AnomalyCluster(
        cluster_id=cluster_id,
        primary=primary,
        members=members,
        propagation_path=_propagation_path(primary.service, set(services), topology),
        hypothesis=_hypothesis(group, primary, topology, services),
        services=services,
        max_level=max_level,
    )


def _pick_primary(group: list[Anomaly], topology: Topology, services: list[str]) -> Anomaly:
    candidates = [a for a in group if a.rule_id in VOLUME_RULES]
    if not candidates:
        # 链路健康度是「聚合症状」：它好用，但**不能当根因**。
        # 「网关到银行渠道的链路健康度只有 71%」是对整条链路的概括，
        # 真正的原因必然在链路的某个节点上——让聚合症状当主根因，
        # 会把归因引向「网关有问题」这种没法落地的结论。
        candidates = [a for a in group if a.rule_id != CHAIN_RULE] or group
    if len(candidates) < len(group):
        logger.debug("主根因候选已从 %d 条收窄到 %d 条", len(group), len(candidates))

    earliest = min(a.first_seen for a in candidates)
    latest = max(a.first_seen for a in candidates)
    span = max(1.0, (latest - earliest).total_seconds())

    def score(anomaly: Anomaly) -> float:
        # 用「这条异常自己」的性质，而不是它所在服务的最强信号。
        # 主根因最终要落到一个具体的异常上，拿服务维度的最大值会系统性地
        # 偏向异常条数多的服务——而条数多恰恰说明它是被影响的那一方。
        affinity = CAUSAL_AFFINITY.get(anomaly.dimension, 0.3)
        depth = _depth_norm(anomaly.service, services, topology)
        # 瓶颈类故障沿拓扑向上游传导，所以越靠下游越像根因；
        # 症状类（成功率/流量）全链路同现时，越靠上游越接近源头。
        direction = (1.0 - depth) if affinity >= CAUSAL_THRESHOLD else depth
        earliest_score = 1.0 - (anomaly.first_seen - earliest).total_seconds() / span
        return (
            SCORE_WEIGHTS["affinity"] * affinity
            + SCORE_WEIGHTS["level"] * LEVEL_SCORE[anomaly.level]
            + SCORE_WEIGHTS["direction"] * direction
            + SCORE_WEIGHTS["earliest"] * earliest_score
        )

    # **先比严重度，同级之间再比加权分数。**
    #
    # 曾经用纯加权分数取最大值，结果在 S4 场景上翻了车：
    # 入口网关一条「成功率略低于 SLO」的 P4（流量天然波动导致的噪声）
    # 因为「越靠上游越像根因」拿满了方向分，压过了真正出故障的
    # user-svc 上那条 P2 业务错误率——根因被定位到了完全无辜的服务上。
    #
    # 严重度优先在直觉上也更站得住：一个 P3 级的下游问题，
    # 解释不了上游的 P1 崩溃；而最严重的那个异常，本身就是最值得优先解释的对象。
    # 同级之间的排序才需要方向、时序这些细腻的信号。
    return min(candidates, key=lambda a: (a.level, -score(a), a.anomaly_id))


def _depth_norm(service: str, services: list[str], topology: Topology) -> float:
    """在受影响服务集合中，本服务有多「靠上游」。

    定义为「有多少个受影响服务在我的下游」占比：
    网关的下游覆盖了几乎所有受影响服务，所以接近 1；最底层的数据库没有下游，所以是 0。
    """
    if len(services) < 2:
        return 0.5
    others = set(services) - {service}
    downstream = len(others & set(topology.descendants(service)))
    return downstream / (len(services) - 1)


# --------------------------------------------------------------------------- 传播路径
def _propagation_path(origin: str, services: set[str], topology: Topology) -> list[str]:
    """从根因服务出发，沿依赖方向穿过受影响服务的最长路径。"""
    best: list[str] = [origin]

    def walk(service: str, trail: list[str]) -> None:
        nonlocal best
        if len(trail) > len(best):
            best = list(trail)
        for child in topology.downstream(service):
            if child in trail:
                continue
            walk(child, trail + [child])

    walk(origin, [origin])
    if len(best) == 1:
        # 根因在最下游时，反过来展示它到上游影响面的路径
        for ancestor in topology.ancestors(origin):
            path = topology.path_between(ancestor, origin) or []
            if len(path) > len(best):
                best = path
    return [node for node in best if node in services] or [origin]


def _hypothesis(
    group: list[Anomaly],
    primary: Anomaly,
    topology: Topology,
    services: list[str],
) -> str:
    """规则侧的传播假设。**这不是结论**，只是给 AI 的一个待验证猜想——
    报告里会和 AI 的结论并列展示，两者不一致时恰恰是最值得人工看一眼的地方。"""
    rules = {a.rule_id for a in group}
    if rules and rules <= VOLUME_RULES:
        return (
            f"全链路 {len(services)} 个服务出现同向流量变化，疑似入口业务流量波动"
            f"（而非任一服务自身故障）；根因指向 {primary.service}"
        )

    if len(services) == 1:
        affected = {a.instance for a in group}
        scope = "全部实例" if len(affected) > 1 else f"单实例 {primary.instance}"
        return f"{primary.service} 的 {scope} 出现 {primary.metric.label} 异常，未见向上游扩散"

    dependents = topology.descendants(primary.service)
    impacted = [s for s in services if s in dependents]
    if impacted:
        return (
            f"{primary.service} 的 {primary.metric.label} 异常沿依赖链向上游传导，"
            f"已影响 {len(impacted)} 个上游服务（{'、'.join(impacted)}），"
            f"建议优先排查 {primary.service}"
        )
    return f"{len(services)} 个服务出现关联异常，疑似同一根因传导，{primary.service} 的偏离最显著"
