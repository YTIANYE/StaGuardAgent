"""历史归档同步。

把 14 天干净历史写入 `metric_points` 表的 `archive` 数据集，作为动态基线的样本池。
这一步等价于真实系统里「从 TSDB / 数仓导入长周期历史」的初始化作业，
也是本项目的离线部分；在线部分（每次巡检）由 collector 负责。
"""

from __future__ import annotations

import logging
import time

from ..config import Settings
from ..store import Repository
from .generator import ARCHIVE_DATASET, BuildReport, GenerationResult, ScenarioWorld, build_plans

logger = logging.getLogger(__name__)


def sync_archive(settings: Settings, repo: Repository, force: bool = False) -> GenerationResult | None:
    """生成并写入归档数据集。已存在且未指定 force 时跳过，避免重复十几秒的重活。"""
    dataset_end, plans = build_plans(settings)
    plan = next((p for p in plans if p.dataset_id == ARCHIVE_DATASET), None)
    if plan is None:
        raise RuntimeError("未找到归档数据集计划，请检查 config/scenarios.yaml")

    existing = repo.count_points(ARCHIVE_DATASET)
    if existing and not force:
        logger.info("归档数据集已存在（%d 点），跳过。需要重建请加 --force", existing)
        return None

    if existing:
        repo.delete_dataset(ARCHIVE_DATASET)

    started = time.perf_counter()
    world = ScenarioWorld(
        settings=settings,
        dataset_id=plan.dataset_id,
        start=plan.start,
        end=plan.end,
        step_seconds=plan.step_seconds,
    )
    result = world.build()
    inserted = repo.insert_metric_points(plan.dataset_id, plan.step_seconds, world.points())
    elapsed = time.perf_counter() - started
    logger.info(
        "归档同步完成：%d 点，耗时 %.1fs（%.0f 点/秒），覆盖 %s ~ %s",
        inserted, elapsed, inserted / elapsed if elapsed else 0,
        result.start.strftime("%m-%d %H:%M"), result.end.strftime("%m-%d %H:%M"),
    )
    return result


def build_all(settings: Settings, repo: Repository, archive: bool = True, files: bool = True) -> BuildReport:
    """一键初始化：归档入库 + 场景导出为文件。"""
    from .exporter import export_scenarios

    report = BuildReport()
    if archive:
        result = sync_archive(settings, repo, force=True)
        if result:
            report.results.append(result)
    if files:
        from .store_file import FileMetricStore

        store = FileMetricStore(settings.app.data_dir / "metrics")
        written = export_scenarios(settings, store)
        logger.info("已导出 %d 个场景数据集文件", len(written))
    # 一次性写入两百多万行之后刷新查询计划统计信息，
    # 否则紧接着的第一次巡检会因为错误的执行计划慢一个数量级（见 Database.optimize）。
    repo.db.optimize()
    return report


__all__ = ["ARCHIVE_DATASET", "build_all", "sync_archive"]
