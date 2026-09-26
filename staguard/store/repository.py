"""数据仓储层。

所有 SQL 都收敛在这里，上层模块只跟领域模型打交道——
这样换数据库、加缓存、做读写分离都不会扩散到业务代码。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, select

from ..models import (
    Anomaly,
    AnomalyCluster,
    BaselineSet,
    ChangeEvent,
    ChangeType,
    InspectionRun,
    MetricName,
    MetricPoint,
    MetricSeries,
    RunStatus,
    ScoreSnapshot,
    TimeWindow,
)
from ..utils.timeutil import from_epoch, to_epoch
from .engine import Database
from .schema import ai_results, anomalies, baselines, changes, clusters, metric_points, reports, runs

logger = logging.getLogger(__name__)

INSERT_CHUNK = 20_000
"""单批写入行数。SQLite 单条 INSERT 约 0.1ms，批量 executemany 可降到 5µs 量级，
分批是为了控制单次事务的内存峰值。"""


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ============================================================== 指标读写
    def delete_dataset(self, dataset_id: str) -> int:
        with self.db.begin() as conn:
            result = conn.execute(delete(metric_points).where(metric_points.c.dataset_id == dataset_id))
        return result.rowcount or 0

    def insert_metric_points(
        self,
        dataset_id: str,
        granularity_seconds: int,
        points: Iterable[MetricPoint],
        chunk_size: int = INSERT_CHUNK,
    ) -> int:
        """批量灌入指标点。分块提交，避免一次性构造百万级行对象。"""
        total = 0
        buffer: list[dict[str, Any]] = []
        for point in points:
            buffer.append(
                {
                    "dataset_id": dataset_id,
                    "granularity_seconds": granularity_seconds,
                    "ts_epoch": to_epoch(point.ts),
                    "ts": point.ts.isoformat(timespec="seconds"),
                    "service": point.service,
                    "instance": point.instance,
                    "metric": point.metric.value,
                    "value": float(point.value),
                }
            )
            if len(buffer) >= chunk_size:
                total += self._flush_points(buffer)
                buffer = []
        if buffer:
            total += self._flush_points(buffer)
        return total

    def _flush_points(self, rows: list[dict[str, Any]]) -> int:
        with self.db.begin() as conn:
            conn.execute(insert(metric_points), rows)
        return len(rows)

    def count_points(self, dataset_id: str) -> int:
        with self.db.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).select_from(metric_points).where(metric_points.c.dataset_id == dataset_id)
                ).scalar_one()
            )

    def dataset_fingerprint(self, dataset_id: str) -> tuple[int, int, int]:
        """数据集的 (点数, 最早时刻, 最晚时刻)。

        用于「内容未变则跳过重写」：定时巡检会对同一个时间窗反复巡检
        （窗口每 15 分钟才滑动一次），每次都删掉 20 万行再写回 20 万行
        既浪费 IO，也会让 SQLite 文件不断膨胀。指纹一致就直接复用。
        """
        with self.db.connect() as conn:
            row = conn.execute(
                select(
                    func.count(), func.min(metric_points.c.ts_epoch), func.max(metric_points.c.ts_epoch)
                ).where(metric_points.c.dataset_id == dataset_id)
            ).one()
        return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))

    def datasets(self) -> list[tuple[str, int, int]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                select(
                    metric_points.c.dataset_id,
                    func.count().label("points"),
                    func.min(metric_points.c.ts_epoch),
                )
                .group_by(metric_points.c.dataset_id)
                .order_by(metric_points.c.dataset_id)
            ).all()
        return [(r[0], int(r[1]), int(r[2])) for r in rows]

    def fetch_values(
        self,
        dataset_id: str,
        service: str,
        instance: str,
        metric: MetricName | str,
        start: datetime,
        end: datetime,
        granularity_seconds: int | None = None,
    ) -> list[tuple[datetime, float]]:
        metric_value = metric.value if isinstance(metric, MetricName) else metric
        stmt = select(metric_points.c.ts_epoch, metric_points.c.value).where(
            metric_points.c.dataset_id == dataset_id,
            metric_points.c.service == service,
            metric_points.c.instance == instance,
            metric_points.c.metric == metric_value,
            metric_points.c.ts_epoch >= to_epoch(start),
            metric_points.c.ts_epoch < to_epoch(end),
        )
        if granularity_seconds is not None:
            stmt = stmt.where(metric_points.c.granularity_seconds == granularity_seconds)
        stmt = stmt.order_by(metric_points.c.ts_epoch)
        with self.db.connect() as conn:
            rows = conn.execute(stmt).all()
        return [(from_epoch(int(r[0])), float(r[1])) for r in rows]

    def fetch_series(
        self,
        dataset_id: str,
        service: str,
        instance: str,
        metric: MetricName,
        start: datetime,
        end: datetime,
        granularity_seconds: int | None = None,
    ) -> MetricSeries:
        points = self.fetch_values(dataset_id, service, instance, metric, start, end, granularity_seconds)
        return MetricSeries(service=service, instance=instance, metric=metric, points=points, unit=metric.unit)

    def fetch_service_series(
        self,
        dataset_id: str,
        service: str,
        metric: MetricName,
        start: datetime,
        end: datetime,
        granularity_seconds: int | None = None,
        instances: Sequence[str] | None = None,
    ) -> dict[str, MetricSeries]:
        """取一个服务下所有实例的同一条指标。用于实例不均衡判定与多实例聚合。"""
        stmt = select(
            metric_points.c.instance, metric_points.c.ts_epoch, metric_points.c.value
        ).where(
            metric_points.c.dataset_id == dataset_id,
            metric_points.c.service == service,
            metric_points.c.metric == metric.value,
            metric_points.c.ts_epoch >= to_epoch(start),
            metric_points.c.ts_epoch < to_epoch(end),
        )
        if granularity_seconds is not None:
            stmt = stmt.where(metric_points.c.granularity_seconds == granularity_seconds)
        if instances:
            stmt = stmt.where(metric_points.c.instance.in_(list(instances)))
        stmt = stmt.order_by(metric_points.c.instance, metric_points.c.ts_epoch)

        grouped: dict[str, list[tuple[datetime, float]]] = {}
        with self.db.connect() as conn:
            for row in conn.execute(stmt):
                grouped.setdefault(str(row[0]), []).append((from_epoch(int(row[1])), float(row[2])))

        return {
            inst: MetricSeries(service=service, instance=inst, metric=metric, points=pts, unit=metric.unit)
            for inst, pts in grouped.items()
        }

    def count_series(
        self, dataset_id: str, start: datetime | None = None, end: datetime | None = None
    ) -> int:
        stmt = select(func.count(func.distinct(metric_points.c.service + "/" + metric_points.c.instance))).where(
            metric_points.c.dataset_id == dataset_id
        )
        if start is not None and end is not None:
            stmt = stmt.where(
                metric_points.c.ts_epoch >= to_epoch(start), metric_points.c.ts_epoch < to_epoch(end)
            )
        with self.db.connect() as conn:
            return int(conn.execute(stmt).scalar_one() or 0)

    # ============================================================== 变更事件
    def replace_changes(self, events: Sequence[ChangeEvent], scenario_ids: dict[str, str] | None = None) -> int:
        """全量替换变更事件表。变更数据量很小（几十条），全量替换比增量 upsert 更简单可靠。"""
        scenario_ids = scenario_ids or {}
        with self.db.begin() as conn:
            conn.execute(delete(changes))
            if events:
                conn.execute(
                    insert(changes),
                    [
                        {
                            "change_id": e.change_id,
                            "scenario_id": scenario_ids.get(e.change_id),
                            "ts_epoch": to_epoch(e.ts),
                            "ts": e.ts.isoformat(timespec="seconds"),
                            "service": e.service,
                            "type": e.type.value,
                            "version": e.version,
                            "operator": e.operator,
                            "description": e.description,
                        }
                        for e in events
                    ],
                )
        return len(events)

    def fetch_changes(
        self,
        start: datetime,
        end: datetime,
        scenario_id: str | None = None,
    ) -> list[ChangeEvent]:
        stmt = select(changes).where(
            changes.c.ts_epoch >= to_epoch(start), changes.c.ts_epoch < to_epoch(end)
        )
        if scenario_id:
            # 背景变更（scenario_id 为空）对所有场景可见，是刻意的干扰项
            stmt = stmt.where((changes.c.scenario_id.is_(None)) | (changes.c.scenario_id == scenario_id))
        with self.db.connect() as conn:
            rows = conn.execute(stmt.order_by(changes.c.ts_epoch)).mappings().all()
        return [
            ChangeEvent(
                change_id=r["change_id"],
                ts=from_epoch(int(r["ts_epoch"])),
                service=r["service"],
                type=ChangeType(r["type"]),
                version=r["version"],
                operator=r["operator"],
                description=r["description"],
            )
            for r in rows
        ]

    # ============================================================== 巡检结果
    def save_run(self, run: InspectionRun) -> None:
        row = run.to_row()
        row["started_at_epoch"] = to_epoch(run.started_at)
        with self.db.begin() as conn:
            conn.execute(delete(runs).where(runs.c.run_id == run.run_id))
            conn.execute(insert(runs), [row])

    def update_run_status(self, run: InspectionRun) -> None:
        self.save_run(run)

    def load_run(self, run_id: str) -> InspectionRun | None:
        with self.db.connect() as conn:
            row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
        if not row:
            return None
        window = TimeWindow(
            start=datetime.fromisoformat(row["window_start"]), end=datetime.fromisoformat(row["window_end"])
        )
        return InspectionRun(
            run_id=row["run_id"],
            scenario_id=row["scenario_id"],
            dataset_id=row["dataset_id"],
            window=window,
            granularity_seconds=int(row["granularity_seconds"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            status=RunStatus(row["status"]),
            source=row["source"],
            anomaly_count=int(row["anomaly_count"] or 0),
            level_counts=dict(row["level_counts"] or {}),
            score=row["score"],
            ai_degraded=bool(row["ai_degraded"]),
        )

    def previous_run(self, run_id: str, scenario_id: str | None = None) -> InspectionRun | None:
        """取上一次巡检。

        默认取时间上紧邻的上一次（任意场景），这样「上一次正常态 -> 本次故障态」
        的对比才讲得通；如果指定 scenario_id，则只跟同场景的上一次比。
        """
        with self.db.connect() as conn:
            current = conn.execute(
                select(runs.c.started_at_epoch).where(runs.c.run_id == run_id)
            ).scalar_one_or_none()
            if current is None:
                return None
            stmt = select(runs.c.run_id).where(runs.c.started_at_epoch < int(current))
            if scenario_id:
                stmt = stmt.where(runs.c.scenario_id == scenario_id)
            stmt = stmt.order_by(runs.c.started_at_epoch.desc()).limit(1)
            prev_id = conn.execute(stmt).scalar_one_or_none()
        return self.load_run(prev_id) if prev_id else None

    def score_history(self, limit: int = 12, scenario_id: str | None = None) -> list[ScoreSnapshot]:
        """历史评分快照（按时间正序返回，便于直接画趋势）。"""
        stmt = select(runs.c.run_id, runs.c.started_at, runs.c.score, runs.c.scenario_id).where(
            runs.c.score.is_not(None)
        )
        if scenario_id:
            stmt = stmt.where(runs.c.scenario_id == scenario_id)
        stmt = stmt.order_by(runs.c.started_at_epoch.desc()).limit(limit)
        with self.db.connect() as conn:
            rows = conn.execute(stmt).all()
        snapshots = [
            ScoreSnapshot(run_id=r[0], started_at=r[1], score=float(r[2]), scenario_id=r[3]) for r in reversed(rows)
        ]
        return snapshots

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                select(runs).order_by(runs.c.started_at_epoch.desc()).limit(limit)
            ).mappings().all()
        return [dict(r) for r in rows]

    def save_anomalies(self, run_id: str, items: Sequence[Anomaly]) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(anomalies).where(anomalies.c.run_id == run_id))
            if items:
                conn.execute(insert(anomalies), [{"run_id": run_id, **a.to_row()} for a in items])

    def save_clusters(
        self,
        run_id: str,
        items: Sequence[AnomalyCluster],
        root_causes: dict[str, dict[str, str]] | None = None,
    ) -> None:
        root_causes = root_causes or {}
        created = to_epoch(items[0].primary.first_seen) if items else 0
        rows = []
        for cluster in items:
            ai = root_causes.get(cluster.cluster_id, {})
            rows.append(
                {
                    "run_id": run_id,
                    "cluster_id": cluster.cluster_id,
                    "signature": cluster.signature(),
                    "primary_anomaly_id": cluster.primary.anomaly_id,
                    "max_level": cluster.max_level.code,
                    "size": cluster.size,
                    "propagation_path": cluster.propagation_path,
                    "services": cluster.services,
                    "hypothesis": cluster.hypothesis,
                    "root_cause_category": ai.get("category"),
                    "root_cause": ai.get("root_cause"),
                    "created_at_epoch": created,
                }
            )
        with self.db.begin() as conn:
            conn.execute(delete(clusters).where(clusters.c.run_id == run_id))
            if rows:
                conn.execute(insert(clusters), rows)

    def recent_clusters(
        self,
        limit: int = 200,
        exclude_run_id: str | None = None,
        exclude_window_end: str | None = None,
    ) -> list[dict[str, Any]]:
        """最近的历史根因簇。用于「复发识别」与「给 AI 提供同类历史事件」——
        这类「记忆」用签名 + 相似度就够，不需要向量库。

        `exclude_window_end` 传入当前巡检的窗口结束时刻，把**同一个窗口**的历史整体
        排除在外：复发意味着「同类问题在之后的窗口里又出现」，同一个窗口被反复看
        （重跑、回归、评测、不同切片）都不是复发。

        这一条不加会出真问题：同一个故障窗口被重跑几次，每次签名完全相同，
        于是每份报告都写上「历史复发」。但「同一轮故障被巡检两次」和「修好了又回来了」
        是两件完全不同的事——前者在报告里标成复发，会让读者以为修复无效，
        属于制造错误信息。

        按窗口而不是按场景来排除，是因为判定「是不是同一轮数据」的依据是时间窗口，
        不是它挂在哪个场景名下：不带场景的巡检读的是 `live_dataset` 切片，
        与同名场景读的是同一份数据，只按场景排除会让两者互相认成复发。
        """
        # 只认「有对应 run 记录」的簇：表结构漂移自愈会重建单张表，
        # 重建 runs 之后 clusters 里会留下孤立行——它们没有窗口、没有切片，
        # 却能被签名匹配上，于是恢复出厂设置后第一份报告就写着「历史复发」。
        stmt = select(clusters).where(clusters.c.run_id.in_(select(runs.c.run_id)))
        if exclude_run_id:
            stmt = stmt.where(clusters.c.run_id != exclude_run_id)
        if exclude_window_end is not None:
            same_window_runs = select(runs.c.run_id).where(runs.c.window_end == exclude_window_end)
            stmt = stmt.where(clusters.c.run_id.not_in(same_window_runs))
        stmt = stmt.order_by(clusters.c.created_at_epoch.desc()).limit(limit)
        with self.db.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def save_baselines(self, run_id: str, baseline_set: BaselineSet) -> None:
        rows = [
            {
                "run_id": run_id,
                "service": b.service,
                "instance": b.instance,
                "metric": b.metric.value,
                "median": None if b.median != b.median else b.median,
                "mad": b.mad,
                "p95": None if b.p95 != b.p95 else b.p95,
                "sample_size": b.sample_size,
                "mode": b.mode.value,
            }
            for b in baseline_set.baselines.values()
        ]
        with self.db.begin() as conn:
            conn.execute(delete(baselines).where(baselines.c.run_id == run_id))
            if rows:
                conn.execute(insert(baselines), rows)

    def save_ai(self, run_id: str, payload: dict[str, Any], meta: dict[str, Any]) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(ai_results).where(ai_results.c.run_id == run_id))
            conn.execute(insert(ai_results), [{"run_id": run_id, "payload": payload, "meta": meta}])

    def load_ai(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                select(ai_results.c.payload, ai_results.c.meta).where(ai_results.c.run_id == run_id)
            ).first()
        return {"payload": row[0], "meta": row[1]} if row else None

    def save_report(self, run_id: str, score: float, summary: str, markdown: str, payload: dict[str, Any]) -> None:
        with self.db.begin() as conn:
            conn.execute(delete(reports).where(reports.c.run_id == run_id))
            conn.execute(
                insert(reports),
                [
                    {
                        "run_id": run_id,
                        "score": score,
                        "summary": summary,
                        "markdown": markdown,
                        "payload": payload,
                        "created_at_epoch": to_epoch(datetime.now().astimezone()),
                    }
                ],
            )

    def load_report(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(select(reports).where(reports.c.run_id == run_id)).mappings().first()
        return dict(row) if row else None

    def load_anomalies(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                select(anomalies).where(anomalies.c.run_id == run_id).order_by(anomalies.c.level)
            ).mappings().all()
        return [dict(r) for r in rows]

    def load_clusters(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                select(clusters).where(clusters.c.run_id == run_id).order_by(clusters.c.max_level)
            ).mappings().all()
        return [dict(r) for r in rows]

    # ============================================================== 运维
    def stats(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            run_count = int(conn.execute(select(func.count()).select_from(runs)).scalar_one())
            anomaly_count = int(conn.execute(select(func.count()).select_from(anomalies)).scalar_one())
            point_count = int(conn.execute(select(func.count()).select_from(metric_points)).scalar_one())
        return {
            "runs": run_count,
            "anomalies": anomaly_count,
            "metric_points": point_count,
            "database": self.db.location(),
        }
