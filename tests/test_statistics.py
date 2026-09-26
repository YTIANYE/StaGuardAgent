"""多粒度统计测试：服务维度与集群维度的口径。

这一组测试锁的是**口径**而不是数值：统计最容易出的错不是算错，
而是把「不该算进去的东西」算进去了——被抑制的异常、链路级异常的伪实例名、
没有异常的服务该不该出现在表里。这些一旦错，报告上看起来一切正常。
"""

from __future__ import annotations

import pytest

from staguard.config import ConfigError, Settings, build_settings
from staguard.detect import build_cluster_stats, build_granularity_stats, build_service_stats
from staguard.models import (
    DEFAULT_CLUSTER,
    Anomaly,
    AnomalyCluster,
    Dimension,
    MetricName,
    ServiceNode,
    Severity,
    TimeWindow,
    Topology,
)
from staguard.utils.timeutil import from_epoch

from .conftest import BASE_EPOCH

WINDOW = TimeWindow(start=from_epoch(BASE_EPOCH - 1800), end=from_epoch(BASE_EPOCH))


def _anomaly(
    service: str,
    instance: str,
    level: Severity = Severity.P2,
    rule_id: str = "BIZ-01",
    metric: MetricName = MetricName.SUCCESS_RATE,
    suppressed: bool = False,
    deviation_score: float = 0.5,
) -> Anomaly:
    return Anomaly(
        anomaly_id=f"{rule_id}|{service}|{instance}|{metric.value}",
        rule_id=rule_id,
        rule_name="测试规则",
        dimension=Dimension.BUSINESS,
        service=service,
        instance=instance,
        metric=metric,
        level=level,
        observed=90.0,
        baseline_value=99.9,
        deviation_pct=-9.9,
        deviation_score=deviation_score,
        first_seen=WINDOW.start,
        last_seen=WINDOW.end,
        is_suppressed=suppressed,
    )


def _cluster(cluster_id: str, primary: Anomaly, members: list[Anomaly]) -> AnomalyCluster:
    services = sorted({a.service for a in members})
    return AnomalyCluster(
        cluster_id=cluster_id,
        primary=primary,
        members=members,
        propagation_path=services,
        services=services,
        max_level=min(a.level for a in members),
    )


def _topology(clusters: dict[str, str] | None = None) -> Topology:
    """两个集群、三个服务的小拓扑，用于精确断言。"""
    nodes = {
        "gateway": ServiceNode(name="gateway", instances=["gateway-0", "gateway-1"], cluster="trade"),
        "order-svc": ServiceNode(
            name="order-svc", instances=["order-svc-0", "order-svc-1"], cluster="trade"
        ),
        "bank-channel": ServiceNode(
            name="bank-channel", instances=["bank-channel-0"], cluster="external"
        ),
    }
    labels = clusters or {"trade": "交易集群", "external": "外部依赖集群", DEFAULT_CLUSTER: "未分组"}
    return Topology(services=nodes, cluster_labels=labels)


def test_service_stats_count_and_worst_level() -> None:
    """异常数按服务聚合，最严重级别取该服务内最高的一条。"""
    anomalies = [
        _anomaly("gateway", "gateway-0", Severity.P3),
        _anomaly("gateway", "gateway-1", Severity.P1),
        _anomaly("bank-channel", "bank-channel-0", Severity.P4),
    ]
    stats = {s.service: s for s in build_service_stats(_topology(), anomalies, [])}

    assert stats["gateway"].anomaly_count == 2
    assert stats["gateway"].max_level is Severity.P1
    assert stats["bank-channel"].max_level is Severity.P4


def test_service_stats_instance_face_dedup() -> None:
    """同一实例上的多条异常只算一个受影响实例。"""
    anomalies = [
        _anomaly("gateway", "gateway-0", metric=MetricName.SUCCESS_RATE),
        _anomaly("gateway", "gateway-0", metric=MetricName.LATENCY_P99, rule_id="PERF-01"),
    ]
    stat = next(s for s in build_service_stats(_topology(), anomalies, []) if s.service == "gateway")

    assert stat.affected_instances == ["gateway-0"]
    assert stat.affected_ratio == 0.5
    assert stat.rule_ids == ["BIZ-01", "PERF-01"]


def test_suppressed_anomalies_are_excluded() -> None:
    """被抑制的异常不参与统计——它和代表它的那条是同一类问题。"""
    anomalies = [
        _anomaly("gateway", "gateway-0", Severity.P1),
        _anomaly("gateway", "gateway-0", Severity.P3, rule_id="PERF-03", suppressed=True),
    ]
    stat = next(s for s in build_service_stats(_topology(), anomalies, []) if s.service == "gateway")

    assert stat.anomaly_count == 1
    assert stat.rule_ids == ["BIZ-01"]


