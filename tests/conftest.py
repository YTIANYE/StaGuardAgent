"""pytest 公共 fixture。

测试策略：**单元测试全部脱离数据库**，端到端测试才用真实的 SQLite 与生成好的数据集。
理由很直接——规则、分级、评分这些最容易出 bug 的逻辑，如果用 mock 数据测，
测的是 mock 的行为；而一旦真的接上数据库，测试又会慢到没人愿意跑。
分层之后，单元测试毫秒级、可以放心重构，端到端测试只跑关键的几条断言。
"""

from __future__ import annotations

import pytest

from staguard.config import Settings, build_settings
from staguard.models import (
    Baseline,
    BaselineMode,
    MetricName,
    MetricPoint,
    MetricSeries,
    Severity,
    TimeWindow,
)
from staguard.rules.leveling import MatchedLevel
from staguard.utils.timeutil import from_epoch, to_epoch

BASE_EPOCH = to_epoch(from_epoch(1_790_000_000))


@pytest.fixture(scope="session")
def settings() -> Settings:
    return build_settings()


def make_window(minutes: int = 30, end_epoch: int | None = None) -> TimeWindow:
    end = end_epoch if end_epoch is not None else BASE_EPOCH
    from datetime import timedelta

    anchor = from_epoch(end)
    return TimeWindow(start=anchor - timedelta(minutes=minutes), end=anchor)


def make_series(
    values: list[float],
    metric: MetricName = MetricName.SUCCESS_RATE,
    service: str = "order-svc",
    instance: str = "order-svc-0",
    step_seconds: int = 60,
    end_epoch: int | None = None,
) -> MetricSeries:
    """按给定的值构造一条时序，末点对齐到 end_epoch。"""
    end = end_epoch if end_epoch is not None else BASE_EPOCH
    start = end - (len(values) - 1) * step_seconds
    return MetricSeries(
        service=service,
        instance=instance,
        metric=metric,
        points=[(from_epoch(start + i * step_seconds), float(v)) for i, v in enumerate(values)],
        unit=metric.unit,
    )


def make_baseline(
    median: float,
    mad: float = 0.05,
    metric: MetricName = MetricName.SUCCESS_RATE,
    service: str = "order-svc",
    instance: str = "order-svc-0",
    mode: BaselineMode = BaselineMode.DYNAMIC,
    sample_size: int = 200,
) -> Baseline:
    return Baseline(
        service=service,
        instance=instance,
        metric=metric,
        median=median,
        mad=mad,
        mad_scaled=max(mad * 1.4826, abs(median) * 0.008),
        mean=median,
        stdev=mad,
        p50=median,
        p95=median,
        p99=median,
        min=median,
        max=median,
        sample_size=sample_size,
        mode=mode,
        static_min=99.0 if metric is MetricName.SUCCESS_RATE else None,
        static_max=None if metric is MetricName.SUCCESS_RATE else 800.0,
    )


class FakeContext:
    """脱离数据库的规则求值上下文。

    规则的可测性是刻意设计出来的：`EvalContext` 只暴露「取时序 / 取基线 / 取策略」
    这几个方法，测试里换成内存实现即可。于是「给一段时序，断言判定结果」
    这类测试可以毫秒级跑完，重构规则时才有底气。
    """

    def __init__(
        self,
        settings: Settings,
        series_map: dict[tuple[str, str, MetricName], MetricSeries],
        baseline_map: dict[tuple[str, str, MetricName], Baseline] | None = None,
        changes: list | None = None,
    ) -> None:
        self.settings = settings
        self.topology = settings.topology
        self._series = series_map
        self._baselines = baseline_map or {}
        self._changes = changes or []
        self._instances = sorted({(service, instance) for service, instance, _ in series_map})
        starts = [series.points[0][0] for series in series_map.values() if series.points]
        ends = [series.points[-1][0] for series in series_map.values() if series.points]
        self.window = TimeWindow(start=min(starts), end=max(ends)) if starts else make_window()
        self.step_minutes = 1.0

    def instances(self):  # noqa: ANN201
        return list(self._instances)

    def instances_of(self, service: str) -> list[str]:
        return [inst for svc, inst in self._instances if svc == service]

    def all_services(self) -> list[str]:
        return list(self.topology.names())

    def series(self, service, instance, metric, window=None):  # noqa: ANN001, ARG002
        return self._series.get(
            (service, instance, metric),
            MetricSeries(service=service, instance=instance, metric=metric, points=[]),
        )

    def service_series(self, service, metric, window=None):  # noqa: ANN001, ARG002
        return {
            instance: series
            for (svc, instance, m), series in self._series.items()
            if svc == service and m is metric
        }

    def baseline(self, service, instance, metric):  # noqa: ANN001
        return self._baselines.get(
            (service, instance, metric),
            make_baseline(median=0.0, metric=metric, service=service, instance=instance),
        )

    def threshold(self, metric, service=None):  # noqa: ANN001
        return self.settings.thresholds.for_metric(metric.value, service)

    def slo(self, service: str):  # noqa: ANN201
        return self.settings.services.slo_for(service)

    def criticality(self, service: str) -> float:
        return self.topology.criticality_of(service)

    def owner(self, service: str) -> str:
        return self.settings.services.owner_of(service)

    def changes(self):  # noqa: ANN201
        return list(self._changes)

    def lookback_window(self, hours: float) -> TimeWindow:
        from datetime import timedelta

        return TimeWindow(start=self.window.end - timedelta(hours=hours), end=self.window.end)


@pytest.fixture
def matched() -> MatchedLevel:
    return MatchedLevel(level=Severity.P2, reason="测试用分级", conditions={})


@pytest.fixture
def points_factory():
    def factory(count: int, metric: MetricName, value: float, step: int = 60) -> list[MetricPoint]:
        start = BASE_EPOCH - count * step
        return [
            MetricPoint(
                ts=from_epoch(start + i * step),
                service="order-svc",
                instance="order-svc-0",
                metric=metric,
                value=value,
            )
            for i in range(count)
        ]

    return factory
