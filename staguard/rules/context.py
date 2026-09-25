"""规则求值上下文。

规则只通过这个上下文访问数据，不直接碰仓储 SQL：
这样规则的单元测试可以完全脱离数据库（注入一个假上下文即可），
也让「一次巡检到底读了哪些数据」这件事变得可审计。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ..config import Settings, SLOSpec, ThresholdSpec
from ..models import (
    Baseline,
    ChangeEvent,
    MetricName,
    MetricSeries,
    TimeWindow,
    Topology,
)
from ..store import Repository
from .provider import BaselineProvider

logger = logging.getLogger(__name__)

CHANGE_LOOKBACK_HOURS = 24
"""归因时回看的变更时间范围。真实发布事故的暴露窗口通常在小时级，
超过一天才显现的问题就很难归因为「变更引入」了。"""


class EvalContext:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        window: TimeWindow,
        detect_dataset: str,
        archive_dataset: str,
        scenario_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.window = window
        self.detect_dataset = detect_dataset
        self.archive_dataset = archive_dataset
        self.scenario_id = scenario_id
        self.topology: Topology = settings.topology
        self._series_cache: dict[tuple[str, str, MetricName, str], MetricSeries] = {}
        self._service_cache: dict[tuple[str, MetricName, str], dict[str, MetricSeries]] = {}
        self._changes: list[ChangeEvent] | None = None
        self.provider = BaselineProvider(settings, repo, archive_dataset, window)
        self.step_minutes = settings.granularity_seconds / 60.0

    # ------------------------------------------------------------------ 范围
    def instances(self) -> list[tuple[str, str]]:
        return self.topology.all_instances()

    def instances_of(self, service: str) -> list[str]:
        node = self.topology.node(service)
        return list(node.instances) if node else []

    def services(self) -> list[str]:
        return [name for name, node in self.topology.services.items() if not node.external]

    def all_services(self) -> list[str]:
        return self.topology.names()

    # ------------------------------------------------------------------ 时序
    def series(
        self,
        service: str,
        instance: str,
        metric: MetricName,
        window: TimeWindow | None = None,
    ) -> MetricSeries:
        target = window or self.window
        key = (service, instance, metric, target.start.isoformat())
        if key not in self._series_cache:
            self._series_cache[key] = self.repo.fetch_series(
                dataset_id=self.detect_dataset,
                service=service,
                instance=instance,
                metric=metric,
                start=target.start,
                end=target.end,
            )
        return self._series_cache[key]

    def service_series(
        self,
        service: str,
        metric: MetricName,
        window: TimeWindow | None = None,
    ) -> dict[str, MetricSeries]:
        target = window or self.window
        key = (service, metric, target.start.isoformat())
        if key not in self._service_cache:
            self._service_cache[key] = self.repo.fetch_service_series(
                dataset_id=self.detect_dataset,
                service=service,
                metric=metric,
                start=target.start,
                end=target.end,
                instances=self.instances_of(service),
            )
        return self._service_cache[key]

    def lookback_window(self, hours: float) -> TimeWindow:
        """趋势型规则的观察窗。

        注意它可能比本次采集实际覆盖的范围更长——仓储会如实返回「有多少给多少」，
        规则端再用 `min_samples` 把「样本不够、不许下结论」这件事挡住。
        这比假装数据齐全要诚实得多。
        """
        return TimeWindow(start=self.window.end - timedelta(hours=hours), end=self.window.end)

    # ------------------------------------------------------------------ 基线 / 策略
    def baseline(self, service: str, instance: str, metric: MetricName) -> Baseline:
        return self.provider.get(service, instance, metric)

    def slo(self, service: str) -> SLOSpec:
        return self.settings.services.slo_for(service)

    def threshold(self, metric: MetricName, service: str | None = None) -> ThresholdSpec:
        return self.settings.thresholds.for_metric(metric.value, service)

    def criticality(self, service: str) -> float:
        return self.topology.criticality_of(service)

    def owner(self, service: str) -> str:
        return self.settings.services.owner_of(service)

    # ------------------------------------------------------------------ 变更
    def changes(self) -> list[ChangeEvent]:
        if self._changes is None:
            self._changes = self.repo.fetch_changes(
                start=self.window.end - timedelta(hours=CHANGE_LOOKBACK_HOURS),
                end=self.window.end,
                scenario_id=self.scenario_id,
            )
        return self._changes

    def changes_near(self, ts, hours: float = 2.0) -> list[ChangeEvent]:  # noqa: ANN001
        """与给定时刻相近的变更。用于「变更引入」的证据收集。"""
        from ..utils.timeutil import to_epoch

        anchor = to_epoch(ts)
        span = hours * 3600
        return [c for c in self.changes() if -span <= (anchor - to_epoch(c.ts)) <= span]


def build_context(
    settings: Settings,
    repo: Repository,
    window: TimeWindow,
    detect_dataset: str,
    archive_dataset: str,
    scenario_id: str | None = None,
) -> EvalContext:
    return EvalContext(settings, repo, window, detect_dataset, archive_dataset, scenario_id)
