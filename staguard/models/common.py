"""跨模块共用的基础类型：时间窗、严重级别、巡检维度、置信度。"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

_SEVERITY_LABEL: dict[int, str] = {1: "紧急", 2: "严重", 3: "一般", 4: "轻微"}
_SEVERITY_ALIAS: dict[str, int] = {
    "P1": 1, "P2": 2, "P3": 3, "P4": 4,
    "紧急": 1, "严重": 2, "一般": 3, "轻微": 4,
}
_DIMENSION_LABEL: dict[str, str] = {
    "business": "业务层",
    "performance": "性能层",
    "resource": "资源层",
    "link": "链路层",
}


class Severity(IntEnum):
    """异常级别。数值越小越严重，便于排序与封顶判断。"""

    P1 = 1
    P2 = 2
    P3 = 3
    P4 = 4

    @property
    def label(self) -> str:
        return _SEVERITY_LABEL[int(self)]

    @property
    def code(self) -> str:
        return f"P{int(self)}"

    def display(self) -> str:
        return f"{self.code} {self.label}"

    @classmethod
    def parse(cls, raw: str | int | Severity) -> Severity:
        if isinstance(raw, Severity):
            return raw
        if isinstance(raw, int):
            return cls(raw)
        key = str(raw).strip().upper()
        if key in _SEVERITY_ALIAS:
            return cls(_SEVERITY_ALIAS[key])
        raise ValueError(f"无法识别的异常级别: {raw!r}")


class Dimension(StrEnum):
    """巡检维度，对齐题目要求的三层覆盖 + 业务链路。"""

    BUSINESS = "business"
    PERFORMANCE = "performance"
    RESOURCE = "resource"
    LINK = "link"

    @property
    def label(self) -> str:
        return _DIMENSION_LABEL[self.value]


class Confidence(StrEnum):
    """置信度。数据质量不足或基线样本不足时降级，用于抑制误报。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def label(self) -> str:
        return {"high": "高", "medium": "中", "low": "低"}[self.value]

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


class BaselineMode(StrEnum):
    """基线来源。冷启动或样本不足时回退静态阈值，报告需显式标注。"""

    DYNAMIC = "dynamic"
    STATIC_FALLBACK = "static_fallback"
    MISSING = "missing"

    @property
    def label(self) -> str:
        return {"dynamic": "动态基线", "static_fallback": "静态阈值回退", "missing": "基线缺失"}[self.value]


class TimeWindow(BaseModel):
    """左闭右开时间窗。巡检的所有统计口径都以它为准。"""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _validate(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError(f"时间窗非法: end({self.end}) 必须晚于 start({self.start})")
        return self

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def duration_minutes(self) -> float:
        return self.duration.total_seconds() / 60

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end

    def last(self, minutes: float) -> TimeWindow:
        return TimeWindow(start=self.end - timedelta(minutes=minutes), end=self.end)

    def shifted(self, minutes: float) -> TimeWindow:
        delta = timedelta(minutes=minutes)
        return TimeWindow(start=self.start + delta, end=self.end + delta)

    def label(self, with_date: bool = False) -> str:
        fmt = "%Y-%m-%d %H:%M" if with_date else "%H:%M"
        return f"{self.start.strftime(fmt)}~{self.end.strftime('%H:%M')}"

    @classmethod
    def around(cls, center: datetime, minutes_before: float, minutes_after: float) -> TimeWindow:
        return cls(
            start=center - timedelta(minutes=minutes_before),
            end=center + timedelta(minutes=minutes_after),
        )


def floor_to_bucket(ts: datetime, granularity_seconds: int) -> datetime:
    """把时间戳对齐到粒度的整数倍，保证多数据源时间轴一致。"""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % granularity_seconds, tz=ts.tzinfo)