def test_non_instance_anomaly_does_not_count_into_face() -> None:
    """链路级异常的 instance 装的是下游服务名，不能计入实例影响面。"""
    anomalies = [
        _anomaly("gateway", "bank-channel", Severity.P1, rule_id="BIZ-06"),  # 伪实例名
        _anomaly("gateway", "gateway-0", Severity.P2),
    ]
    stat = next(s for s in build_service_stats(_topology(), anomalies, []) if s.service == "gateway")

    assert stat.anomaly_count == 2
    assert stat.affected_instances == ["gateway-0"]
    assert stat.affected_ratio is not None and stat.affected_ratio <= 1.0


def test_healthy_services_are_present_and_sorted_last() -> None:
    """无异常的服务同样出现在统计里，且排在最后。"""
    anomalies = [_anomaly("gateway", "gateway-0", Severity.P1)]
    stats = build_service_stats(_topology(), anomalies, [])

    assert {s.service for s in stats} == {"gateway", "order-svc", "bank-channel"}
    assert stats[0].service == "gateway"
    healthy = next(s for s in stats if s.service == "order-svc")
    assert healthy.max_level is None
    assert healthy.is_healthy
    assert healthy.affected_ratio == 0.0


def test_cluster_stats_aggregate_across_services() -> None:
    """集群维度跨服务聚合：异常数求和、级别取最高、实例数求和。"""
    anomalies = [
        _anomaly("gateway", "gateway-0", Severity.P2),
        _anomaly("order-svc", "order-svc-0", Severity.P1),
    ]
    service_stats = build_service_stats(_topology(), anomalies, [])
    clusters = {c.cluster: c for c in build_cluster_stats(_topology(), service_stats)}

    trade = clusters["trade"]
    assert trade.label == "交易集群"
    assert trade.service_count == 2
    assert trade.affected_service_count == 2
    assert trade.anomaly_count == 2
    assert trade.max_level is Severity.P1
    assert trade.affected_instances == 2
    assert trade.total_instances == 4

    external = clusters["external"]
    assert external.is_healthy
    assert external.affected_ratio == 0.0


def test_root_cause_attribution_is_recorded() -> None:
    """根因归属：主根因服务记在 root_cause_of，簇成员记在 in_clusters。"""
    root = _anomaly("bank-channel", "bank-channel-0", Severity.P1, rule_id="RES-04")
    impact = _anomaly("gateway", "gateway-0", Severity.P2)
    clusters = [_cluster("CLUS-01", root, [root, impact])]

    stats = {s.service: s for s in build_service_stats(_topology(), [root, impact], clusters)}

    assert stats["bank-channel"].root_cause_of == ["CLUS-01"]
    assert stats["bank-channel"].in_clusters == ["CLUS-01"]
    assert stats["gateway"].root_cause_of == []
    assert stats["gateway"].in_clusters == ["CLUS-01"]

    cluster_stats = {c.cluster: c for c in build_cluster_stats(_topology(), list(stats.values()))}
    assert cluster_stats["external"].root_cause_count == 1
    assert cluster_stats["trade"].root_cause_count == 0


def test_granularity_stats_share_one_pipeline() -> None:
    """两张表由同一次计算产出，口径不可能不一致。"""
    anomalies = [_anomaly("gateway", "gateway-0", Severity.P1)]
    service_stats, cluster_stats = build_granularity_stats(_topology(), anomalies, [])

    assert sum(s.anomaly_count for s in service_stats) == sum(c.anomaly_count for c in cluster_stats)


def test_service_without_cluster_falls_back_to_default() -> None:
    """未配置集群的服务归入 default，展示名为「未分组」。"""
    topology = Topology(
        services={"legacy-svc": ServiceNode(name="legacy-svc", instances=["legacy-svc-0"])},
        cluster_labels={"default": "未分组"},
    )
    stats = build_service_stats(topology, [], [])

    assert stats[0].cluster == DEFAULT_CLUSTER
    assert stats[0].cluster_label == "未分组"


def test_undefined_cluster_reference_is_rejected() -> None:
    """服务引用了未定义的集群 → 启动期直接失败，而不是静默分错组。"""
    from staguard.config.schemas import ServicesConfig

    config = ServicesConfig(
        clusters={"trade": "交易集群"},
        services={"gateway": {"instances": ["gateway-0"], "cluster": "tarde"}},  # 拼错
    )
    assert config.undefined_clusters() == [("gateway", "tarde")]


def test_real_config_clusters_are_consistent(settings: Settings) -> None:
    """仓库里的 services.yaml 必须自洽：没有悬空引用，且每个服务都有集群。"""
    assert settings.services.undefined_clusters() == []
    topology = settings.topology
    assert all(topology.cluster_of(name) != "" for name in topology.names())
    assert len(topology.cluster_members()) >= 2


def test_build_settings_rejects_dangling_cluster(tmp_path) -> None:
    """走真实的 build_settings 路径，确认悬空引用会抛 ConfigError。"""
    source = settings_dir()
    target = tmp_path / "config"
    target.mkdir()
    for item in source.iterdir():
        (target / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")
    services = target / "services.yaml"
    services.write_text(
        services.read_text(encoding="utf-8").replace("cluster: trade", "cluster: trade-x"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        build_settings(target)


def settings_dir():  # noqa: ANN201
    from pathlib import Path

    from staguard.config import AppConfig

    return Path(AppConfig().config_dir)
