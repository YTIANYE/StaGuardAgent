"""数据源接入的统一抽象。

题目要求「两种方式任选其一」，本项目两条都实现，并且**共用同一套归一化接口**：
巡检编排器只认识 `MetricSource.fetch(request)`，至于数据来自本地文件还是 HTTP 接口，
是数据源自己的事。这样将来接 Prometheus / 云监控只需要再写一个实现，编排逻辑零改动。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from ..models import MetricPoint, TimeWindow

logger = logging.getLogger(__name__)


class CollectRequest(BaseModel):
    """一次采集请求。

    注意它同时携带 `window_minutes` 与 `history_minutes`：
    巡检窗口是 30 分钟，但趋势型规则（内存泄漏、性能劣化）需要更长的观察窗，
    所以采集必须一次性把「窗口 + 观察期」都拉回来。
    """

    dataset_id: str
    window_end: datetime
    window_minutes: int = 30
    history_minutes: int = 180
    granularity_seconds: int = 60

    @property
    def window(self) -> TimeWindow:
        return TimeWindow(
            start=self.window_end - timedelta(minutes=self.window_minutes),
            end=self.window_end,
        )

    @property
    def fetch_start(self) -> datetime:
        return self.window_end - timedelta(minutes=self.history_minutes)

    @property
    def fetch_end(self) -> datetime:
        return self.window_end


class CollectionResult(BaseModel):
    source: str
    dataset_id: str
    points: list[MetricPoint] = Field(default_factory=list)
    window: TimeWindow
    fetched_from: datetime
    fetched_to: datetime
    declared_units: dict[str, str] = Field(default_factory=dict)
    """series key -> 上游声明的单位。标准化模块据此做单位换算。"""
    series_keys: list[str] = Field(default_factory=list)
    elapsed_ms: int = 0
    warnings: list[str] = Field(default_factory=list)

    @property
    def point_count(self) -> int:
        return len(self.points)

    def summary(self) -> str:
        return (
            f"{self.source} 通道采集 {self.point_count} 个点 / {len(self.series_keys)} 条时序"
            f"（{self.elapsed_ms}ms）"
        )


class MetricSource(ABC):
    """数据源接口。"""

    name: str = "unknown"

    @abstractmethod
    def fetch(self, request: CollectRequest) -> CollectionResult:
        """拉取指定时间范围的指标。实现方负责把数据归一成 `MetricPoint`。"""

    def _empty(self, request: CollectRequest, warning: str) -> CollectionResult:
        return CollectionResult(
            source=self.name,
            dataset_id=request.dataset_id,
            window=request.window,
            fetched_from=request.fetch_start,
            fetched_to=request.fetch_end,
            warnings=[warning],
        )


class CollectTimer:
    """阶段耗时统计。巡检的每个阶段都要可观测，否则线上排查时就只能靠猜。"""

    def __init__(self) -> None:
        self.started = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def __enter__(self) -> CollectTimer:
        return self

    def __exit__(self, *exc: object) -> None:
        return None
