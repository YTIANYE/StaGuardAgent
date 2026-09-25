"""定时巡检调度。

支持两种触发形态，覆盖不同的部署场景：
- **间隔 / cron 常驻**：适合巡检本身很轻、需要秒级响应的场景（进程常驻，K8s Deployment）；
- **K8s CronJob**：适合巡检较重、平时不需要常驻进程的场景（见 `deploy/k8s/cronjob.yaml`）。

两条路径最终都走到同一个编排器，不存在「定时跑的逻辑和手动跑的不一样」这种分叉。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings
from .orchestrator import InspectionOrchestrator
from .store import Database, Repository
from .utils.logging import bind_run_context, clear_run_context

logger = logging.getLogger(__name__)

JOB_ID = "staguard-inspection"


class InspectionScheduler:
    def __init__(
        self,
        settings: Settings,
        repo: Repository | None = None,
        scenario_id: str | None = None,
        source_name: str | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.scenario_id = scenario_id
        self.source_name = source_name
        self.scheduler = BackgroundScheduler(
            timezone=settings.app.timezone,
            job_defaults={
                "coalesce": True,
                # 错过多次触发只补跑一次：巡检是「当前状态快照」，
                # 补跑五次的窗口全是过去的时间，只会产生五份过期报告
                "max_instances": 1,
                "misfire_grace_time": 300,
            },
        )

    def start(
        self,
        interval_minutes: int = 30,
        cron: str | None = None,
        run_immediately: bool = True,
    ) -> None:
        trigger = (
            CronTrigger.from_crontab(cron, timezone=self.settings.app.timezone)
            if cron
            else IntervalTrigger(minutes=interval_minutes, timezone=self.settings.app.timezone)
        )
        self.scheduler.add_job(self._job, trigger=trigger, id=JOB_ID, replace_existing=True, name="稳定性巡检")
        self.scheduler.start()
        logger.info(
            "定时巡检已启动：%s，场景 %s，数据源 %s",
            f"cron `{cron}`" if cron else f"每 {interval_minutes} 分钟",
            self.scenario_id or "全部",
            self.source_name or self.settings.app.default_source,
        )
        if run_immediately:
            self._job()

    def _job(self) -> None:
        """执行一次巡检。

        **异常必须被吞掉**：调度器里抛出异常会直接干掉整个 job 甚至调度器进程，
        一次数据库抖动就让定时巡检永久停摆——这是最隐蔽也最致命的线上故障之一。
        """
        try:
            orchestrator = InspectionOrchestrator(self.settings, self.repo)
            report = orchestrator.run(scenario_id=self.scenario_id, source_name=self.source_name)
            bind_run_context(report.run.run_id, self.scenario_id)
            logger.info(
                "定时巡检完成：评分 %.1f，异常 %d 条，状态 %s",
                report.score.total, report.run.anomaly_count, report.run.status.label,
            )
            clear_run_context()
        except Exception:  # noqa: BLE001 - 调度器必须活下去
            logger.exception("定时巡检执行失败，已跳过本次")

    def next_run_at(self):
        job = self.scheduler.get_job(JOB_ID)
        return job.next_run_time if job else None

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("定时巡检调度器已停止")


def build_default_scheduler(settings: Settings | None = None, scenario_id: str | None = None) -> InspectionScheduler:
    from .config import build_settings

    settings = settings or build_settings()
    database = Database(settings.app.db_url)
    database.init_schema()
    return InspectionScheduler(settings, Repository(database), scenario_id)
