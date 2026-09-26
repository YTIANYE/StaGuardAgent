"""模拟数据集生成器。

生成两层数据，对应真实世界的两种存储形态：

    archive  长期归档：14 天 @ 5 分钟，**完全干净**
             动态基线（hour-of-week 稳健统计）只从这里取样本。
             把它和注入数据分开，是为了避免「故障数据污染自己的基线」——
             这是动态阈值落地时最容易犯、也最难发现的错误。

    S0..S6   场景高精度：窗口前后 @ 1 分钟，故障注入发生在这一层
             高频数据只覆盖需要精细判定的时段，既省存储又贴合真实架构。

生成过程分五步，顺序不能乱（对应真实系统的因果链）：

    1. 构造干净世界      日周期 + 周周期 + 噪声 + 实例间天然差异
    2. 注入主动指标      流量 / 成功率 / 业务错误率 / 内存 / 延时
    3. 健康度传播        下游故障沿拓扑向上游扩散，按跳数衰减
    4. 推导耦合指标      错误数 = f(QPS, 成功率)；CPU、连接数 = f(吞吐, 延时)
    5. 覆盖型注入        CPU 打满 / 连接池打满这类「直接指定水位」的故障
    6. 制造不完美        缺失点、越界脏数据——数据质量模块的输入
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import ScenarioSpec, Settings
from ..models import MetricName, MetricPoint
from ..utils.timeutil import from_epoch, now, parse_iso, to_epoch
from .profiles import (
    BUSINESS_ERROR_RATE_BASE,
    GENERATION_RANGE,
    NOISE_CV,
    P95_RATIO,
    P99_RATIO,
    SUCCESS_RATE_BASE,
    clamp,
    daily_factor,
    profile_for,
    weekly_factor,
)

logger = logging.getLogger(__name__)

ARCHIVE_DATASET = "archive"
"""长期归档数据集标识。"""

# 注入分两类：主动指标先注入，耦合指标在耦合计算之后覆盖写入
_COUPLING_OVERRIDE_KINDS = {"cpu_spike", "conn_saturation"}

# 传播衰减：每往上一跳，影响衰减 20%
PROPAGATION_ATTENUATION = 0.8

# 单实例天然差异：同一服务的实例不会跑得一样快，这个差异是「实例不均衡」规则的正常底噪
INSTANCE_SPREAD: dict[MetricName, float] = {
    MetricName.QPS: 0.06,
    MetricName.SUCCESS_RATE: 0.00005,
    MetricName.BUSINESS_ERROR_RATE: 0.10,
    MetricName.LATENCY_P50: 0.08,
    MetricName.LATENCY_P95: 0.06,
    MetricName.LATENCY_P99: 0.07,
    MetricName.CPU_USAGE: 0.10,
    MetricName.MEM_USAGE: 0.05,
    MetricName.CONN_USAGE: 0.08,
    MetricName.ERROR_COUNT: 0.0,
}

VOLUME_METRICS = {MetricName.QPS}
LATENCY_METRICS = (MetricName.LATENCY_P50, MetricName.LATENCY_P95, MetricName.LATENCY_P99)


@dataclass
class GenerationResult:
    dataset_id: str
    points: int
    buckets: int
    start: datetime
    end: datetime
    step_seconds: int
    scenario_id: str | None = None

    def summary(self) -> str:
        return (
            f"{self.dataset_id}: {self.points} 点 / {self.buckets} 桶 / 步长 {self.step_seconds}s"
            f" / {self.start:%m-%d %H:%M} ~ {self.end:%m-%d %H:%M}"
        )


def _align(ts: datetime, step: int) -> datetime:
    epoch = to_epoch(ts)
    return from_epoch(epoch - epoch % step)


class WorldGrid:
    """一个数据集的全部时序，按 (service, instance, metric) 索引到与时间桶对齐的数组。

    用 numpy 会更快，但这里刻意用纯 Python：单次生成几百万个浮点数在本机只需要十几秒，
    而少一个重依赖能让「评审 clone 下来就能跑」这件事更稳。
    """

    def __init__(self, start: datetime, step_seconds: int, bucket_count: int) -> None:
        self.start = start
        self.step = step_seconds
        self.bucket_count = bucket_count
        self.values: dict[tuple[str, str, MetricName], list[float]] = {}

    def key(self, service: str, instance: str, metric: MetricName) -> tuple[str, str, MetricName]:
        return (service, instance, metric)

    def put(self, service: str, instance: str, metric: MetricName, series: list[float]) -> None:
        self.values[(service, instance, metric)] = series

    def get(self, service: str, instance: str, metric: MetricName) -> list[float]:
        return self.values[(service, instance, metric)]

    def has(self, service: str, instance: str, metric: MetricName) -> bool:
        return (service, instance, metric) in self.values

    def index_of(self, ts: datetime) -> int | None:
        offset = (ts - self.start).total_seconds()
        if offset < 0 or offset >= self.bucket_count * self.step:
            return None
        return int(offset // self.step)

    def ts_of(self, index: int) -> datetime:
        return from_epoch(to_epoch(self.start) + index * self.step)


class ScenarioWorld:
    """单个数据集（归档或某个场景）的生成器。"""

    def __init__(
        self,
        settings: Settings,
        dataset_id: str,
        start: datetime,
        end: datetime,
        step_seconds: int,
        scenario: ScenarioSpec | None = None,
        seed: int | None = None,
    ) -> None:
        self.settings = settings
        self.topology = settings.topology
        self.dataset_id = dataset_id
        self.scenario = scenario
        self.step = step_seconds
        self.start = _align(start, step_seconds)
        self.end = _align(end, step_seconds)
        bucket_count = max(1, int((self.end - self.start).total_seconds() // step_seconds))
        self.bucket_count = bucket_count
        self.grid = WorldGrid(self.start, step_seconds, bucket_count)
        self.rng = random.Random(seed if seed is not None else (scenario.seed if scenario else 0))
        self.clean: dict[tuple[str, str, MetricName], list[float]] = {}
        self.missing: set[tuple[str, str, MetricName, int]] = set()

    # ------------------------------------------------------------------ 主流程
    def build(self) -> GenerationResult:
        self._build_clean()
        self._snapshot_clean()
        injections = list(self.scenario.injections) if self.scenario else []
        self._apply_injections(injections, overrides=False)
        self._couple()
        self._apply_injections(injections, overrides=True)
        self._degrade_data_quality()
        return GenerationResult(
            dataset_id=self.dataset_id,
            points=len(self.grid.values) * self.bucket_count - len(self.missing),
            buckets=self.bucket_count,
            start=self.start,
            end=self.end,
            step_seconds=self.step,
            scenario_id=self.scenario.id if self.scenario else None,
        )

    def points(self) -> Iterator[MetricPoint]:
        for (service, instance, metric), series in self.grid.values.items():
            for index, value in enumerate(series):
                if (service, instance, metric, index) in self.missing:
                    continue
                yield MetricPoint(
                    ts=self.grid.ts_of(index),
                    service=service,
                    instance=instance,
                    metric=metric,
                    value=round(float(value), 4),
                )

    # ------------------------------------------------------------------ 1. 干净世界
    def _build_clean(self) -> None:
        for service, instance in self.topology.all_instances():
            profile = profile_for(service)
            spreads = self._instance_spread(service, instance)
            for metric in MetricName:
                self.grid.put(
                    service, instance, metric,
                    self._clean_series(service, instance, metric, profile, spreads[metric]),
                )

    def _instance_spread(self, service: str, instance: str) -> dict[MetricName, float]:
        """实例级持久偏移。用实例名的哈希做种子，保证同一实例每次生成结果一致。"""
        rng = random.Random(f"{service}/{instance}")
        return {
            metric: 1.0 + rng.uniform(-spread, spread)
            for metric, spread in INSTANCE_SPREAD.items()
        }

    def _clean_series(
        self,
        service: str,
        instance: str,
        metric: MetricName,
        profile,  # noqa: ANN001 - ServiceProfile
        spread: float,
    ) -> list[float]:
        rng = random.Random(f"{self.dataset_id}|{service}|{instance}|{metric.value}")
        cv = NOISE_CV[metric]
        out: list[float] = []
        for index in range(self.bucket_count):
            ts = self.grid.ts_of(index)
            cycle = daily_factor(ts) * weekly_factor(ts)
            base = self._clean_base(metric, profile, cycle) * spread
            noise = 1.0 + rng.gauss(0.0, cv) if cv else 1.0
            # 夹到物理合法区间：成功率不会超过 100%，业务错误率不会为负。
            # 不夹的话高斯噪声会造出 100.02% 这种值，被清洗模块当成脏数据剔除，
            # 于是干净场景凭空多出一堆数据质量问题——合成数据的自伤。
            low, high = GENERATION_RANGE[metric]
            out.append(clamp(base * noise, low, high))
        return out

    def _clean_base(self, metric: MetricName, profile, cycle: float) -> float:  # noqa: ANN001
        if metric is MetricName.QPS:
            return profile.qps * cycle
        if metric is MetricName.SUCCESS_RATE:
            # 用「不可靠度」建模：永远贴着 100% 附近，噪声绝对值极小
            return 100.0 - (100.0 - SUCCESS_RATE_BASE) * (1.0 + 0.35 * (1.0 - cycle))
        if metric is MetricName.BUSINESS_ERROR_RATE:
            return BUSINESS_ERROR_RATE_BASE * (1.0 + 0.3 * (cycle - 1.0))
        if metric is MetricName.LATENCY_P50:
            return profile.p50_ms * (1.0 + 0.22 * (cycle - 1.0))
        if metric is MetricName.LATENCY_P95:
            return profile.p50_ms * P95_RATIO * (1.0 + 0.26 * (cycle - 1.0))
        if metric is MetricName.LATENCY_P99:
            return profile.p50_ms * P99_RATIO * (1.0 + 0.30 * (cycle - 1.0))
        if metric is MetricName.CPU_USAGE:
            return profile.cpu * (0.68 + 0.5 * cycle)
        if metric is MetricName.MEM_USAGE:
            # 内存有极慢的自然漂移，为「内存增长趋势」规则提供正常底噪
            return profile.mem * (1.0 + 0.01 * (cycle - 1.0))
        if metric is MetricName.CONN_USAGE:
            return profile.conn * (0.8 + 0.3 * cycle)
        if metric is MetricName.ERROR_COUNT:
            return profile.qps * cycle * (100.0 - SUCCESS_RATE_BASE) / 100.0 * 60.0
        return 0.0

    def _snapshot_clean(self) -> None:
        self.clean = {key: list(values) for key, values in self.grid.values.items()}

    # ------------------------------------------------------------------ 2/5. 故障注入
    def _apply_injections(self, injections: list, overrides: bool) -> None:  # noqa: ANN001
        for spec in injections:
            is_override = spec.kind in _COUPLING_OVERRIDE_KINDS
            if is_override != overrides:
                continue
            targets = self._resolve_target(spec.target)
            if not targets:
                logger.warning("注入目标无法解析: %s", spec.target)
                continue
            start_idx, end_idx = self._injection_range(spec)
            if start_idx is None:
                continue
            ramp = self._ramp_curve(start_idx, end_idx)
            for service, instance in targets:
                delta = self._apply_one(spec, service, instance, start_idx, end_idx, ramp)
                if delta and spec.propagate:
                    self._propagate(service, delta, start_idx, end_idx)

    def _resolve_target(self, target: str) -> list[tuple[str, str]]:
        """把 `service` / `service/instance` 解析成待注入的实例列表。

        解析失败不会抛异常——场景配置是给人手写的，写错一个服务名不应该让整次
        数据生成崩掉，日志里能看见就够了。
        """
        parts = target.split("/", 1)
        service = parts[0]
        node = self.topology.node(service)
        if node is None:
            return []
        instances = [parts[1]] if len(parts) == 2 else list(node.instances)
        return [(service, inst) for inst in instances]

    def _injection_range(self, spec) -> tuple[int | None, int | None]:  # noqa: ANN001
        anchor = self._window_end()
        start = anchor + timedelta(minutes=spec.start_offset_minutes)
        end = start + timedelta(minutes=spec.duration_minutes)
        if start >= self.end:
            return None, None
        raw_start = self.grid.index_of(start)
        start_idx = 0 if raw_start is None else raw_start
        raw_end = self.grid.index_of(end)
        end_idx = self.bucket_count - 1 if raw_end is None else max(0, raw_end)
        if end_idx < start_idx:
            return None, None
        return start_idx, end_idx

    def _window_end(self) -> datetime:
        """注入的时间锚点：场景若指定了 window_end 用它，否则用数据集结束时刻。"""
        if self.scenario and self.scenario.window_end:
            return parse_iso(self.scenario.window_end)
        return self.end

    def _ramp_curve(self, start_idx: int, end_idx: int) -> list[float]:
        """注入的进出斜坡。

        真实故障不是 0 和 1 的阶跃：流量是逐步涨上去的，故障也有一个劣化过程。
        没有斜坡的数据会让「持续时长」这类判定失真，也是合成数据一眼假的主要原因。
        """
        length = end_idx - start_idx + 1
        ramp_len = max(1, min(int(length * 0.15), max(1, int(180 / self.step))))
        curve: list[float] = []
        for i in range(length):
            if i < ramp_len:
                curve.append((i + 1) / ramp_len)
            elif length - i <= ramp_len:
                curve.append((length - i) / ramp_len)
            else:
                curve.append(1.0)
        return curve

    def _apply_one(self, spec, service, instance, start_idx, end_idx, ramp) -> dict | None:  # noqa: ANN001
        """施加单条注入，返回它对目标造成的健康度变化（供传播使用）。"""
        kind = spec.kind
        params = spec.params or {}

        if kind == "latency_spike":
            delta = self._inject_latency(service, instance, start_idx, ramp, params)
            return {"kind": "latency", "lat_abs": delta} if delta else None

        if kind == "success_rate_drop":
            delta = self._inject_success_rate(service, instance, start_idx, ramp, float(params["value"]))
            return {"kind": "success_rate", "sr_drop": delta} if delta else None

        if kind == "business_error_spike":
            self._inject_business_error(service, instance, start_idx, ramp, float(params["value"]))
            return None

        if kind == "traffic_burst":
            multiplier = float(params.get("multiplier", 2.0))
            self._inject_qps(service, instance, start_idx, ramp, multiplier)
            return {"kind": "qps", "multiplier": multiplier}

        if kind == "traffic_drop":
            multiplier = 1.0 - float(params.get("down_pct", 50.0)) / 100.0
            self._inject_qps(service, instance, start_idx, ramp, multiplier)
            return {"kind": "qps", "multiplier": multiplier}

        if kind == "cpu_spike":
            self._inject_waterline(service, instance, MetricName.CPU_USAGE, start_idx, ramp, float(params["value"]))
            return None

        if kind == "conn_saturation":
            self._inject_waterline(service, instance, MetricName.CONN_USAGE, start_idx, ramp, float(params["value"]))
            return None

        if kind == "memory_leak":
            self._inject_memory_leak(service, instance, start_idx, end_idx, params)
            return None

        logger.warning("未知的注入类型: %s", kind)
        return None

    # ---- 各类注入的原子实现 ----
    def _inject_latency(self, service: str, instance: str, start_idx: int, ramp: list[float], params: dict) -> float:
        """按「P99 目标值」整体抬高三个分位数，保持分位比例关系不失真。"""
        target_ms = float(params["target_ms"])
        clean_p99 = self.clean[(service, instance, MetricName.LATENCY_P99)]
        reference = clean_p99[start_idx] or 1.0
        factor = target_ms / reference
        for offset, weight in enumerate(ramp):
            index = start_idx + offset
            if index >= self.bucket_count:
                break
            applied = 1.0 + weight * (factor - 1.0)
            for metric in LATENCY_METRICS:
                series = self.grid.get(service, instance, metric)
                clean = self.clean[(service, instance, metric)]
                series[index] = clean[index] * applied
        return max(0.0, reference * (factor - 1.0))

    def _inject_success_rate(
        self, service: str, instance: str, start_idx: int, ramp: list[float], value: float
    ) -> float:
        series = self.grid.get(service, instance, MetricName.SUCCESS_RATE)
        clean = self.clean[(service, instance, MetricName.SUCCESS_RATE)]
        for offset, weight in enumerate(ramp):
            index = start_idx + offset
            if index >= self.bucket_count:
                break
            series[index] = clean[index] + weight * (value - clean[index])
        return max(0.0, clean[start_idx] - min(value, clean[start_idx]))

    def _inject_business_error(
        self, service: str, instance: str, start_idx: int, ramp: list[float], value: float
    ) -> None:
        series = self.grid.get(service, instance, MetricName.BUSINESS_ERROR_RATE)
        clean = self.clean[(service, instance, MetricName.BUSINESS_ERROR_RATE)]
        for offset, weight in enumerate(ramp):
            index = start_idx + offset
            if index >= self.bucket_count:
                break
            series[index] = clean[index] + weight * (value - clean[index])

    def _inject_qps(self, service: str, instance: str, start_idx: int, ramp: list[float], multiplier: float) -> None:
        series = self.grid.get(service, instance, MetricName.QPS)
        clean = self.clean[(service, instance, MetricName.QPS)]
        for offset, weight in enumerate(ramp):
            index = start_idx + offset
            if index >= self.bucket_count:
                break
            series[index] = clean[index] * (1.0 + weight * (multiplier - 1.0))

    def _inject_waterline(
        self, service: str, instance: str, metric: MetricName, start_idx: int, ramp: list[float], value: float
    ) -> None:
        series = self.grid.get(service, instance, metric)
        clean = self.clean[(service, instance, metric)]
        for offset, weight in enumerate(ramp):
            index = start_idx + offset
            if index >= self.bucket_count:
                break
            series[index] = clean[index] + weight * (value - clean[index])

    def _inject_memory_leak(self, service: str, instance: str, start_idx: int, end_idx: int, params: dict) -> None:
        """内存泄漏：线性爬升，没有进出斜坡——泄漏就是这么一段单调上升的斜线。"""
        series = self.grid.get(service, instance, MetricName.MEM_USAGE)
        start_value = float(params.get("start_value", 40.0))
        end_value = float(params.get("end_value", 90.0))
        span = max(1, end_idx - start_idx)
        for index in range(start_idx, min(end_idx, self.bucket_count - 1) + 1):
            progress = (index - start_idx) / span
            series[index] = start_value + (end_value - start_value) * progress

    # ------------------------------------------------------------------ 3. 健康度传播
    def _propagate(self, source_service: str, delta: dict, start_idx: int, end_idx: int) -> None:
        """把故障沿依赖拓扑向上游传播，按跳数衰减。

        这是「根因簇」能成立的前提：只有数据里真的存在传播关系，
        「下游是根因、上游是影响面」这个结论才是被验证出来的，而不是被讲出来的。
        """
        kind = delta.get("kind")
        if kind == "qps":
            # 流量是守恒的：入口流量变了，下游必然等比例变化，不衰减
            for descendant in self.topology.descendants(source_service):
                self._propagate_qps(descendant, delta["multiplier"], start_idx, end_idx)
            return

        ancestors = self.topology.ancestors(source_service)
        if not ancestors:
            return
        hops = self._hop_distances(source_service)
        for ancestor in ancestors:
            weight = PROPAGATION_ATTENUATION ** hops.get(ancestor, 1)
            if kind == "success_rate":
                self._propagate_success_rate(ancestor, delta["sr_drop"], weight, start_idx, end_idx)
            elif kind == "latency":
                self._propagate_latency(ancestor, delta["lat_abs"], weight, start_idx, end_idx)

    def _propagate_qps(self, service: str, multiplier: float, start_idx: int, end_idx: int) -> None:
        """下游流量跟随上游变化。

        这条关系是 S5「流量突降」能被解释的前提：监控里所有服务都健康，
        只有流量集体萎缩——不沿拓扑追，就只会看到一堆互不相关的静默指标。
        """
        for instance in self.topology.node(service).instances:
            series = self.grid.get(service, instance, MetricName.QPS)
            clean = self.clean[(service, instance, MetricName.QPS)]
            for index in range(start_idx, min(end_idx, self.bucket_count - 1) + 1):
                current_ratio = series[index] / clean[index] if clean[index] else 1.0
                # 取偏离更极端的那个，避免多次叠加把倍数越乘越大
                if abs(multiplier - 1.0) > abs(current_ratio - 1.0):
                    series[index] = clean[index] * multiplier

    def _hop_distances(self, source: str) -> dict[str, int]:
        """到各个上游服务的跳数。用 BFS 从 source 反向走。"""
        from collections import deque

        distances: dict[str, int] = {}
        queue: deque[tuple[str, int]] = deque((u, 1) for u in self.topology.upstream(source))
        while queue:
            node, distance = queue.popleft()
            if node in distances and distances[node] <= distance:
                continue
            distances[node] = distance
            for parent in self.topology.upstream(node):
                queue.append((parent, distance + 1))
        return distances

    def _propagate_success_rate(self, service: str, sr_drop: float, weight: float, start_idx: int, end_idx: int) -> None:
        """上游成功率按「不可靠度沿依赖传递」衰减。

        用 min() 而不是直接赋值：多个下游同时故障时取最坏影响，
        且不会覆盖掉上游自己被显式注入的更严重结果。
        """
        drop = sr_drop * weight
        for instance in self.topology.node(service).instances:
            series = self.grid.get(service, instance, MetricName.SUCCESS_RATE)
            clean = self.clean[(service, instance, MetricName.SUCCESS_RATE)]
            for index in range(start_idx, min(end_idx, self.bucket_count - 1) + 1):
                series[index] = min(series[index], max(60.0, clean[index] - drop))

    def _propagate_latency(self, service: str, lat_abs: float, weight: float, start_idx: int, end_idx: int) -> None:
        """上游延时叠加下游延时增量。P50 打得轻一些，长尾放得更大——
        真实级联里中位数往往还稳，长尾先崩。"""
        added = lat_abs * weight
        if added <= 0:
            return
        for instance in self.topology.node(service).instances:
            for metric in LATENCY_METRICS:
                share = 0.6 if metric is MetricName.LATENCY_P50 else 1.0
                series = self.grid.get(service, instance, metric)
                clean = self.clean[(service, instance, metric)]
                for index in range(start_idx, min(end_idx, self.bucket_count - 1) + 1):
                    series[index] = max(series[index], clean[index] + added * share)

    # ------------------------------------------------------------------ 4. 耦合推导
    def _couple(self) -> None:
        """由驱动量推导被导出量：「成功率先跌、错误数才涨」这条因果链必须在数据里成立。"""
        for service, instance in self.topology.all_instances():
            qps = self.grid.get(service, instance, MetricName.QPS)
            clean_qps = self.clean[(service, instance, MetricName.QPS)]
            sr = self.grid.get(service, instance, MetricName.SUCCESS_RATE)
            clean_p99 = self.clean[(service, instance, MetricName.LATENCY_P99)]
            p99 = self.grid.get(service, instance, MetricName.LATENCY_P99)
            clean_cpu = self.clean[(service, instance, MetricName.CPU_USAGE)]
            clean_conn = self.clean[(service, instance, MetricName.CONN_USAGE)]
            cpu = self.grid.get(service, instance, MetricName.CPU_USAGE)
            conn = self.grid.get(service, instance, MetricName.CONN_USAGE)
            errors = self.grid.get(service, instance, MetricName.ERROR_COUNT)

            for index in range(self.bucket_count):
                qps_ratio = qps[index] / clean_qps[index] if clean_qps[index] else 1.0
                lat_ratio = p99[index] / clean_p99[index] if clean_p99[index] else 1.0
                errors[index] = max(0.0, qps[index] * (100.0 - sr[index]) / 100.0 * 60.0)
                cpu[index] = clamp(
                    clean_cpu[index] * (0.68 + 0.5 * qps_ratio) + min(30.0, 10.0 * max(0.0, lat_ratio - 1.0)),
                    3.0, 99.0,
                )
                conn[index] = clamp(
                    clean_conn[index] * (0.8 + 0.3 * qps_ratio) + min(40.0, 25.0 * max(0.0, lat_ratio - 1.0)),
                    3.0, 99.0,
                )

    # ------------------------------------------------------------------ 6. 数据不完美
    def _degrade_data_quality(self) -> None:
        """制造缺失点与越界脏数据。

        一份 100% 干净的数据集是没有说服力的：真实监控数据一定会缺采样、会有脏值，
        而「清洗」与「数据质量降级」正是巡检链路里最容易被忽略的一环。
        """
        total = self.bucket_count * len(self.grid.values)
        missing_target = int(total * 0.0015)
        keys = list(self.grid.values)
        for _ in range(missing_target):
            key = self.rng.choice(keys)
            index = self.rng.randrange(self.bucket_count)
            self.missing.add((key[0], key[1], key[2], index))

        # 归档层额外塞几个明显越界的脏点（success_rate 超过 100%），
        # 用于验证标准化模块的越界清洗确实生效。场景层不放，避免干扰判定。
        if self.dataset_id == ARCHIVE_DATASET:
            sr_keys = [k for k in keys if k[2] is MetricName.SUCCESS_RATE]
            for _ in range(3):
                key = self.rng.choice(sr_keys)
                index = self.rng.randrange(self.bucket_count)
                self.grid.values[key][index] = 103.7 + self.rng.random()


# ---------------------------------------------------------------------- 组装入口
@dataclass
class BuildPlan:
    dataset_id: str
    start: datetime
    end: datetime
    step_seconds: int
    scenario: ScenarioSpec | None = None


@dataclass
class BuildReport:
    results: list[GenerationResult] = field(default_factory=list)

    def total_points(self) -> int:
        return sum(r.points for r in self.results)

    def summary(self) -> str:
        return f"共 {len(self.results)} 个数据集 / {self.total_points()} 个指标点"


def required_history_minutes(settings: Settings) -> int:
    """采集/生成需要多长的观察窗——**由规则本身决定**。

    趋势型规则（内存泄漏、性能劣化）声明的 `lookback_hours` 就是硬需求：
    如果按固定的 30 分钟窗口去采集，内存泄漏这类慢故障在数据层面就已经看不到了，
    规则写得再好也无从判定。让「采集范围」由「判定需求」反推，是巡检链路设计里
    很容易被忽略、但一旦漏掉就整条链路失效的一环。
    """
    lookbacks = [
        int(float(rule.params.get("lookback_hours", 0)) * 60)
        for rule in settings.rules.enabled()
        if rule.params.get("lookback_hours")
    ]
    return max([settings.scenarios.scenario_history_minutes, settings.window_minutes, *lookbacks])


def build_plans(settings: Settings) -> tuple[datetime, list[BuildPlan]]:
    """规划要生成哪些数据集。"""
    scenarios_cfg = settings.scenarios
    if scenarios_cfg.dataset_end:
        dataset_end = _align(parse_iso(scenarios_cfg.dataset_end), settings.granularity_seconds)
    else:
        dataset_end = _align(now(), settings.granularity_seconds)

    archive_step = scenarios_cfg.baseline_interval_seconds
    archive_start = dataset_end - timedelta(days=scenarios_cfg.baseline_history_days)
    plans = [
        BuildPlan(
            dataset_id=ARCHIVE_DATASET,
            start=archive_start,
            end=dataset_end,
            step_seconds=archive_step,
        )
    ]
    required = required_history_minutes(settings)
    for scenario in scenarios_cfg.scenarios:
        history = max(scenario.history_minutes or 0, required)
        plans.append(
            BuildPlan(
                dataset_id=scenario.id,
                start=dataset_end - timedelta(minutes=history),
                end=dataset_end,
                step_seconds=settings.granularity_seconds,
                scenario=scenario,
            )
        )
    return dataset_end, plans


__all__ = [
    "ARCHIVE_DATASET",
    "BuildPlan",
    "BuildReport",
    "GenerationResult",
    "ScenarioWorld",
    "WorldGrid",
    "build_plans",
    "required_history_minutes",
]
