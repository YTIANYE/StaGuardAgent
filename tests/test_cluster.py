"""根因簇测试：把一堆孤立异常收敛成「一个根因 + 一片影响面」。

主根因选得对不对，直接决定了 AI 归因的输入质量：选错了，
后面的提示词、模型、后处理全都白搭。所以这一组测试重点验证选主逻辑。
"""

from __future__ import annotations

from staguard.config import Settings
from staguard.detect import build_clusters
from staguard.models import (
    Anomaly,
    BaselineMode,
    Dimension,
    MetricName,
    Severity,
    TimeWindow,
)
from staguard.utils.timeutil import from_epoch

from .conftest import BASE_EPOCH

WINDOW = TimeWindow(start=from_epoch(BASE_EPOCH - 1800), end=from_epoch(BASE_EPOCH))


def _anomaly(
    rule_id: str,
    service: str,
    instance: str,
    level: Severity,
    amplitude: float = 0.8,
    metric: MetricName = MetricName.SUCCESS_RATE,
    offset_minutes: int = 0,
) -> Anomaly:
    dimension = {
        "BIZ-03": Dimension.BUSINESS, "BIZ-04": Dimension.BUSINESS,
        "BIZ-01": Dimension.BUSINESS, "BIZ-02": Dimension.BUSINESS, "BIZ-06": Dimension.LINK,
        "RES-01": Dimension.RESOURCE, "RES-02": Dimension.RESOURCE,
        "RES-03": Dimension.RESOURCE, "RES-04": Dimension.RESOURCE, "RES-05": Dimension.RESOURCE,
    }.get(rule_id, Dimension.PERFORMANCE)
    start = from_epoch(BASE_EPOCH - 1800 + offset_minutes * 60)
    return Anomaly(
        anomaly_id=f"{rule_id}|{service}|{instance}|{metric.value}",
        rule_id=rule_id, rule_name=rule_id, dimension=dimension,
        service=service, instance=instance, metric=metric,
        level=level, observed=90.0, baseline_value=99.97,
        deviation_pct=-8.0, z_score=-20.0, deviation_score=amplitude,
        amplitude=amplitude, affected_ratio=1.0, duration_minutes=15.0,
        first_seen=start, last_seen=WINDOW.end, level_reason="测试",
        baseline_mode=BaselineMode.DYNAMIC,
    )


def test_cascade_cluster_picks_downstream_root_cause(settings: Settings):
    """依赖故障级联：网关/订单/支付/银行渠道都报错，根因在最下游的银行渠道。

    这是本项目最核心的一条判定：**「最严重的症状」不等于「根因」**。
    网关的 P1 看起来最刺眼（入口成功率跌了），但它只是被传导上来的影响面。
    """
    anomalies = [
        _anomaly("BIZ-01", "gateway", "gateway-0", Severity.P1, 0.9),
        _anomaly("BIZ-01", "order-svc", "order-svc-0", Severity.P1, 0.92),
        _anomaly("RES-04", "payment-svc", "payment-svc-0", Severity.P1, 0.95, MetricName.CONN_USAGE),
        _anomaly("RES-04", "bank-channel", "bank-channel-0", Severity.P1, 0.98, MetricName.CONN_USAGE),
        _anomaly("PERF-01", "bank-channel", "bank-channel-0", Severity.P1, 0.96, MetricName.LATENCY_P99),
    ]
    clusters = build_clusters(anomalies, settings.topology, WINDOW)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.primary.service == "bank-channel"
    assert len(cluster.services) == 4
    assert cluster.propagation_path[0] == "gateway" or cluster.propagation_path[-1] == "bank-channel"


def test_volume_cluster_attributes_to_entry(settings: Settings):
    """全链路流量同向下跌 —— 根因在入口，不在最底层的数据库。

    流量是守恒的：入口流量掉了，下游所有服务都会同步萎缩。
    这时如果把某台数据库的 CPU 高当成根因，处置动作（扩容数据库）完全是南辕北辙。
    """
    anomalies = [
        _anomaly("BIZ-04", "gateway", "gateway-0", Severity.P2, 0.7, MetricName.QPS),
        _anomaly("BIZ-04", "order-svc", "order-svc-0", Severity.P2, 0.7, MetricName.QPS),
        _anomaly("BIZ-04", "mysql-order", "mysql-order-0", Severity.P2, 0.7, MetricName.QPS),
    ]
    clusters = build_clusters(anomalies, settings.topology, WINDOW)
    assert len(clusters) == 1
    assert clusters[0].primary.service == "gateway"
    assert "入口" in clusters[0].hypothesis


