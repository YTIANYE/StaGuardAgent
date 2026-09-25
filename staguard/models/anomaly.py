"""异常域模型：规则发现 -> 去重后的异常 -> 根因簇。

三层结构的用意：
1. `AnomalyFinding` 是规则的原始输出，一条规则可能在一个窗口里命中多个时刻；
2. `Anomaly` 是按 dedup_key 融合后的对外实体（一个实例一个指标一条），避免报告刷屏；
3. `AnomalyCluster` 是按拓扑传播关系聚合出的「根因簇」，AI 只对簇做归因，
   这样它才能说出「谁是根因、谁是影响面」，而不是给一堆孤立结论。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .common import BaselineMode, Confidence, Dimension, Severity, TimeWindow
from .metric import MetricName


class AnomalyFinding(BaseModel):
    """单条规则的原始发现。"""

    rule_id: str
    rule_name: str
    dimension: Dimension
    service: str
    instance: str
    metric: MetricName

    level: Severity
    observed: float
    """窗口内的「最坏」观测值（延时/负载取最大，成功率取最小）。"""
    baseline_value: float | None = None
    deviation_pct: float = 0.0
    z_score: float = 0.0
    deviation_score: float = 0.0
    """归一化严重度 0~1，由「偏离幅度 / 持续时长 / 影响面 / 服务关键度」加权，是所有分级的统一输入。"""

    amplitude: float = 0.0
    """偏离幅度 0~1，是 deviation_score 的主导项，单独保留便于解释分数来源。"""
    affected_ratio: float = 0.0
    """同服务内受同一规则影响的实例占比。规则先按单实例给出下界，
    引擎在所有规则跑完后用真实的影响面回填并重算分数——影响面天然是聚合量。"""

    duration_minutes: float = 0.0
    window: TimeWindow
    level_reason: str = ""
    baseline_mode: BaselineMode = BaselineMode.MISSING
    baseline_confidence: Confidence = Confidence.LOW
    evidence: dict[str, Any] = Field(default_factory=dict)
    """额外事实（阈值、起始时刻、命中点数…），会作为 evidence_id 的载体交给 AI。"""

    @property
    def dedup_key(self) -> str:
        return f"{self.rule_id}|{self.service}|{self.instance}|{self.metric.value}"

    def evidence_id(self) -> str:
        return f"{self.rule_id}:{self.service}/{self.instance}/{self.metric.value}"

    def evidence_ref(self) -> dict[str, Any]:
        """进入 AI 证据包的最小事实集（不含任何结论）。"""
        payload = {
            "evidence_id": self.evidence_id(),
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "dimension": self.dimension.value,
            "service": self.service,
            "instance": self.instance,
            "metric": self.metric.value,
            "metric_label": self.metric.label,
            "unit": self.metric.unit,
            "level": self.level.code,
            "observed": round(self.observed, 3),
            "baseline": None if self.baseline_value is None else round(self.baseline_value, 3),
            "deviation_pct": round(self.deviation_pct, 2),
            "z_score": round(self.z_score, 2),
            "duration_minutes": round(self.duration_minutes, 1),
            "baseline_mode": self.baseline_mode.value,
            "baseline_confidence": self.baseline_confidence.value,
        }
        payload.update({k: v for k, v in self.evidence.items() if k not in payload})
        return payload


class Anomaly(BaseModel):
    """去重/融合后的异常，报告的「异常清单」一行就是它。"""

    anomaly_id: str
    rule_id: str
    rule_name: str
    dimension: Dimension
    service: str
    instance: str
    metric: MetricName

    level: Severity
    observed: float
    baseline_value: float | None = None
    deviation_pct: float = 0.0
    z_score: float = 0.0
    deviation_score: float = 0.0
    amplitude: float = 0.0
    affected_ratio: float = 0.0
    duration_minutes: float = 0.0

    first_seen: datetime
    last_seen: datetime
    level_reason: str = ""
    baseline_mode: BaselineMode = BaselineMode.MISSING
    baseline_confidence: Confidence = Confidence.LOW
    evidence: dict[str, Any] = Field(default_factory=dict)

    merged_count: int = 1
    """被合并的原始发现条数，反映同一问题在窗口内反复命中的程度。"""
    related_rule_ids: list[str] = Field(default_factory=list)
    cluster_id: str | None = None
    is_suppressed: bool = False
    """被同一实例同一指标上更严重的异常抑制。仍会落库，但不进报告主表。"""
    suppressed_by: str | None = None
    is_recurring: bool = False
    """是否与历史 run 中的事件签名匹配（复发）。由 storage 的历史检索回填。"""

    @classmethod
    def from_finding(cls, finding: AnomalyFinding) -> Anomaly:
        return cls(
            anomaly_id=finding.dedup_key,
            rule_id=finding.rule_id,
            rule_name=finding.rule_name,
            dimension=finding.dimension,
            service=finding.service,
            instance=finding.instance,
            metric=finding.metric,
            level=finding.level,
            observed=finding.observed,
            baseline_value=finding.baseline_value,
            deviation_pct=finding.deviation_pct,
            z_score=finding.z_score,
            deviation_score=finding.deviation_score,
            amplitude=finding.amplitude,
            affected_ratio=finding.affected_ratio,
            duration_minutes=finding.duration_minutes,
            first_seen=finding.window.start,
            last_seen=finding.window.end,
            level_reason=finding.level_reason,
            baseline_mode=finding.baseline_mode,
            baseline_confidence=finding.baseline_confidence,
            evidence=dict(finding.evidence),
        )

    @property
    def target(self) -> str:
        return f"{self.service}/{self.instance}"

    @property
    def subject(self) -> str:
        return f"{self.service}/{self.instance} {self.metric.label}"

    def token(self) -> str:
        """用于根因簇签名的 token：服务 + 指标。"""
        return f"{self.service}.{self.metric.value}"

    def evidence_ref(self) -> dict[str, Any]:
        finding = AnomalyFinding(
            rule_id=self.rule_id,
            rule_name=self.rule_name,
            dimension=self.dimension,
            service=self.service,
            instance=self.instance,
            metric=self.metric,
            level=self.level,
            observed=self.observed,
            baseline_value=self.baseline_value,
            deviation_pct=self.deviation_pct,
            z_score=self.z_score,
            duration_minutes=self.duration_minutes,
            window=TimeWindow(start=self.first_seen, end=self.last_seen),
            level_reason=self.level_reason,
            baseline_mode=self.baseline_mode,
            baseline_confidence=self.baseline_confidence,
            evidence=self.evidence,
        )
        payload = finding.evidence_ref()
        payload["merged_count"] = self.merged_count
        return payload

    def recompute_score(self, criticality: float, affected_ratio: float | None = None) -> float:
        """用最终的影响面重算严重度分数。由聚合阶段调用。"""
        from ..rules.leveling import compute_deviation_score

        if affected_ratio is not None:
            self.affected_ratio = affected_ratio
        self.deviation_score = compute_deviation_score(
            amplitude=self.amplitude,
            duration_minutes=self.duration_minutes,
            affected_ratio=self.affected_ratio,
            criticality=criticality,
        )
        return self.deviation_score

    def to_row(self) -> dict[str, Any]:
        return {
            "anomaly_id": self.anomaly_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "dimension": self.dimension.value,
            "service": self.service,
            "instance": self.instance,
            "metric": self.metric.value,
            "level": self.level.code,
            "observed": self.observed,
            "baseline_value": self.baseline_value,
            "deviation_pct": self.deviation_pct,
            "z_score": self.z_score,
            "deviation_score": self.deviation_score,
            "amplitude": self.amplitude,
            "affected_ratio": self.affected_ratio,
            "duration_minutes": self.duration_minutes,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "level_reason": self.level_reason,
            "baseline_mode": self.baseline_mode.value,
            "baseline_confidence": self.baseline_confidence.value,
            "cluster_id": self.cluster_id,
            "merged_count": self.merged_count,
            "is_suppressed": self.is_suppressed,
            "suppressed_by": self.suppressed_by,
            "evidence_json": self.evidence,
        }


class AnomalyCluster(BaseModel):
    """根因簇：一个根因 + 它的影响面。"""

    cluster_id: str
    primary: Anomaly
    """最靠近故障源的那条异常（拓扑最下游 / 最早出现 / 严重度最高）。"""
    members: list[Anomaly] = Field(default_factory=list)
    propagation_path: list[str] = Field(default_factory=list)
    """传播路径，例如 gateway -> order-svc -> payment-svc -> bank-channel。"""
    hypothesis: str = ""
    """规则侧给出的传播假设（结论仍由 AI 复核，二者不一致时报告会并列展示）。"""
    services: list[str] = Field(default_factory=list)
    max_level: Severity = Severity.P4

    @property
    def size(self) -> int:
        return 1 + len(self.members)

    def all_anomalies(self) -> list[Anomaly]:
        return [self.primary, *self.members]

    def signature(self) -> str:
        """簇签名，用于跨 run 的复发识别与相似历史检索（不依赖向量库）。"""
        tokens = sorted({a.token() for a in self.all_anomalies()})
        return "&".join(tokens)

    def evidence_refs(self) -> list[dict[str, Any]]:
        refs = [self.primary.evidence_ref()]
        covered = {self.primary.anomaly_id}
        for member in self.members:
            if member.anomaly_id in covered:
                continue
            covered.add(member.anomaly_id)
            refs.append(member.evidence_ref())
        return refs

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "signature": self.signature(),
            "max_level": self.max_level.code,
            "propagation_path": self.propagation_path,
            "rule_hypothesis": self.hypothesis,
            "primary": {
                "service": self.primary.service,
                "instance": self.primary.instance,
                "metric": self.primary.metric.value,
                "level": self.primary.level.code,
                "summary": self.primary.level_reason,
            },
            "evidence": self.evidence_refs(),
        }
