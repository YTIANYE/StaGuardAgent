"""文件形态的指标数据集。

场景数据同时以三种形态存在，且**内容完全一致**——这是刻意的设计：
    1. 文件（`data/metrics/<dataset>.json`）  —— 对应「读取本地结构化巡检指标文件」
    2. Mock HTTP 接口（`staguard/mockapi`）   —— 对应「模拟 HTTP 监控接口拉取实时指标」
    3. 落库后的 metric_points 表              —— 采集归一化之后的结构化存储

两条接入通道共用这一份文件，因此「文件通道」与「HTTP 通道」的巡检结果必然一致。
两种接入方式读的是同一份数据，可通过各跑一遍做交叉验证。

存储格式用「一条时序一行、值与时间桶对齐、缺失记为 null」的紧凑结构：
如果按点展开成 JSON 对象（每点一个 dict），一份 25 小时的数据会膨胀到 20 倍以上。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..models import MetricName, MetricPoint
from ..utils.timeutil import from_epoch, to_epoch

logger = logging.getLogger(__name__)


class SeriesBlob(BaseModel):
    """一条时序的紧凑表示。values 与时间桶一一对齐，null 表示该点缺失。"""

    service: str
    instance: str
    metric: str
    unit: str = ""
    start_epoch: int
    step_seconds: int
    values: list[float | None] = Field(default_factory=list)

    def to_points(self, start: datetime | None = None, end: datetime | None = None) -> list[MetricPoint]:
        metric = MetricName(self.metric)
        start_ep = to_epoch(start) if start else None
        end_ep = to_epoch(end) if end else None
        points: list[MetricPoint] = []
        for index, value in enumerate(self.values):
            if value is None:
                continue
            epoch = self.start_epoch + index * self.step_seconds
            if start_ep is not None and epoch < start_ep:
                continue
            if end_ep is not None and epoch >= end_ep:
                continue
            points.append(
                MetricPoint(
                    ts=from_epoch(epoch),
                    service=self.service,
                    instance=self.instance,
                    metric=metric,
                    value=float(value),
                )
            )
        return points


class DatasetFile(BaseModel):
    dataset_id: str
    scenario_id: str | None = None
    scenario_name: str | None = None
    step_seconds: int
    start_epoch: int
    end_epoch: int
    generated_at: str
    expected_root_cause: str | None = None
    series: list[SeriesBlob] = Field(default_factory=list)

    def series_count(self) -> int:
        return len(self.series)

    def point_count(self) -> int:
        return sum(len(s.values) for s in self.series)

    def summary(self) -> str:
        return (
            f"{self.dataset_id}: {self.series_count()} 条时序 / {self.point_count()} 个点 / "
            f"步长 {self.step_seconds}s / {from_epoch(self.start_epoch):%m-%d %H:%M}~{from_epoch(self.end_epoch):%H:%M}"
        )


class FileMetricStore:
    """基于文件的数据集仓库。文件源与 Mock HTTP 服务共用它，保证两条通道数据一致。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_of(self, dataset_id: str) -> Path:
        return self.root / f"{dataset_id}.json"

    def datasets(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))

    def exists(self, dataset_id: str) -> bool:
        return self.path_of(dataset_id).exists()

    def load(self, dataset_id: str) -> DatasetFile:
        path = self.path_of(dataset_id)
        if not path.exists():
            raise FileNotFoundError(
                f"数据集文件不存在: {path}。先执行 `staguard gen-data` 生成模拟数据。"
            )
        with path.open("r", encoding="utf-8") as fh:
            return DatasetFile.model_validate(json.load(fh))

    def save(self, dataset: DatasetFile) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_of(dataset.dataset_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(dataset.model_dump(), fh, ensure_ascii=False, separators=(",", ":"))
        return path

    def query(
        self,
        dataset_id: str,
        service: str | None = None,
        instances: list[str] | None = None,
        metrics: list[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MetricPoint]:
        dataset = self.load(dataset_id)
        metric_filter = set(metrics) if metrics else None
        instance_filter = set(instances) if instances else None
        points: list[MetricPoint] = []
        for blob in dataset.series:
            if service and blob.service != service:
                continue
            if instance_filter and blob.instance not in instance_filter:
                continue
            if metric_filter and blob.metric not in metric_filter:
                continue
            points.extend(blob.to_points(start, end))
        return points

    def manifest(self) -> list[dict[str, Any]]:
        """数据集清单。HTTP 接口的 `/datasets` 直接返回它。"""
        items: list[dict[str, Any]] = []
        for dataset_id in self.datasets():
            try:
                dataset = self.load(dataset_id)
            except Exception:  # noqa: BLE001 - 清单接口不应因为单个坏文件整体失败
                logger.warning("数据集文件损坏，已跳过: %s", dataset_id)
                continue
            items.append(
                {
                    "dataset_id": dataset.dataset_id,
                    "scenario_id": dataset.scenario_id,
                    "scenario_name": dataset.scenario_name,
                    "step_seconds": dataset.step_seconds,
                    "start": from_epoch(dataset.start_epoch).isoformat(timespec="seconds"),
                    "end": from_epoch(dataset.end_epoch).isoformat(timespec="seconds"),
                    "series": dataset.series_count(),
                    "points": dataset.point_count(),
                    "expected_root_cause": dataset.expected_root_cause,
                }
            )
        return items
