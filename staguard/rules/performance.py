"""性能层专有规则：长尾恶化、性能劣化趋势。

这两条对应的是「阈值抓不到的性能问题」：

- **长尾恶化**（PERF-04）：P50 完全正常，P99 却飙了。均值指标全绿，
  但 1% 的用户在等 3 秒。看 P99/P50 的比值比看任何一个绝对值都敏感。
- **性能劣化趋势**（PERF-05）：每一分钟都还在阈值以内，斜率却明确向上。
  等它超阈就已经是事故了，这是唯一能提前发现它的规则。
"""

from __future__ import annotations

import logging
import statistics

from ..models import AnomalyFinding, Baseline, BaselineMode, MetricName, MetricSeries
from ..utils.timeutil import from_epoch, to_epoch
from .base import Rule, SeriesFacts, series_step_minutes
from .context import EvalContext
from .leveling import (
    MatchedLevel,
    amplitude_from_value,
    amplitude_from_z,
    bounds_for_key,
    match_level_series,
)

logger = logging.getLogger(__name__)


class LongTailRule(Rule):
    """PERF-04 长尾恶化：P99 / P50 比值。"""

    def evaluate(self, ctx: EvalContext) -> list[AnomalyFinding]:
        numerator = MetricName(self.params.get("numerator", "latency_p99"))
        denominator = MetricName(self.params.get("denominator", "latency_p50"))
        findings: list[AnomalyFinding] = []

        for service, instance in ctx.instances():
            top = ctx.series(service, instance, numerator)
            bottom = ctx.series(service, instance, denominator)
            if len(top.points) < 3 or len(bottom.points) < 3:
                continue

            ratios = _align_ratio(top, bottom)
            if not ratios:
                continue
            observed_ratio = max(ratios.values())

            baseline = ctx.baseline(service, instance, numerator)
            base_bottom = ctx.baseline(service, instance, denominator)
            reference = self._reference_ratio(baseline, base_bottom)
            if reference is None or reference <= 0:
                continue
            rise_pct = (observed_ratio - reference) / reference * 100.0

            facts = self._facts(
                ctx, service, instance, numerator, ratios, observed_ratio, reference, rise_pct
            )
            matched = match_level_series(self.levels, facts.facts)
            if matched is None:
                continue
            amplitude = self._amplitude(facts, matched)
            findings.append(
                self.make_finding(
                    ctx,
                    facts,
                    matched,
                    amplitude=amplitude,
                    duration_minutes=facts.facts["_window_minutes"],
                    extra_facts={
                        "path": f"{numerator.label} / {denominator.label}",
                    },
                    extra_evidence={
                        "numerator": numerator.value,
                        "denominator": denominator.value,
                        "p99_ms": round(top.worst(), 2),
                        "p50_ms": round(bottom.percentile(50), 2),
                        "baseline_ratio": round(reference, 3),
                    },
                    metric_label=f"{numerator.label}/{denominator.label}",
                    unit="倍",
                )
            )
        return findings

    @staticmethod
    def _reference_ratio(baseline: Baseline, base_bottom: Baseline) -> float | None:
        if baseline.mode is not BaselineMode.DYNAMIC or base_bottom.mode is not BaselineMode.DYNAMIC:
            return None
        if not base_bottom.median or base_bottom.median <= 0:
            return None
        return baseline.median / base_bottom.median

    def _facts(self, ctx, service, instance, metric, ratios, observed, reference, rise_pct) -> SeriesFacts:  # noqa: ANN001
        baseline = ctx.baseline(service, instance, metric)
        epochs = sorted(ratios)
        series = MetricSeries(
            service=service,
            instance=instance,
            metric=metric,
            points=[(from_epoch(e), ratios[e]) for e in epochs],
            unit="倍",
        )
        facts_dict = {
            "observed": observed,
            "value": observed,
            "baseline": reference,
            "ratio": observed,
            "ratio_rise_pct": rise_pct,
            "deviation_pct": rise_pct,
            "z_score": baseline.z_score(observed, ctx.window.end.hour) if baseline.mad_scaled else 0.0,
            "scale": baseline.mad_scaled,
            "threshold": reference,
            "_series_values": [ratios[e] for e in epochs],
            "_step_minutes": series_step_minutes(series),
            "_window_minutes": ctx.window.duration_minutes,
        }
        return SeriesFacts(
            service=service,
            instance=instance,
            metric=metric,
            series=series,
            baseline=baseline,
            observed=observed,
            baseline_value=reference,
            deviation_pct=rise_pct,
            z_score=facts_dict["z_score"],
            duration_minutes=ctx.window.duration_minutes,
            facts=facts_dict,
        )

    def _amplitude(self, facts: SeriesFacts, matched: MatchedLevel) -> float:
        candidates = []
        for key in ("ratio", "ratio_rise_pct"):
            warn, critical = bounds_for_key(self.levels, key)
            if warn is None:
                continue
            candidates.append(amplitude_from_value(abs(float(facts.facts.get(key, 0))), warn, critical or warn))
        if facts.has_dynamic_baseline:
            candidates.append(amplitude_from_z(facts.z_score))
        return max(candidates) if candidates else 0.3


