"""巡检运行域模型：run、阶段记录、变更事件。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .common import Severity, TimeWindow


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEGRADED = "degraded"
    """执行成功但走了降级路径（例如 AI 不可用改用规则归因）。"""

    @property
    def label(self) -> str:
        return {
            "pending": "待执行",
            "running": "执行中",
            "succeeded": "成功",
            "failed": "失败",
            "degraded": "降级",
        }[self.value]


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    """部分阶段失败，但报告仍可用（编排器阶段隔离的产物）。"""
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {"running": "运行中", "succeeded": "成功", "partial": "部分降级", "failed": "失败"}[self.value]


STAGE_ORDER: tuple[str, ...] = (
    "collect",
    "normalize",
    "store",
    "baseline",
    "rule_engine",
    "aggregate",
    "score",
    "ai_analysis",
    "report",
    "notify",
)
"""巡检阶段顺序。编排器按此推进，每个阶段独立 try/except，单个阶段失败不炸全局。

注意 `score` 排在 `ai_analysis` 之前：规则先算出分数，AI 拿到这个分数作为证据的一部分，
再给出 ±5 的微调建议。顺序反过来就变成「AI 先定分、规则再解释」，
那正是本项目要避免的做法。"""


class StageRecord(BaseModel):
    name: str
    status: StageStatus = StageStatus.PENDING
    duration_ms: int = 0
    detail: str = ""
    error: str | None = None

    @property
    def label(self) -> str:
        return {
            "collect": "数据采集",
            "normalize": "标准化清洗",
            "store": "结构化存储",
            "baseline": "动态基线",
            "rule_engine": "规则巡检",
            "aggregate": "聚合去重",
            "ai_analysis": "AI 智能分析",
            "score": "稳定性评分",
            "report": "报告输出",
            "notify": "告警推送",
        }.get(self.name, self.name)


class ChangeType(StrEnum):
    RELEASE = "release"
    CONFIG = "config"
    ROLLBACK = "rollback"
    SCALE = "scale"
    DB_MIGRATION = "db_migration"

    @property
    def label(self) -> str:
        return {
            "release": "版本发布",
            "config": "配置变更",
            "rollback": "回滚",
            "scale": "扩缩容",
            "db_migration": "数据库变更",
        }[self.value]


class ChangeEvent(BaseModel):
    """变更事件。发布/配置变更是异常归因里最强的证据之一：

    异常起始时间与变更时间高度重合，几乎可以直接定性为「变更引入」。
    """

    change_id: str
    ts: datetime
    service: str
    type: ChangeType
    version: str | None = None
    operator: str | None = None
    description: str | None = None

    def minutes_before(self, ts: datetime) -> float:
        return (ts - self.ts).total_seconds() / 60.0

    def to_prompt_dict(self, related_ts: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "change_id": self.change_id,
            "ts": self.ts.isoformat(timespec="minutes"),
            "service": self.service,
            "type": self.type.label,
            "version": self.version,
            "description": self.description,
        }
        if related_ts is not None:
            payload["minutes_before_anomaly"] = round(self.minutes_before(related_ts), 1)
        return payload


class InspectionRun(BaseModel):
    """一次巡检的元信息与结果摘要。"""

    run_id: str
    scenario_id: str | None = None
    window: TimeWindow
    granularity_seconds: int = 60
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus = RunStatus.RUNNING
    source: str = "file"
    """本次使用的数据源：file / http。"""
    stages: list[StageRecord] = Field(default_factory=list)
    anomaly_count: int = 0
    level_counts: dict[str, int] = Field(default_factory=dict)
    """{"P1": 1, "P2": 2, ...}，故意用字符串键，避免 JSON 序列化时整型键被改写。"""
    score: float | None = None
    ai_degraded: bool = False
    scope_services: int = 0
    scope_instances: int = 0
    error: str | None = None

    @property
    def duration_ms(self) -> int:
        if not self.finished_at:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def count_levels(self, levels: list[Severity]) -> None:
        counts = {s.code: 0 for s in Severity}
        for level in levels:
            counts[level.code] += 1
        self.level_counts = counts

    @property
    def highest_level(self) -> Severity | None:
        for sev in Severity:
            if self.level_counts.get(sev.code, 0) > 0:
                return sev
        return None

    def stage(self, name: str) -> StageRecord | None:
        return next((s for s in self.stages if s.name == name), None)

    def upsert_stage(self, record: StageRecord) -> None:
        for idx, existing in enumerate(self.stages):
            if existing.name == record.name:
                self.stages[idx] = record
                return
        self.stages.append(record)

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "window_start": self.window.start.isoformat(),
            "window_end": self.window.end.isoformat(),
            "granularity_seconds": self.granularity_seconds,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status.value,
            "source": self.source,
            "anomaly_count": self.anomaly_count,
            "level_counts": self.level_counts,
            "score": self.score,
            "ai_degraded": self.ai_degraded,
            "duration_ms": self.duration_ms,
        }
