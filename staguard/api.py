"""HTTP 服务层。

存在的理由不是「多一个入口」，而是**常驻部署下必须能被编排系统管起来**：

    /healthz          存活探针：进程还活着吗（liveness）
    /readyz           就绪探针：现在能接活吗（readiness）——真实检查数据库连通性与场景数据是否就绪
    /metrics          供 Prometheus 抓取，让巡检系统自己的健康度也可观测
    POST /inspections 手动触发，弥补「定时巡检不够灵活」的缺口
    GET  /inspections 查询历史，供看板或告警联动

`/readyz` 不只是一个 `return 200`：它真的去 `SELECT 1` 并检查数据集是否已初始化。
一个「进程活着但数据库连不上」的实例，在 K8s 里如果不摘流量，
会持续把 500 返回给调用方——探针的价值全在这个细节里。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import Settings, get_settings
from .orchestrator import InspectionOrchestrator
from .store import Database, Repository
from .utils.timeutil import parse_iso

logger = logging.getLogger(__name__)

STARTED_AT = time.time()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = Database(settings.app.db_url)
    database.init_schema()
    repo = Repository(database)
    counters: Counter[str] = Counter()
    # 正在执行的巡检请求数。HPA 用它而不是 CPU 扩容：巡检是 IO 密集（读时序库、等大模型），
    # CPU 打满之前请求早就开始排队了，按 CPU 扩容永远慢半拍。
    inflight_lock = threading.Lock()
    inflight = {"count": 0}

    app = FastAPI(
        title="StaGuardAgent API",
        version="0.1.0",
        description="AI 驱动的业务稳定性自动化巡检 Agent —— 触发巡检、查询结果、导出报告。",
    )

    # ------------------------------------------------------------------ 探针
    @app.get("/healthz", summary="存活探针", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "uptime_seconds": int(time.time() - STARTED_AT)}

    @app.get("/readyz", summary="就绪探针（真实检查依赖）")
    def readyz() -> JSONResponse:
        checks: dict[str, Any] = {}
        ready = True

        checks["database"] = database.ping()
        ready &= checks["database"]

        datasets = [d[0] for d in repo.datasets()] if checks["database"] else []
        checks["dataset_initialized"] = bool(datasets)
        checks["datasets"] = datasets
        # 数据集没生成时进程是「活着但干不了活」的：不该接流量，但不该被重启
        ready &= checks["dataset_initialized"]

        checks["llm_configured"] = settings.app.has_llm_credentials
        checks["llm_degradable"] = True  # AI 不可用会降级为规则归因，不影响就绪

        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )

    @app.get("/metrics", summary="Prometheus 指标", response_class=PlainTextResponse)
    def metrics() -> str:
        stats = repo.stats()
        lines = [
            "# HELP staguard_inspections_total 累计巡检次数",
            "# TYPE staguard_inspections_total counter",
            f"staguard_inspections_total {stats['runs']}",
            "# HELP staguard_anomalies_total 累计识别异常数",
            "# TYPE staguard_anomalies_total counter",
            f"staguard_anomalies_total {stats['anomalies']}",
            "# HELP staguard_metric_points 指标点总数",
            "# TYPE staguard_metric_points gauge",
            f"staguard_metric_points {stats['metric_points']}",
            "# HELP staguard_uptime_seconds 进程存活时长",
            "# TYPE staguard_uptime_seconds gauge",
            f"staguard_uptime_seconds {int(time.time() - STARTED_AT)}",
            "# HELP staguard_pending_inspections 正在执行的巡检请求数（HPA 扩容依据）",
            "# TYPE staguard_pending_inspections gauge",
            f"staguard_pending_inspections {inflight['count']}",
        ]
        for reason, count in counters.items():
            lines.append(f'staguard_inspection_triggers_total{{result="{reason}"}} {count}')
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ 巡检
    @app.post("/api/v1/inspections", summary="触发一次巡检")
    def create_inspection(
        scenario_id: str | None = Query(
            default=None,
            description="场景编号，如 S1（回放该历史故障切片）；为空则巡检当前水位",
        ),
        source: str | None = Query(default=None, description="数据源：file / http"),
        window_end: str | None = Query(default=None, description="巡检窗口结束时刻（ISO）"),
    ) -> dict[str, Any]:
        anchor = None
        if window_end:
            try:
                anchor = parse_iso(window_end)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"window_end 非法：{window_end}") from exc
        if source and source not in ("file", "http"):
            raise HTTPException(status_code=400, detail="source 只支持 file 或 http")

        with inflight_lock:
            inflight["count"] += 1
        try:
            try:
                report = InspectionOrchestrator(settings, repo).run(
                    scenario_id=scenario_id, window_end=anchor, source_name=source
                )
            except Exception as exc:  # noqa: BLE001 - 编排器内部已做阶段隔离，这里只兜底
                counters["error"] += 1
                logger.exception("HTTP 触发的巡检失败")
                raise HTTPException(status_code=500, detail=f"巡检执行失败：{exc}") from exc

            counters["success" if report.run.status.value != "failed" else "failed"] += 1
            return {
                "run_id": report.run.run_id,
                "status": report.run.status.value,
                "score": report.score.total,
                "grade": report.score.grade,
                "anomalies": report.run.anomaly_count,
                "level_counts": report.run.level_counts,
                "ai_degraded": report.ai_meta.degraded,
                "duration_ms": report.run.duration_ms,
                "report_url": f"/api/v1/inspections/{report.run.run_id}/report?format=md",
            }
        finally:
            with inflight_lock:
                inflight["count"] -= 1

    @app.get("/api/v1/inspections", summary="历史巡检列表")
    def list_inspections(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
        rows = repo.list_runs(limit)
        return {"total": len(rows), "items": rows}

    @app.get("/api/v1/inspections/{run_id}", summary="巡检详情")
    def get_inspection(run_id: str) -> dict[str, Any]:
        run = repo.load_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"未找到巡检记录 {run_id}")
        return {
            "run": run.model_dump(mode="json"),
            "anomalies": repo.load_anomalies(run_id),
            "clusters": repo.load_clusters(run_id),
            "ai": repo.load_ai(run_id),
        }

    @app.get("/api/v1/inspections/{run_id}/report", summary="导出巡检报告")
    def get_report(
        run_id: str,
        format: str = Query(default="md", pattern="^(md|json)$"),  # noqa: A002 - 与 URL 语义一致
    ) -> Any:
        stored = repo.load_report(run_id)
        if not stored:
            raise HTTPException(status_code=404, detail=f"未找到 {run_id} 的报告")
        if format == "json":
            return {"run_id": run_id, "score": stored["score"], "payload": stored["payload"]}
        return PlainTextResponse(stored["markdown"], media_type="text/markdown; charset=utf-8")

    @app.get("/api/v1/scenarios", summary="可用场景与一键演示清单")
    def list_scenarios() -> dict[str, Any]:
        return {
            "dataset_end": settings.scenarios.dataset_end,
            "items": [
                {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "expected_root_cause": s.expected_root_cause,
                    "expected_level": s.expected_level,
                    "expect_clean": s.expect_clean,
                }
                for s in settings.scenarios.scenarios
            ],
        }

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"service": "StaGuardAgent", "docs": "/docs", "health": "/healthz", "ready": "/readyz"}

    return app


app = create_app()
