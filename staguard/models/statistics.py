"""多粒度巡检统计：服务维度与集群维度。

题目要求「支持服务维度、集群维度多粒度巡检统计」。这里只有**只读聚合**：
输入是聚合层已经产出的异常清单与根因簇，输出两张可直接进报告的统计表。

刻意不做「服务评分」「集群评分」——题目要的是统计。再引入一套评分口径，
读者就要同时记住「稳定性评分」和「服务分」两套数字，且两套数字必然互相矛盾
（同一批异常，两个不同的加权口径）。统计只回答事实：哪些服务/集群受影响、
影响多深、有没有根因落在里面。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .common import Severity
from .topology import DEFAULT_CLUSTER, DEFAULT_CLUSTER_LABEL


class ServiceStat(BaseModel):
    """一个服务在本次巡检中的统计口径。无异常的服务同样出现在统计里。"""

    service: str
    cluster: str = DEFAULT_CLUSTER
    cluster_label: str = DEFAULT_CLUSTER_LABEL
    tier: str | None = None
    owner: str | None = None
    criticality: float = 0.5

    anomaly_count: int = 0
    """未抑制异常条数。被抑制项与代表它的那条同服务同指标，计入会重复计数。"""
    max_level: Severity | None = None
    """最严重级别；无异常时为 None。"""
    affected_instances: list[str] = Field(default_factory=list)
    """出现异常（未抑制）的实例，去重后按字典序。"""
    total_instances: int = 0
    rule_ids: list[str] = Field(default_factory=list)
    """命中的规则 ID，去重后有序。"""
    worst_metric: str | None = None
    worst_summary: str | None = None
    """最严重那条异常的一句话事实（实测 / 基线 / 级别）。"""

    root_cause_of: list[str] = Field(default_factory=list)
    """该服务作为主根因的根因簇 ID。"""
    in_clusters: list[str] = Field(default_factory=list)
    """该服务作为成员出现的根因簇 ID（含它自己是主根因的那些）。"""

    @property
    def affected_ratio(self) -> float | None:
        """影响面：受影响实例数 / 实例总数。实例数为 0 时返回 None。"""
        if not self.total_instances:
            return None
        return len(self.affected_instances) / self.total_instances

    @property
    def is_healthy(self) -> bool:
        return self.anomaly_count == 0


class ClusterStat(BaseModel):
    """一个集群的汇总统计，由它下面所有服务的统计聚合而来。"""

    cluster: str
    label: str = DEFAULT_CLUSTER_LABEL
    service_count: int = 0
    affected_service_count: int = 0
    anomaly_count: int = 0
    max_level: Severity | None = None
    affected_instances: int = 0
    total_instances: int = 0
    root_cause_count: int = 0
    """该集群内服务作为主根因的根因簇数量。"""

    @property
    def affected_ratio(self) -> float | None:
        if not self.total_instances:
            return None
        return self.affected_instances / self.total_instances

    @property
    def affected_service_ratio(self) -> float | None:
        if not self.service_count:
            return None
        return self.affected_service_count / self.service_count

    @property
    def is_healthy(self) -> bool:
        return self.anomaly_count == 0
