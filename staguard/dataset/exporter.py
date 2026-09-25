"""把生成的世界导出成「本地结构化指标文件」。

导出的是**窗口 + 观察期**的切片，而不是把 14 天全量倒出来：
真实巡检也不会把归档全量拉一遍，只取判定需要的那一段。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..config import Settings
from ..utils.timeutil import now
from .generator import ARCHIVE_DATASET, ScenarioWorld, build_plans
from .store_file import DatasetFile, FileMetricStore, SeriesBlob

logger = logging.getLogger(__name__)


def _scenario_world(settings: Settings, plan) -> ScenarioWorld:  # noqa: ANN001
    return ScenarioWorld(
        settings=settings,
        dataset_id=plan.dataset_id,
        start=plan.start,
        end=plan.end,
        step_seconds=plan.step_seconds,
        scenario=plan.scenario,
    )


def export_scenarios(settings: Settings, store: FileMetricStore | None = None) -> list[str]:
    """生成全部场景的数据集文件，返回写出的 dataset_id 列表。"""
    store = store or FileMetricStore(settings.app.data_dir / "metrics")
    dataset_end, plans = build_plans(settings)
    written: list[str] = []

    for plan in plans:
        if plan.scenario is None:
            continue  # 归档数据走数据库，不进文件通道
        world = _scenario_world(settings, plan)
        world.build()
        dataset = DatasetFile(
            dataset_id=plan.dataset_id,
            scenario_id=plan.scenario.id,
            scenario_name=plan.scenario.name,
            step_seconds=plan.step_seconds,
            start_epoch=int(world.start.timestamp()),
            end_epoch=int(world.end.timestamp()),
            generated_at=now().isoformat(timespec="seconds"),
            expected_root_cause=plan.scenario.expected_root_cause,
            series=_to_blobs(world),
        )
        path = store.save(dataset)
        written.append(plan.dataset_id)
        logger.info("导出场景数据集 %s -> %s（%s）", plan.dataset_id, path.name, dataset.summary())

    return written


def _to_blobs(world: ScenarioWorld) -> list[SeriesBlob]:
    """把世界网格压成紧凑的「值数组」形态。缺失点写 null，让清洗环节有真实输入。"""
    start_epoch = int(world.start.timestamp())
    blobs: list[SeriesBlob] = []
    for (service, instance, metric), series in world.grid.values.items():
        values: list[float | None] = []
        for index, value in enumerate(series):
            if (service, instance, metric, index) in world.missing:
                values.append(None)
            else:
                values.append(round(float(value), 4))
        blobs.append(
            SeriesBlob(
                service=service,
                instance=instance,
                metric=metric.value,
                unit=metric.unit,
                start_epoch=start_epoch,
                step_seconds=world.step,
                values=values,
            )
        )
    return blobs


def archive_window(settings: Settings) -> tuple[datetime, datetime]:
    """归档数据覆盖的时间范围。"""
    dataset_end, plans = build_plans(settings)
    archive = next(p for p in plans if p.dataset_id == ARCHIVE_DATASET)
    return archive.start, archive.end


def observation_start(settings: Settings, history_minutes: int, end: datetime) -> datetime:
    return end - timedelta(minutes=history_minutes)
