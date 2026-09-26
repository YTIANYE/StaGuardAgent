"""报告域模型：评分明细、历史对比、最终报告实体。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .ai import AIAnalysis, AIMeta
from .anomaly import Anomaly, AnomalyCluster
from .baseline import BaselineSet
from .metric import DataQualityReport
from .run import ChangeEvent, InspectionRun
from .statistics import ClusterStat, ServiceStat


class ScoreBreakdownItem(BaseModel):
    """评分扣分明细的一项。

    报告里会把这张表原样展示，回答「这 72 分是怎么来的」。
    这是本项目对「AI 稳定性打分」的处理方式：分数可解释、可复现，AI 只能微调。
    """

    key: str
    name: str
    weight: float
    deduction: float
    score: float
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class ScoreResult(BaseModel):
    rule_score: float
    """规则算分（未含 AI 微调）。"""
    ai_adjust: int = 0
    total: float
    grade: str = "A"
    capped_by: str | None = None
    """触发封顶的原因，例如「存在 P1 异常」。"""
    breakdown: list[ScoreBreakdownItem] = Field(default_factory=list)

    @staticmethod
    def grade_of(score: float) -> str:
        if score >= 90:
            return "A 优秀"
        if score >= 80:
            return "B 良好"
        if score >= 70:
            return "C 关注"
        if score >= 60:
            return "D 风险"
        return "E 严重"


class RunComparison(BaseModel):
    """与上一次巡检的对比。"""

    prev_run_id: str
    prev_started_at: str
    score_delta: float = 0.0
    score_trend: str = "flat"
    """up / down / flat。"""
    new_anomalies: list[str] = Field(default_factory=list)
    """异常签名列表。"""
    recovered: list[str] = Field(default_factory=list)
    persistent: list[str] = Field(default_factory=list)
    recurring: list[str] = Field(default_factory=list)
    summary: str = ""

    @property
    def score_trend_label(self) -> str:
        return {"up": "较上次上升", "down": "较上次下降", "flat": "与上次持平"}[self.score_trend]


class ScoreSnapshot(BaseModel):
    """历史评分快照，用于趋势图。"""

    run_id: str
    started_at: str
    score: float
    scenario_id: str | None = None


class InspectionReport(BaseModel):
    """一次巡检的完整产物，是报告渲染模块的唯一输入。"""

    run: InspectionRun
    score: ScoreResult
    anomalies: list[Anomaly] = Field(default_factory=list)
    clusters: list[AnomalyCluster] = Field(default_factory=list)
    ai: AIAnalysis | None = None
    ai_meta: AIMeta = Field(default_factory=AIMeta)
    data_quality: DataQualityReport | None = None
    baselines: BaselineSet | None = None
    changes: list[ChangeEvent] = Field(default_factory=list)
    comparison: RunComparison | None = None
    history: list[ScoreSnapshot] = Field(default_factory=list)
    rule_stats: dict[str, int] = Field(default_factory=dict)
    """规则命中统计 {rule_id: 命中次数}。"""
    service_stats: list[ServiceStat] = Field(default_factory=list)
    """服务维度统计：每个服务一行，含无异常的服务。"""
    cluster_stats: list[ClusterStat] = Field(default_factory=list)
    """集群维度统计：每个集群一行，由服务维度聚合而来。"""

    @property
    def top_anomalies(self) -> list[Anomaly]:
        return sorted(self.anomalies, key=lambda a: (a.level, -a.deviation_score))
