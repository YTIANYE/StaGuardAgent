"""模拟 HTTP 监控接口数据源。

对应题目「模拟 HTTP 监控接口拉取实时业务指标数据」。
它对外的样子就是一个普通的监控查询接口（GET + 时间范围参数），
不改任何一行编排代码就能从下面的文件源切过来——这正是「数据源可插拔」的价值。
"""

from __future__ import annotations

import logging

import httpx

from ..dataset.store_file import DatasetFile
from ..models import MetricName
from ..utils.timeutil import from_epoch, to_epoch
from .base import CollectionResult, CollectRequest, CollectTimer, MetricSource

logger = logging.getLogger(__name__)


class HttpMetricSource(MetricSource):
    name = "http"

    def __init__(
        self,
        base_url: str,
        timeout_s: float = 10.0,
        granularity_seconds: int = 60,
        trust_env: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.granularity_seconds = granularity_seconds
        # 默认不读系统代理：监控接口通常在内网直连，
        # 被系统代理转发会返回 502，且错误现象极具误导性（看起来像对端挂了）
        self.trust_env = trust_env
        self._client = httpx.Client(timeout=timeout_s, trust_env=trust_env)

    def fetch(self, request: CollectRequest) -> CollectionResult:
        url = f"{self.base_url}/api/v1/metrics/{request.dataset_id}"
        params: dict[str, str] = {
            "from": from_epoch(to_epoch(request.fetch_start)).isoformat(timespec="seconds"),
            "to": from_epoch(to_epoch(request.fetch_end)).isoformat(timespec="seconds"),
        }
        if request.services:
            params["services"] = ",".join(request.services)
        if request.metrics:
            params["metrics"] = ",".join(request.metrics)

        with CollectTimer() as timer:
            try:
                response = self._client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                # 把响应体也带上：502/500 这类错误的关键线索往往就在 body 里
                body = exc.response.text[:160].replace("\n", " ")
                return self._empty(
                    request,
                    f"监控接口返回 {exc.response.status_code}：{url}；响应体：{body}。"
                    "若为 502，请检查是否被系统代理转发（STAGUARD_HTTP_SOURCE_TRUST_ENV）",
                )
            except httpx.HTTPError as exc:
                return self._empty(request, f"监控接口不可达（{exc.__class__.__name__}: {exc}）：{url}")

            dataset = DatasetFile.model_validate(payload)
            points = []
            declared_units: dict[str, str] = {}
            series_keys: list[str] = []
            for blob in dataset.series:
                key = f"{blob.service}/{blob.instance}/{blob.metric}"
                series_keys.append(key)
                declared_units[key] = blob.unit or MetricName(blob.metric).unit
                points.extend(blob.to_points(request.fetch_start, request.fetch_end))

        logger.info(
            "HTTP 源拉取 %s：%d 点 / %d 时序（%dms）",
            request.dataset_id, len(points), len(series_keys), timer.elapsed_ms(),
        )
        return CollectionResult(
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

    def ping(self) -> bool:
        """健康检查：探测监控接口是否可达。"""
        try:
            return self._client.get(f"{self.base_url}/health").status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._client.close()