class TrendRule(Rule):
    """PERF-05 / RES-03：趋势型规则。

    两道闸门缺一不可：
      1. **幅度闸门**：每小时变化百分比要够大；
      2. **显著性闸门**：斜率的 t 统计量必须够大（默认 3.0）。

    只有幅度闸门的话，一段噪声在 30 个点上随手一拟合就能「涨 15%/小时」，
    趋势规则会在干净数据上稳定误报——这是动态阈值落地最容易翻车的地方。
    """

    def evaluate(self, ctx: EvalContext) -> list[AnomalyFinding]:
        metric = self.metric_param()
        lookback_hours = float(self.params.get("lookback_hours", ctx.window.duration_minutes / 60.0))
        min_samples = int(self.params.get("min_samples", 10))
        min_t = float(self.params.get("min_t_stat", 3.0))
        min_r2 = float(self.params.get("min_r2", 0.0))
        window = ctx.lookback_window(lookback_hours)

        findings: list[AnomalyFinding] = []
        for service, instance in ctx.instances():
            series = ctx.series(service, instance, metric, window=window)
            if len(series.points) < min_samples:
                continue
            slope, t_stat, r2 = series.trend_stats()
            if abs(t_stat) < min_t or r2 < min_r2:
                continue
            slope_pct = series.slope_pct_per_hour()
            if abs(slope_pct) < _smallest_threshold(self.levels, "slope_pct_per_hour"):
                continue

            latest = series.latest()
            observed = latest[1] if latest else float("nan")
            reference = statistics.median(series.values())
            facts = SeriesFacts(
                service=service,
                instance=instance,
                metric=metric,
                series=series,
                baseline=ctx.baseline(service, instance, metric),
                observed=observed,
                baseline_value=reference,
                deviation_pct=(observed - reference) / abs(reference) * 100.0 if reference else 0.0,
                z_score=0.0,
                duration_minutes=window.duration_minutes,
                facts={
                    "observed": observed,
                    "value": observed,
                    "baseline": reference,
                    "slope_pct_per_hour": abs(slope_pct),
                    "deviation_pct": (observed - reference) / abs(reference) * 100.0 if reference else 0.0,
                    "z_score": 0.0,
                    "scale": 0.0,
                    "threshold": reference,
                    "trend_t_stat": round(t_stat, 2),
                    "trend_r2": round(r2, 4),
                    "samples": len(series.points),
                    "forecast": self._forecast(observed, slope_pct, metric),
                    "_series_values": series.values(),
                    "_step_minutes": series_step_minutes(series),
                    "_window_minutes": window.duration_minutes,
                },
            )
            matched = match_level_series(self.levels, facts.facts)
            if matched is None:
                continue
            warn, critical = bounds_for_key(self.levels, "slope_pct_per_hour")
            amplitude = amplitude_from_value(abs(slope_pct), warn or 0.0, critical or (warn or 1.0))
            findings.append(
                self.make_finding(
                    ctx,
                    facts,
                    matched,
                    amplitude=amplitude,
                    duration_minutes=window.duration_minutes,
                    extra_evidence={
                        "trend_t_stat": round(t_stat, 2),
                        "trend_r2": round(r2, 4),
                        "slope_per_hour": round(slope * 60.0, 4),
                        "observation_hours": round(window.duration_minutes / 60.0, 2),
                        "samples": len(series.points),
                    },
                )
            )
        return findings

    @staticmethod
    def _forecast(observed: float, slope_pct_per_hour: float, metric: MetricName) -> str:
        """把趋势外推成一句人话——「当前风险」升级为「未来必然发生的故障」。"""
        if slope_pct_per_hour <= 0:
            return "按当前趋势继续下行，需关注是否为流量衰减"
        limit = 95.0 if metric in (MetricName.MEM_USAGE, MetricName.CPU_USAGE, MetricName.CONN_USAGE) else None
        if limit is None or observed >= limit:
            return ""
        hours = (limit - observed) / (observed * slope_pct_per_hour / 100.0) if observed else 0.0
        if hours <= 0 or hours > 240:
            return ""
        return f"按当前斜率外推，约 {hours:.1f} 小时后触及 {limit:.0f}% 危险水位"


def _align_ratio(numerator: MetricSeries, denominator: MetricSeries) -> dict[int, float]:
    """按 epoch 对齐两条序列后逐点求比值。时间轴对不齐就不硬凑——
    宁可少判，也不能把两个不同时刻的数字相除。"""
    bottom = {to_epoch(ts): value for ts, value in denominator.points if value > 0}
    ratios: dict[int, float] = {}
    for ts, value in numerator.points:
        epoch = to_epoch(ts)
        if epoch in bottom:
            ratios[epoch] = value / bottom[epoch]
    return ratios


def _smallest_threshold(levels, key: str) -> float:  # noqa: ANN001
    """配置里最宽松的阈值。低于它连最轻微的级别都够不上，直接跳过。"""
    warn, _ = bounds_for_key(levels, key)
    return warn if warn is not None else 0.0


__all__ = ["LongTailRule", "TrendRule"]
