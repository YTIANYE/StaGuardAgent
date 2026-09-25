"""数据接入包：文件源 + Mock HTTP 源，共用同一套归一化输出。"""

from __future__ import annotations

from ..config import Settings
from ..dataset.store_file import FileMetricStore
from .base import CollectionResult, CollectRequest, CollectTimer, MetricSource
from .file_source import FileMetricSource
from .http_source import HttpMetricSource

SOURCE_NAMES = ("file", "http")


def build_source(settings: Settings, name: str | None = None) -> MetricSource:
    """按配置构造数据源。未知名称回退到文件源并给出提示，而不是直接抛异常。"""
    source_name = (name or settings.app.default_source).lower()
    if source_name == "http":
        return HttpMetricSource(
            base_url=settings.app.mock_api_base_url,
            timeout_s=settings.app.mock_api_timeout_s,
            granularity_seconds=settings.granularity_seconds,
            trust_env=settings.app.http_source_trust_env,
        )
    store = FileMetricStore(settings.app.data_dir / "metrics")
    return FileMetricSource(store, granularity_seconds=settings.granularity_seconds)


__all__ = [
    "SOURCE_NAMES",
    "CollectRequest",
    "CollectionResult",
    "CollectTimer",
    "FileMetricSource",
    "HttpMetricSource",
    "MetricSource",
    "build_source",
]
