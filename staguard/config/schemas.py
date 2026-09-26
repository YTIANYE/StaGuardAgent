"""YAML 配置的结构定义与校验。

配置即策略：规则启停、阈值、分级条件、评分权重、故障场景全部放在 YAML 里，
改策略不需要改代码——这是「可配置」这一要求的落地方式。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..models import DEFAULT_CLUSTER, DEFAULT_CLUSTER_LABEL, Dimension, Severity, Topology
from ..models.topology import ServiceNode


# --------------------------------------------------------------------------- 服务与 SLO
class SLOSpec(BaseModel):
    """服务级 SLO。评分里的「可用性」子项直接由它换算错误预算。"""

    success_rate: float = 99.9
    latency_p99_ms: float = 800.0
    latency_p95_ms: float = 300.0
    business_error_rate: float = 1.0

    def merged_with(self, override: SLOSpec | None) -> SLOSpec:
        if override is None:
            return self
        return SLOSpec(
            success_rate=override.success_rate or self.success_rate,
            latency_p99_ms=override.latency_p99_ms or self.latency_p99_ms,
            latency_p95_ms=override.latency_p95_ms or self.latency_p95_ms,
            business_error_rate=override.business_error_rate or self.business_error_rate,
        )


class ServiceSpec(BaseModel):
    tier: str | None = None
    criticality: float = Field(default=0.5, ge=0.0, le=1.0)
    owner: str | None = None
    description: str | None = None
    instances: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    external: bool = False
    cluster: str | None = None
    """所属集群键，用于「集群维度」统计。必须出现在顶层的 `clusters` 里。"""
    slo: SLOSpec | None = None


class ServicesConfig(BaseModel):
    default_slo: SLOSpec = Field(default_factory=SLOSpec)
    clusters: dict[str, str] = Field(default_factory=dict)
    """集群键 -> 展示名（例如 `trade: 交易集群`）。空则全部服务归入「未分组」。"""
    services: dict[str, ServiceSpec] = Field(default_factory=dict)

    def cluster_of(self, service: str) -> str:
        spec = self.services.get(service)
        return spec.cluster if spec and spec.cluster else DEFAULT_CLUSTER

    def undefined_clusters(self) -> list[tuple[str, str]]:
        """返回 (服务名, 未定义的集群键)，供启动期强校验使用。

        拼错一个字母就会把一个服务静默分到错误的集群里，而报告上看起来毫无异常——
        这类错误必须在启动时暴露，不能等读者去比对配置。
        """
        return [
            (name, spec.cluster)
            for name, spec in self.services.items()
            if spec.cluster and spec.cluster not in self.clusters
        ]

    def to_topology(self) -> Topology:
        nodes: dict[str, ServiceNode] = {}
        for name, spec in self.services.items():
            nodes[name] = ServiceNode(
                name=name,
                instances=list(spec.instances),
                tier=spec.tier,
                criticality=spec.criticality,
                external=spec.external,
                depends_on=list(spec.depends_on),
                owner=spec.owner,
                description=spec.description,
                cluster=self.cluster_of(name),
            )
        labels = dict(self.clusters)
        labels.setdefault(DEFAULT_CLUSTER, DEFAULT_CLUSTER_LABEL)
        return Topology(services=nodes, cluster_labels=labels)

    def slo_for(self, service: str) -> SLOSpec:
        spec = self.services.get(service)
        return self.default_slo.merged_with(spec.slo if spec else None)

    def owner_of(self, service: str) -> str:
        spec = self.services.get(service)
        return (spec.owner if spec and spec.owner else "未指定责任方")


# --------------------------------------------------------------------------- 阈值
class ThresholdSpec(BaseModel):
    """静态阈值。既用于「绝对阈值」判定，也用于动态基线冷启动时的回退。"""

    min: float | None = None
    max: float | None = None

    def exceeded(self, value: float) -> bool:
        if self.min is not None and value < self.min:
            return True
        return self.max is not None and value > self.max

    def label(self) -> str:
        parts = []
        if self.min is not None:
            parts.append(f"≥{self.min:g}")
        if self.max is not None:
            parts.append(f"≤{self.max:g}")
        return " ".join(parts) if parts else "未配置"


class ThresholdConfig(BaseModel):
    defaults: dict[str, ThresholdSpec] = Field(default_factory=dict)
    overrides: dict[str, dict[str, ThresholdSpec]] = Field(default_factory=dict)

    def for_metric(self, metric: str, service: str | None = None) -> ThresholdSpec:
        if service:
            scoped = self.overrides.get(service, {})
            if metric in scoped:
                return scoped[metric]
        return self.defaults.get(metric, ThresholdSpec())


# --------------------------------------------------------------------------- 规则
class LevelSpec(BaseModel):
    """一个级别的触发条件。

    `when` 里的键由各规则自行解释（常见键：deviation_pct / z_score / duration_minutes /
    multiplier / ratio / drop_pct），这样新增规则不需要改配置 schema。
    """

    level: Severity
    when: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    """级别理由模板，支持 {observed} {baseline} {deviation} 等占位符。"""

    @field_validator("level", mode="before")
    @classmethod
    def _coerce_level(cls, value: Any) -> Any:
        """允许 YAML 里写 `P1` / `紧急` / 1 三种形式，配置写起来更顺手。"""
        return value if isinstance(value, Severity) else Severity.parse(value)


class RuleConfig(BaseModel):
    id: str
    name: str
    dimension: Dimension
    enabled: bool = True
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    levels: list[LevelSpec] = Field(default_factory=list)


class RulesConfig(BaseModel):
    rules: list[RuleConfig] = Field(default_factory=list)

    def get(self, rule_id: str) -> RuleConfig | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def enabled(self) -> list[RuleConfig]:
        return [r for r in self.rules if r.enabled]

    def by_dimension(self) -> dict[Dimension, list[RuleConfig]]:
        grouped: dict[Dimension, list[RuleConfig]] = {}
        for rule in self.enabled():
            grouped.setdefault(rule.dimension, []).append(rule)
        return grouped


# --------------------------------------------------------------------------- 评分
class SubScoreSpec(BaseModel):
    key: str
    name: str
    weight: float
    description: str = ""


class ScoreCapSpec(BaseModel):
    condition: str
    """支持的封顶条件：has_p1 / p1_on_critical / p1_count_ge:2 / change_induced_p1。"""
    max: float
    reason: str


class ScoringConfig(BaseModel):
    dimensions: list[SubScoreSpec] = Field(default_factory=list)
    caps: list[ScoreCapSpec] = Field(default_factory=list)
    ai_adjust_limit: int = 5

    @property
    def total_weight(self) -> float:
        return sum(d.weight for d in self.dimensions) or 1.0


# --------------------------------------------------------------------------- 场景
class InjectionSpec(BaseModel):
    """故障注入脚本。数据生成器按它把「正常世界」改造成「故障世界」。"""

    kind: str
    """支持的注入类型见 data/generator.py 的 INJECTORS 注册表。"""
    target: str
    """目标，形如 `service` 或 `service/instance`。"""
    metrics: list[str] = Field(default_factory=list)
    start_offset_minutes: float = -20.0
    """相对巡检窗口结束时刻的偏移，负数表示在窗口内。"""
    duration_minutes: float = 15.0
    params: dict[str, Any] = Field(default_factory=dict)
    propagate: bool = True
    """是否沿拓扑向下游扩散影响（模拟真实故障传播）。"""


class ScenarioSpec(BaseModel):
    id: str
    name: str
    description: str
    expected_root_cause: str
    """期望根因类别（RootCauseCategory.value），归因评测集据此打分。"""
    also_accept: list[str] = Field(default_factory=list)
    """同样判定为正确的根因类别。真实世界里根因常有多个说得通的解释，
    例如内存泄漏既可归为「资源瓶颈」也可归为「代码异常」，评测集不应一刀切。"""
    expected_level: str = "P2"
    seed: int = 42
    window_end: str | None = None
    """巡检窗口结束时刻（ISO）。为空时取数据集结束时刻。"""
    history_minutes: int | None = None
    """本场景需要的高精度（1 分钟）历史长度，默认继承全局配置。
    内存泄漏这类慢故障需要 24 小时才看得见趋势，所以要单独放宽。"""
    injections: list[InjectionSpec] = Field(default_factory=list)
    expect_clean: bool = False
    """True 表示期望「无异常」，用于验证不误报。"""


class ScenariosConfig(BaseModel):
    dataset_end: str | None = None
    """数据集结束时刻（ISO）。**固定它才能保证可复现**——否则每次生成的数据都不同，
    报告里的数字也就无法复现，评审无法核对。为空时取当前时间并对齐到分钟。"""
    baseline_history_days: int = 14
    """长期归档历史天数。动态基线（hour-of-week 稳健统计）从这份数据里取样本。"""
    baseline_interval_seconds: int = 300
    """归档历史粒度。基线是统计量，5 分钟粒度足够，没必要存 14 天的分钟级数据。"""
    scenario_history_minutes: int = 180
    """每个场景额外生成的高精度（分钟级）历史长度，用于趋势型规则判定。"""
    scenarios: list[ScenarioSpec] = Field(default_factory=list)

    def get(self, scenario_id: str) -> ScenarioSpec | None:
        return next((s for s in self.scenarios if s.id == scenario_id), None)

    def ids(self) -> list[str]:
        return [s.id for s in self.scenarios]
