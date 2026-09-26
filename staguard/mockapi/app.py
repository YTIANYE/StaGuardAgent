"""模拟监控数据接口。

它的存在只为一件事：把「HTTP 拉取指标」这条接入通道做成真的。
读的是和文件源完全相同的数据文件，所以两条通道的结果必然一致——
两种接入方式读的是同一份底层数据，可各跑一次做交叉验证。

接口设计刻意贴近真实监控系统：
    GET /api/v1/datasets                      可选数据集清单
    GET /api/v1/metrics/{dataset_id}          按服务/指标/时间范围查询序列
    GET /api/v1/topology                      服务依赖拓扑
    GET /health                               存活探针
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..dataset.store_file import FileMetricStore, SeriesBlob
from ..utils.timeutil import parse_iso, to_epoch

logger = logging.getLogger(__name__)

SERIES_SHAPE_HINT = {
    "dataset_id": "数据集标识",
    "step_seconds": "采样步长（秒）",
    "series": "序列数组，values 与时间桶对齐，null 表示该点缺失",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = FileMetricStore(settings.app.data_dir / "metrics")
    topology = settings.topology

    app = FastAPI(
        title="StaGuard Mock Monitoring API",
        version="0.1.0",
        description="模拟业务监控数据源，供 StaGuardAgent 的 HTTP 采集通道使用。",
    )
    # 一天 150 条时序的响应约 2.7MB，压缩后降到十分之一量级。
    # 真实监控系统的查询接口也都会开 gzip——30 天窗口的数据量不上压缩根本传不动。
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.get("/health", summary="存活探针")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "staguard-mock-monitor",
            "datasets": len(store.datasets()),
            "poll_interval_seconds": settings.granularity_seconds,
        }

    @app.get("/api/v1/datasets", summary="数据集清单")
    def datasets() -> dict[str, Any]:
        items = store.manifest()
        return {"total": len(items), "items": items, "schema": SERIES_SHAPE_HINT}

    @app.get("/api/v1/topology", summary="服务依赖拓扑")
    def topology_view() -> dict[str, Any]:
        return {
            "services": {
                name: {
                    "instances": node.instances,
                    "depends_on": node.depends_on,
                    "criticality": node.criticality,
                    "external": node.external,
                }
                for name, node in topology.services.items()
            },
            "edges": topology.as_edges(),
        }

    @app.get("/api/v1/metrics/{dataset_id}", summary="按时间范围查询指标序列")
    def metrics(
        dataset_id: str,
        start: str | None = Query(default=None, alias="from", description="起始时间（ISO）"),
        end: str | None = Query(default=None, alias="to", description="结束时间（ISO）"),
        services: str | None = Query(default=None, description="服务名，逗号分隔"),
        metrics_filter: str | None = Query(default=None, alias="metrics", description="指标名，逗号分隔"),
    ) -> JSONResponse:
        if not store.exists(dataset_id):
            raise HTTPException(
                status_code=404,
                detail=f"数据集不存在：{dataset_id}；可用：{store.datasets()}",
            )
        dataset = store.load(dataset_id)

        service_set = {s for s in services.split(",") if s} if services else None
        metric_set = {m for m in metrics_filter.split(",") if m} if metrics_filter else None
        start_ts = _parse(start)
        end_ts = _parse(end)

        selected = []
        for blob in dataset.series:
            if service_set and blob.service not in service_set:
                continue
            if metric_set and blob.metric not in metric_set:
                continue
            selected.append(_slice_blob(blob, start_ts, end_ts, dataset.step_seconds))

        payload = {
            "dataset_id": dataset.dataset_id,
            "scenario_id": dataset.scenario_id,
            "scenario_name": dataset.scenario_name,
            "step_seconds": dataset.step_seconds,
            "start_epoch": dataset.start_epoch,
            "end_epoch": dataset.end_epoch,
            "generated_at": dataset.generated_at,
            "expected_root_cause": dataset.expected_root_cause,
            # 必须 model_dump：JSONResponse 不做 pydantic 序列化，
            # 直接塞模型对象会在渲染响应体时抛 TypeError，表现为 500。
            "series": [blob.model_dump() for blob in selected],
        }
        return JSONResponse(payload)

    return app


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parse_iso(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"时间参数非法：{raw}") from exc


def _slice_blob(blob: SeriesBlob, start: datetime | None, end: datetime | None, step_seconds: int) -> SeriesBlob:
    """按时间范围裁剪序列。

    保留「值数组与时间桶严格对齐」的结构：裁剪只改起点与数组内容，不改索引语义。
    这样下游不必为「被裁剪过的数据」写特殊分支——接口返回的永远是同一种形状。
    """
    if start is None and end is None:
        return blob

    new_start = blob.start_epoch
    values = blob.values
    if start is not None:
        offset = max(0, (to_epoch(start) - blob.start_epoch) // step_seconds)
        values = values[offset:]
        new_start = blob.start_epoch + offset * step_seconds
    if end is not None:
        limit = max(0, (to_epoch(end) - new_start) // step_seconds)
        values = values[:limit]
    return blob.model_copy(update={"start_epoch": new_start, "values": values})


app = create_app()
