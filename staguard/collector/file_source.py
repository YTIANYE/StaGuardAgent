"""本地结构化指标文件数据源。

对应题目「读取本地结构化巡检指标文件（QPS、成功率、P99 延时、错误数、CPU/内存负载）」。
"""

from __future__ import annotations

import logging

from ..dataset.store_file import FileMetricStore
from ..models import MetricName
from ..utils.timeutil import from_epoch, to_epoch
from .base import CollectionResult, CollectRequest, CollectTimer, MetricSource

logger = logging.getLogger(__name__)


class FileMetricSource(MetricSource):
    name = "file"

    def __init__(self, store: FileMetricStore, granularity_seconds: int = 60) -> None:
        self.store = store
        self.granularity_seconds = granularity_seconds

    def fetch(self, request: CollectRequest) -> CollectionResult:
        with CollectTimer() as timer:
            if not self.store.exists(request.dataset_id):
                available = "、".join(self.store.datasets()) or "（空）"
                return self._empty(
                    request,
                    f"数据集 {request.dataset_id} 不存在；可用数据集：{available}",
                )

            dataset = self.store.load(request.dataset_id)
            metric_filter = set(request.metrics) if request.metrics else None
            service_filter = set(request.services) if request.services else None
            start_epoch = to_epoch(request.fetch_start)
            end_epoch = to_epoch(request.fetch_end)

            points = []
            declared_units: dict[str, str] = {}
            series_keys: list[str] = []
            for blob in dataset.series:
                if service_filter and blob.service not in service_filter:
                    continue
                if metric_filter and blob.metric not in metric_filter:
                    continue
                key = f"{blob.service}/{blob.instance}/{blob.metric}"
                series_keys.append(key)
                declared_units[key] = blob.unit or MetricName(blob.metric).unit
                points.extend(blob.to_points(request.fetch_start, request.fetch_end))

            result = CollectionResult(
                source=self.name,
                dataset_id=request.dataset_id,
                points=points,
                window=request.window,
                fetched_from=request.fetch_start,
                fetched_to=request.fetch_end,
                declared_units=declared_units,
                series_keys=series_keys,
                elapsed_ms=timer.elapsed_ms(),
            )
            logger.info(
                "文件源读取 %s：%d 点 / %d 时序，覆盖 %s ~ %s",
                request.dataset_id, len(points), len(series_keys),
                from_epoch(start_epoch).strftime("%m-%d %H:%M"),
                from_epoch(end_epoch).strftime("%H:%M"),
            )
            return result
