"""AI 分析域模型：根因分类、AI 结论、调用元信息。

约束设计（这是本项目最重要的工程主张之一）：
- AI **不负责打分**，只能对规则算出的分数做 ±5 微调，`overall_score_adjust` 有硬边界；
- 每条根因必须引用 `evidence_ids`，没有证据的结论会被校验判为无效；
- AI 必须自陈 `uncertainties`，说不清的地方要承认，而不是编一个自信的答案。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

SCORE_ADJUST_LIMIT = 5
"""AI 允许的评分微调上限。刻意做小，保证「分数主要由可解释的规则决定」。"""


class RootCauseCategory(StrEnum):
    """根因分类体系。覆盖题目点名的四类，并补充变更与容量两类。"""

    TRAFFIC_FLUCTUATION = "traffic_fluctuation"
    RESOURCE_BOTTLENECK = "resource_bottleneck"
    CODE_DEFECT = "code_defect"
    DEPENDENCY_FAILURE = "dependency_failure"
    CHANGE_INDUCED = "change_induced"
    CAPACITY_CONFIG = "capacity_config"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            "traffic_fluctuation": "流量波动",
            "resource_bottleneck": "资源瓶颈",
            "code_defect": "代码异常",
            "dependency_failure": "依赖故障",
            "change_induced": "变更引入",
            "capacity_config": "容量/配置问题",
            "unknown": "证据不足",
        }[self.value]

    @property
    def playbook(self) -> str:
        """该类根因的兜底处置建议，AI 不可用时由它生成建议文本。"""
        return {
            "traffic_fluctuation": "容量侧：核对容量水位与限流配置；业务侧：确认是否为预期活动流量。",
            "resource_bottleneck": "定位热点实例，评估扩容或优化热点代码路径，必要时先摘除异常实例。",
            "code_defect": "拉取该实例异常日志与堆栈，结合最近一次代码变更定位缺陷。",
            "dependency_failure": "检查下游依赖健康度与连接池占用，必要时降级、熔断或切换备用链路。",
            "change_induced": "优先回滚最近一次变更，验证恢复后再做灰度复盘。",
            "capacity_config": "复核连接池/线程池/超时与重试配置，评估容量与限流的合理性。",
            "unknown": "补充日志与链路数据后重新巡检。",
        }[self.value]


class Finding(BaseModel):
    """针对一个根因簇的 AI 结论。"""

    cluster_id: str
    root_cause_category: RootCauseCategory
    root_cause: str
    """一句话根因结论。"""
    root_cause_service: str | None = None
    """AI 认定的根因所在服务。

    单独拉出这个字段，是为了让「AI 的判断与规则聚类的判断是否一致」变成
    一个**可机械校验**的事实。只比较自然语言里有没有某个词是不可靠的——
    那种检查要么永远触发、要么永远不触发，等于没有。
    """
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    impact: str = ""
    """对业务的影响面描述。"""
    suggestions: list[str] = Field(default_factory=list)
    """本次可落地的修复建议。"""
    is_recurring: bool = False
    owner_hint: str | None = None

    @property
    def category_label(self) -> str:
        return self.root_cause_category.label


class TrendJudgement(BaseModel):
    direction: str = "stable"
    """improving / stable / degrading。"""
    reasoning: str = ""
    forecast: str = ""
    """未来风险预测（例如「按当前内存增速，约 6 小时后触发 OOM」）。"""

    @property
    def label(self) -> str:
        return {"improving": "趋于好转", "stable": "整体平稳", "degrading": "持续恶化"}.get(
            self.direction, self.direction
        )


class AIAnalysis(BaseModel):
    """AI 智能分析的完整输出。字段与题目「AI 必须输出」四项一一对应。"""

    overall_score_adjust: int = 0
    """稳定性评分微调，硬边界 ±5（超出会被校验器裁剪）。"""
    summary: str = ""
    """本次巡检风险总结。"""
    findings: list[Finding] = Field(default_factory=list)
    """根因推理结论。粒度是根因簇：每个簇一条，合起来覆盖本次全部未抑制异常。

    被抑制的异常（同一服务同一指标上已有更严重的那条）不单独出结论——
    它们的成因由代表它们的那个簇给出，被抑制项本身在报告折叠区可见。
    """
    trend: TrendJudgement = Field(default_factory=TrendJudgement)
    """稳定性趋势判断。"""
    stability_advice: list[str] = Field(default_factory=list)
    """长期稳定性优化方案。"""
    uncertainties: list[str] = Field(default_factory=list)
    """AI 自陈证据不足之处。"""

    @field_validator("overall_score_adjust")
    @classmethod
    def _clamp_adjust(cls, value: int) -> int:
        return max(-SCORE_ADJUST_LIMIT, min(SCORE_ADJUST_LIMIT, int(value)))

    def finding_for(self, cluster_id: str) -> Finding | None:
        return next((f for f in self.findings if f.cluster_id == cluster_id), None)

    def covered_cluster_ids(self) -> set[str]:
        return {f.cluster_id for f in self.findings}


class AIMeta(BaseModel):
    """AI 调用元信息。降级必须留痕，报告里要能一眼看出结论是不是 AI 给的。"""

    provider: str = "none"
    model: str = "none"
    degraded: bool = False
    degraded_reason: str | None = None
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    prompt_version: str = "v1"
    raw_preview: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def status_label(self) -> str:
        if self.degraded:
            return f"降级运行（{self.degraded_reason or '未知原因'}）"
        return f"{self.provider}/{self.model}"

    def to_row(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "degraded": int(self.degraded),
            "degraded_reason": self.degraded_reason,
            "attempts": self.attempts,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "prompt_version": self.prompt_version,
        }
