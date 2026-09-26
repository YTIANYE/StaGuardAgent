"""AI 归因模块测试。

三组断言，分别对应三条工程约束：
1. **AI 不可用时报告照样出得来** —— 降级链必须真的能兜住；
2. **模型编造的引用会被清掉** —— 防幻觉不能只靠提示词；
3. **模型漏掉的簇会被补齐** —— 覆盖完整性优先于来源纯净。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from staguard.ai import AIAnalyzer, FallbackAttributor
from staguard.ai.analyzer import _mark_recurrence
from staguard.ai.provider import DisabledProvider, LLMResponse, ProviderError, ProviderUnavailable
from staguard.config import Settings
from staguard.models import (
    Anomaly,
    AnomalyCluster,
    BaselineMode,
    ChangeEvent,
    ChangeType,
    Dimension,
    MetricName,
    RootCauseCategory,
    Severity,
)
from staguard.utils.timeutil import from_epoch

from .conftest import BASE_EPOCH, FakeContext

CHANGE_TS = from_epoch(BASE_EPOCH - 900)


def _ctx(settings: Settings) -> FakeContext:
    """最小的可用上下文：证明「证据包组装 -> 模型调用 -> 校验」这条链路真的跑通了。"""
    return FakeContext(settings, {})


def _anomaly(
    rule_id: str,
    service: str,
    metric: MetricName,
    level: Severity = Severity.P1,
    instance: str | None = None,
    first_seen_offset_minutes: int = 3,
) -> Anomaly:
    dimension = {
        "BIZ-02": Dimension.BUSINESS, "BIZ-03": Dimension.BUSINESS, "BIZ-04": Dimension.BUSINESS,
        "BIZ-01": Dimension.BUSINESS, "RES-01": Dimension.RESOURCE, "RES-02": Dimension.RESOURCE,
        "RES-03": Dimension.RESOURCE, "RES-04": Dimension.RESOURCE, "RES-05": Dimension.RESOURCE,
    }.get(rule_id, Dimension.PERFORMANCE)
    start = CHANGE_TS + timedelta(minutes=first_seen_offset_minutes)
    return Anomaly(
        anomaly_id=f"{rule_id}|{service}|{instance or service + '-0'}|{metric.value}",
        rule_id=rule_id, rule_name=rule_id, dimension=dimension,
        service=service, instance=instance or f"{service}-0", metric=metric,
        level=level, observed=90.0, baseline_value=99.97,
        deviation_pct=-8.0, z_score=-20.0, deviation_score=0.8, amplitude=0.8,
        affected_ratio=1.0, duration_minutes=15.0,
        first_seen=start, last_seen=start + timedelta(minutes=15),
        level_reason="测试用理由", baseline_mode=BaselineMode.DYNAMIC,
    )


def _cluster(rule_id: str, service: str, metric: MetricName, level: Severity = Severity.P1) -> AnomalyCluster:
    primary = _anomaly(rule_id, service, metric, level)
    return AnomalyCluster(
        cluster_id="CLUS-01", primary=primary, members=[], services=[service],
        propagation_path=[service], hypothesis="测试用假设", max_level=level,
    )


# --------------------------------------------------------------------- 决策树
def test_change_window_overrides_everything(settings: Settings):
    """异常起始与变更时间高度重合 —— 这是最强证据，优先级最高。"""
    change = ChangeEvent(
        change_id="CHG-1", ts=CHANGE_TS, service="payment-svc",
        type=ChangeType.RELEASE, version="v1.9.0", description="登录态校验重构",
    )
    attributor = FallbackAttributor([change])
    cluster = _cluster("RES-01", "payment-svc", MetricName.CPU_USAGE)
    finding = attributor._attribute(cluster)  # noqa: SLF001
    assert finding.root_cause_category is RootCauseCategory.CHANGE_INDUCED
    assert "高度重合" in finding.root_cause


def test_unrelated_change_does_not_cause_false_attribution(settings: Settings):
    """时间上接近但因果无关的变更，不能被当成根因。

    这是「看到变更就说是变更引入」的懒惰推理——真实环境里变更很多，
    如果 AI 不区分相关与无关，它的归因就没有任何价值。
    """
    unrelated = ChangeEvent(
        change_id="CHG-2", ts=CHANGE_TS, service="redis-session",
        type=ChangeType.CONFIG, description="缓存过期时间调整",
    )
    attributor = FallbackAttributor([unrelated])
    cluster = _cluster("RES-01", "payment-svc", MetricName.CPU_USAGE)
    finding = attributor._attribute(cluster)  # noqa: SLF001
    assert finding.root_cause_category is not RootCauseCategory.CHANGE_INDUCED


def test_dependency_failure_when_pool_saturated_with_latency(settings: Settings):
    """连接池打满 + 延时恶化，且**下游也在报警** —— 才能判为下游拖垮上游。"""
    primary = _anomaly("RES-04", "payment-svc", MetricName.CONN_USAGE)
    downstream = _anomaly("PERF-01", "bank-channel", MetricName.LATENCY_P99, Severity.P2)
    cluster = AnomalyCluster(
        cluster_id="CLUS-01", primary=primary, members=[downstream],
        services=["payment-svc", "bank-channel"], max_level=Severity.P1,
        propagation_path=["payment-svc", "bank-channel"],
    )
    attributor = FallbackAttributor([], topology=settings.topology)
    assert attributor._attribute(cluster).root_cause_category is RootCauseCategory.DEPENDENCY_FAILURE  # noqa: SLF001


def test_leaf_service_saturation_is_not_blamed_on_a_nonexistent_downstream(settings: Settings):
    """链路最下游的叶子服务，其连接池打满不能再归因给「它的下游」。

    这是实测踩到的坑：S1 场景里规则兜底对着无下游的银行渠道说
    「指向其下游依赖响应退化」——审阅者一核对拓扑就会发现这个结论不可观测。
    **宁可给出保守但站得住的结论，也不要给出专业但无依据的结论。**
    """
    primary = _anomaly("RES-04", "bank-channel", MetricName.CONN_USAGE)
    latency = _anomaly("PERF-01", "bank-channel", MetricName.LATENCY_P99, Severity.P1)
    cluster = AnomalyCluster(
        cluster_id="CLUS-01", primary=primary, members=[latency],
        services=["bank-channel", "payment-svc"], max_level=Severity.P1,
        propagation_path=["payment-svc", "bank-channel"],
    )
    attributor = FallbackAttributor([], topology=settings.topology)
    finding = attributor._attribute(cluster)  # noqa: SLF001
    assert finding.root_cause_category is RootCauseCategory.RESOURCE_BOTTLENECK
    assert "最下游" in finding.root_cause


def test_traffic_fluctuation_is_not_treated_as_fault(settings: Settings):
    """流量突增本身不是故障。

    AI 若把它渲染成 P1 事故就是误判：正确输出是容量评估 + 限流建议。
    """
    primary = _anomaly("BIZ-03", "order-svc", MetricName.QPS, Severity.P2)
    cpu = _anomaly("RES-01", "order-svc", MetricName.CPU_USAGE, Severity.P1)
    cluster = AnomalyCluster(
        cluster_id="CLUS-01", primary=primary, members=[cpu],
        services=["order-svc"], max_level=Severity.P1, propagation_path=["order-svc"],
    )
    finding = FallbackAttributor([])._attribute(cluster)  # noqa: SLF001
    assert finding.root_cause_category is RootCauseCategory.TRAFFIC_FLUCTUATION
    assert any("容量" in s or "余量" in s for s in finding.suggestions), "流量根因必须同时给出容量建议"


def test_memory_leak_maps_to_code_defect(settings: Settings):
    cluster = _cluster("RES-03", "payment-svc", MetricName.MEM_USAGE)
    finding = FallbackAttributor([])._attribute(cluster)  # noqa: SLF001
    assert finding.root_cause_category is RootCauseCategory.CODE_DEFECT
    assert finding.confidence > 0


# --------------------------------------------------------------------- 降级
def test_analyzer_falls_back_when_provider_unavailable(settings: Settings):
    """AI 不可用时必须产出完整可用的结论，并如实标记降级。"""
    provider = DisabledProvider("未配置 llm_api_key")
    analyzer = AIAnalyzer(settings, provider)
    cluster = _cluster("RES-01", "order-svc", MetricName.CPU_USAGE)

    analysis, meta = analyzer.analyze([cluster], _ctx(settings))
    assert meta.degraded
    assert "llm_api_key" in (meta.degraded_reason or "")
    assert len(analysis.findings) == 1
    assert analysis.uncertainties, "降级时必须自陈结论的局限"
    assert analysis.summary


def test_analyzer_survives_provider_errors(settings: Settings):
    """网络抖动 / 限流不能让巡检白跑 —— 重试耗尽后必须走降级。"""

    class FlakyProvider:
        name = "flaky"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def available(self):
            return True, None

        def complete_json(self, system: str, user: str) -> LLMResponse:  # noqa: ARG002
            self.calls += 1
            raise ProviderError("模拟 429 限流")

    provider = FlakyProvider()
    analyzer = AIAnalyzer(settings, provider)
    analysis, meta = analyzer.analyze([_cluster("RES-01", "order-svc", MetricName.CPU_USAGE)], _ctx(settings))
    assert provider.calls == settings.app.llm_max_attempts
    assert meta.degraded
    assert "429" in (meta.degraded_reason or "")
    assert analysis.findings


def test_provider_unavailable_skips_retries(settings: Settings):
    """凭据缺失属于确定性失败，重试只是浪费时间和配额。"""

    class Unavailable:
        name = "none"
        model = "none"

        def __init__(self) -> None:
            self.calls = 0

        def available(self):
            return False, "没有凭据"

        def complete_json(self, system, user):  # noqa: ANN001, ARG002
            self.calls += 1
            raise ProviderUnavailable("没有凭据")

    provider = Unavailable()
    analysis, meta = AIAnalyzer(settings, provider).analyze(
        [_cluster("RES-01", "order-svc", MetricName.CPU_USAGE)], _ctx(settings)
    )
    assert provider.calls == 0
    assert meta.degraded


# --------------------------------------------------------------------- 校验
def test_hallucinated_evidence_ids_are_stripped(settings: Settings):
    """模型编造的 evidence_id 必须被清掉并降低置信度。

    如果不校验，报告里就会出现指向不存在证据的结论；
    读者一核对发现对不上，整份报告的可信度就没了。
    """
    analyzer = AIAnalyzer(settings, DisabledProvider("测试"))
    cluster = _cluster("RES-01", "order-svc", MetricName.CPU_USAGE)
    real_id = cluster.evidence_refs()[0]["evidence_id"]

    payload = {
        "overall_score_adjust": 99,
        "summary": "测试",
        "findings": [
            {
                "cluster_id": "CLUS-01",
                "root_cause_category": "dependency_failure",
                "root_cause": "测试根因",
                "confidence": 0.9,
                "evidence_ids": [real_id, "FAKE:does/not/exist"],
                "suggestions": ["做点什么"],
            }
        ],
    }
    analysis, repairs = analyzer._validate(payload, [cluster], FallbackAttributor([]))  # noqa: SLF001
    assert analysis is not None
    assert analysis.findings[0].evidence_ids == [real_id]
    assert analysis.overall_score_adjust == settings.scoring.ai_adjust_limit
    assert any("不存在" in item for item in repairs)


def test_missing_clusters_are_backfilled(settings: Settings):
    """模型漏答的簇要用规则归因补齐 —— 覆盖完整性优先于来源纯净。"""
    analyzer = AIAnalyzer(settings, DisabledProvider("测试"))
    clusters = [
        _cluster("RES-01", "order-svc", MetricName.CPU_USAGE),
        _cluster("BIZ-02", "user-svc", MetricName.BUSINESS_ERROR_RATE, Severity.P2),
    ]
    clusters[1].cluster_id = "CLUS-02"
    payload = {
        "summary": "只回答了一个簇",
        "findings": [{"cluster_id": "CLUS-01", "root_cause_category": "resource_bottleneck", "root_cause": "x"}],
    }
    analysis, repairs = analyzer._validate(payload, clusters, FallbackAttributor([]))  # noqa: SLF001
    assert analysis is not None
    assert analysis.covered_cluster_ids() == {"CLUS-01", "CLUS-02"}
    assert any("补齐" in item for item in repairs)


def test_unknown_enum_is_downgraded_not_dropped(settings: Settings):
    """枚举写错不该导致整份分析被丢弃——能修的就修，并把修复记录在案。"""
    analyzer = AIAnalyzer(settings, DisabledProvider("测试"))
    cluster = _cluster("RES-01", "order-svc", MetricName.CPU_USAGE)
    payload = {
        "summary": "x",
        "findings": [
            {"cluster_id": "CLUS-01", "root_cause_category": "not_a_category", "root_cause": "x"}
        ],
    }
    analysis, repairs = analyzer._validate(payload, [cluster], FallbackAttributor([]))  # noqa: SLF001
    assert analysis is not None
    assert analysis.findings[0].root_cause_category is RootCauseCategory.UNKNOWN
    assert any("非枚举值" in item for item in repairs)


def test_invalid_top_level_structure_is_rejected(settings: Settings):
    analyzer = AIAnalyzer(settings, DisabledProvider("测试"))
    analysis, _ = analyzer._validate({"findings": "不是列表"}, [], FallbackAttributor([]))  # noqa: SLF001
    assert analysis is not None
    assert analysis.findings == []


def test_recurrence_flag_comes_from_history(settings: Settings):
    from staguard.ai.history import HistoryMatch

    cluster = _cluster("RES-04", "payment-svc", MetricName.CONN_USAGE)
    _mark_recurrence(
        [cluster],
        {"CLUS-01": [HistoryMatch("payment-svc.conn_usage", "run-old", "dependency_failure", "x", 0.9, "P1")]},
    )
    assert all(anomaly.is_recurring for anomaly in cluster.all_anomalies())


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("a&b", "a&b", 1.0),
        ("a&b", "b&c", 1 / 3),
        ("a&b", "c&d", 0.0),
    ],
)
def test_jaccard_similarity(left: str, right: str, expected: float):
    from staguard.ai.history import jaccard, signature_tokens

    assert jaccard(signature_tokens(left), signature_tokens(right)) == pytest.approx(expected, abs=1e-6)