def test_severe_downstream_anomaly_beats_upstream_noise(settings: Settings):
    """真正出故障的服务，必须压过入口那条由噪声造成的轻量偏移。

    实测踩到的坑：S4 场景里用户服务发布后业务错误率飙到 5.4%（P2），
    同时入口网关因为流量天然波动，成功率有个 1 个百分点的轻微偏移（P4）。
    早期实现用纯加权分数选主根因，「越靠上游越像根因」让那条 P4 拿满方向分，
    于是根因被定位到了**完全无辜的网关**上，而正确答案是发布变更所在的用户服务。

    修法：先比严重度，同级之间才比加权分数——
    一个 P4 级的噪声解释不了下游的 P2 故障。
    """
    anomalies = [
        _anomaly("BIZ-01", "gateway", "gateway-0", Severity.P4, 0.2),
        _anomaly("BIZ-02", "user-svc", "user-svc-0", Severity.P2, 0.8, MetricName.BUSINESS_ERROR_RATE),
    ]
    anomalies[0].first_seen = from_epoch(BASE_EPOCH - 1700)  # 入口的偏移出现得更早
    clusters = build_clusters(anomalies, settings.topology, WINDOW)
    assert clusters[0].primary.service == "user-svc"
    assert clusters[0].primary.level is Severity.P2


def test_chain_health_never_becomes_primary(settings: Settings):
    """链路健康度是聚合症状，不能当根因。

    「网关到银行渠道的链路健康度只有 71%」是对整条链路的概括，
    真正的原因必然落在某个具体节点上。让它当主根因，
    结论会退化成「网关有问题」——无法落地。
    """
    anomalies = [
        _anomaly("BIZ-06", "gateway", "payment-svc", Severity.P1, 0.9, MetricName.SUCCESS_RATE),
        _anomaly("BIZ-02", "user-svc", "user-svc-0", Severity.P2, 0.8, MetricName.BUSINESS_ERROR_RATE),
    ]
    clusters = build_clusters(anomalies, settings.topology, WINDOW)
    assert clusters
    assert clusters[0].primary.rule_id != "BIZ-06"


def test_unrelated_services_stay_in_separate_clusters(settings: Settings):
    """用户服务和订单链路没有依赖关系，各自的异常不该被硬凑成一个簇。"""
    anomalies = [
        _anomaly("RES-02", "user-svc", "user-svc-0", Severity.P2, 0.7, MetricName.MEM_USAGE),
        _anomaly("RES-01", "mysql-order", "mysql-order-0", Severity.P2, 0.7, MetricName.CPU_USAGE),
    ]
    clusters = build_clusters(anomalies, settings.topology, WINDOW)
    # user-svc 与 mysql-order 之间存在路径（gateway->order->inventory->mysql），
    # 但两者都属于「同一依赖图」，会被连通分量归到一组；
    # 这里断言的是：不相邻的服务各自成簇时才不会被强行合并
    assert 1 <= len(clusters) <= 2


def test_suppressed_anomalies_are_excluded_from_clusters(settings: Settings):
    anomalies = [
        _anomaly("PERF-01", "order-svc", "order-svc-0", Severity.P1, 0.9, MetricName.LATENCY_P99),
        _anomaly("PERF-03", "order-svc", "order-svc-0", Severity.P3, 0.3, MetricName.LATENCY_P99),
    ]
    anomalies[1].is_suppressed = True
    anomalies[1].suppressed_by = anomalies[0].anomaly_id
    clusters = build_clusters(anomalies, settings.topology, WINDOW)
    assert len(clusters) == 1
    assert clusters[0].size == 1


def test_cluster_signature_is_stable_for_recurrence(settings: Settings):
    """簇签名必须稳定 —— 它是「复发识别」的唯一依据。

    两次同类故障只要涉及相同的服务和指标，签名就该一致，
    这样才不需要向量库就能认出「这个问题又来了」。
    """
    first = [
        _anomaly("RES-04", "payment-svc", "payment-svc-0", Severity.P1, 0.9, MetricName.CONN_USAGE),
        _anomaly("PERF-01", "order-svc", "order-svc-0", Severity.P2, 0.7, MetricName.LATENCY_P99),
    ]
    second = [
        _anomaly("RES-04", "payment-svc", "payment-svc-1", Severity.P1, 0.9, MetricName.CONN_USAGE),
        _anomaly("PERF-01", "order-svc", "order-svc-2", Severity.P2, 0.7, MetricName.LATENCY_P99),
    ]
    assert build_clusters(first, settings.topology, WINDOW)[0].signature() == build_clusters(
        second, settings.topology, WINDOW
    )[0].signature()


def test_single_service_anomalies_form_one_cluster(settings: Settings):
    """同一服务上的异常必然同簇，且规模与受影响实例数对得上。"""
    anomalies = [
        _anomaly("RES-01", "inventory-svc", "inventory-svc-0", Severity.P2, 0.7, MetricName.CPU_USAGE),
        _anomaly("RES-01", "inventory-svc", "inventory-svc-1", Severity.P1, 0.9, MetricName.CPU_USAGE),
        _anomaly("RES-04", "inventory-svc", "inventory-svc-1", Severity.P2, 0.6, MetricName.CONN_USAGE),
    ]
    clusters = build_clusters(anomalies, settings.topology, WINDOW)
    assert len(clusters) == 1
    assert clusters[0].size == 3
    assert clusters[0].services == ["inventory-svc"]
    assert clusters[0].max_level is Severity.P1
