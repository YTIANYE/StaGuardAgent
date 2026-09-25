"""异常分级与严重度归一化。

两条正交的度量，刻意分开：
- **level（紧急/严重/一般/轻微）** 是「策略」：由 YAML 里的阈值条件决定，可被人调整；
- **deviation_score（0~1）** 是「排序权重」：由算法算出，用于让不同量纲的异常可比。

为什么必须分开：成功率的「跌 5 个百分点」和 P99 的「涨到 3000ms」根本没有共同量纲，
但报告需要把它们排在一张表里、评分需要按严重度加权。把它们统一映射到 0~1 的分数，
是让「多维度巡检结果可聚合」的前提。

deviation_score 的构成（权重体现的是 SRE 的经验判断）：

    0.55 x 幅度      异常偏离得有多离谱（最主导）
    0.20 x 持续时长  持续 20 分钟和持续 1 分钟不是一回事
    0.15 x 影响面    同服务多少个实例一起中招
    0.10 x 服务关键度 核心链路出问题更严重
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import LevelSpec
from ..models import Severity

# 分级条件中「取绝对值比较」的键：下跌和上涨都是异常，方向由规则自己保证
ABS_CONDITIONS = frozenset({"deviation_pct", "z_score", "down_pct", "slope_pct_per_hour"})

# 纯布尔条件
BOOL_CONDITIONS = frozenset({"below_slo"})

# 需要 facts 直接比较的数值条件
VALUE_CONDITIONS = frozenset({
    "value", "ratio", "cv", "multiplier", "drop_points", "rise_points",
    "ratio_rise_pct", "min_absolute", "duration_minutes", "instances", "health_below",
})

ALL_CONDITIONS = ABS_CONDITIONS | BOOL_CONDITIONS | VALUE_CONDITIONS

SCORE_WEIGHTS = {"amplitude": 0.55, "duration": 0.20, "blast": 0.15, "criticality": 0.10}
DURATION_SATURATION_MINUTES = 15.0
"""持续 15 分钟以上就视为「持续故障」，再长也不额外加权——否则一次长时间故障会
把分数吃满，掩盖掉其它同时发生的更严重问题。"""


class UnknownCondition(ValueError):
    """配置里出现了规则无法解释的条件键。启动期校验会提前抓住它，而不是等线上。"""


@dataclass(slots=True)
class MatchedLevel:
    level: Severity
    reason: str
    conditions: dict[str, Any]


def condition_satisfied(key: str, threshold: Any, facts: dict[str, Any]) -> bool:
    """判断单个条件是否满足。未知条件键按「不满足」处理并已在校验期被拦截。"""
    if key in BOOL_CONDITIONS:
        return bool(facts.get(key, False)) == bool(threshold)
    actual = facts.get(key)
    if actual is None:
        return False
    try:
        actual_value = float(actual)
        target = float(threshold)
    except (TypeError, ValueError):
        return False
    if key in ABS_CONDITIONS:
        return abs(actual_value) >= abs(target)
    if key == "health_below":
        return actual_value <= target
    return actual_value >= target


def match_level(levels: list[LevelSpec], facts: dict[str, Any]) -> MatchedLevel | None:
    """按配置顺序匹配级别，首个全部条件满足者生效。

    配置顺序即优先级（YAML 里按 P1 -> P4 排列），这比「取最严重」更可控：
    运维同学一眼就能看出哪条规则先命中，而不必去推演所有条件的组合。
    """
    for spec in levels:
        if not spec.when:
            continue
        if all(condition_satisfied(key, value, facts) for key, value in spec.when.items()):
            return MatchedLevel(level=spec.level, reason=spec.reason, conditions=dict(spec.when))
    return None


POINTWISE_CONDITIONS = frozenset({
    "value", "deviation_pct", "z_score", "drop_points", "rise_points", "down_pct", "multiplier",
})
"""可以逐点求值的条件。其余条件（cv、ratio、slope、instances、min_absolute…）是窗口聚合量，
无法逐点判断，它们的「持续时长」只能按整个巡检窗口计。"""


def point_predicate(key: str, threshold: Any, facts: dict[str, Any]):  # noqa: ANN201
    """构造「单点是否满足该条件」的谓词；聚合类条件返回 None。

    有了它，「持续 3 分钟以上」就不再是一个拍脑袋的布尔量，而是**真正沿时间轴数出来的**：
    这 30 个采样点里，连续满足该级别条件的点有几个。
    """
    try:
        target = float(threshold)
    except (TypeError, ValueError):
        return None
    baseline = _as_float(facts.get("baseline"), 0.0)

    if key == "value":
        return lambda v: v >= target
    if key == "multiplier":
        return (lambda v: v / baseline >= target) if baseline else None
    if key == "drop_points":
        return lambda v: baseline - v >= target
    if key == "rise_points":
        return lambda v: v - baseline >= target
    if key == "down_pct":
        return (lambda v: (baseline - v) / abs(baseline) * 100.0 >= target) if baseline else None
    if key == "deviation_pct":
        return (lambda v: abs(v - baseline) / abs(baseline) * 100.0 >= abs(target)) if baseline else None
    if key == "z_score":
        scale = _as_float(facts.get("scale"), 0.0)
        return (lambda v: abs(v - baseline) / scale >= abs(target)) if scale else None
    if key == "below_slo":
        slo = _as_float(facts.get("slo"), 0.0)
        if not slo:
            return None
        # 方向由指标语义决定：成功率跌破 SLO，业务错误率则是冲破 SLO
        if facts.get("_higher_is_better", True):
            return lambda v: v < slo
        return lambda v: v > slo
    return None


def longest_run(values: list[float], predicates: list, step_minutes: float) -> tuple[float, int]:
    """最长连续满足（所有谓词同时成立）的时长与起始下标。

    返回起始下标是为了拿到**异常真正的起始时刻**：报告里说「从 09:38 开始」
    比说「持续了 20 分钟」有用得多，它还能直接和变更时间对齐做归因。
    """
    if not predicates or not values:
        return 0.0, -1
    best = current = 0
    best_start = current_start = -1
    for index, value in enumerate(values):
        if all(predicate(value) for predicate in predicates):
            if current == 0:
                current_start = index
            current += 1
            if current > best:
                best = current
                best_start = current_start
        else:
            current = 0
    return best * step_minutes, best_start


def longest_run_minutes(values: list[float], predicates: list, step_minutes: float) -> float:
    return longest_run(values, predicates, step_minutes)[0]


def match_level_series(levels: list[LevelSpec], facts: dict[str, Any]) -> MatchedLevel | None:
    """带「持续时长」精确匹配的分级。

    与 `match_level` 的区别：`duration_minutes` 条件不是拿一个预先算好的值去比，
    而是针对**当前级别自己的条件**沿时间轴数出连续满足的时长再比。

    这么做的意义：P1 要求「跌幅超 5 个百分点且持续 3 分钟」，
    P2 只要求「跌幅超 2 个百分点且持续 2 分钟」。如果用一个全局 duration，
    1 分钟的极端下跌会被误判成 P1——它其实只是瞬时毛刺。
    """
    series: list[float] = facts.get("_series_values") or []
    step_minutes = _as_float(facts.get("_step_minutes"), 1.0)
    window_minutes = _as_float(facts.get("_window_minutes"), step_minutes * max(1, len(series)))

    for spec in levels:
        if not spec.when:
            continue
        aggregate_ok = True
        predicates: list = []
        for key, value in spec.when.items():
            if key == "duration_minutes":
                continue
            if not condition_satisfied(key, value, facts):
                aggregate_ok = False
                break
            if key in POINTWISE_CONDITIONS:
                predicate = point_predicate(key, value, facts)
                if predicate is not None:
                    predicates.append(predicate)
        if not aggregate_ok:
            continue

        required = _as_float(spec.when.get("duration_minutes"), 0.0)
        duration = (
            longest_run_minutes(series, predicates, step_minutes) if predicates else window_minutes
        )
        if duration + 1e-9 >= required:
            return MatchedLevel(level=spec.level, reason=spec.reason, conditions=dict(spec.when))
    return None


def condition_for_key(levels: list[LevelSpec], key: str, level: Severity) -> Any:
    """取某个级别下某个条件的阈值，用于在 reason 里展示「红线是多少」。"""
    for spec in levels:
        if spec.level is level and key in spec.when:
            return spec.when[key]
    return None


def compute_deviation_score(
    amplitude: float,
    duration_minutes: float,
    affected_ratio: float,
    criticality: float,
) -> float:
    """把四类事实归一化成一个 0~1 的严重度分数。"""
    duration_factor = min(1.0, max(0.0, duration_minutes) / DURATION_SATURATION_MINUTES)
    score = (
        SCORE_WEIGHTS["amplitude"] * _clamp01(amplitude)
        + SCORE_WEIGHTS["duration"] * duration_factor
        + SCORE_WEIGHTS["blast"] * _clamp01(affected_ratio)
        + SCORE_WEIGHTS["criticality"] * _clamp01(criticality)
    )
    return round(_clamp01(score), 4)


def amplitude_from_value(observed: float, warn: float, critical: float) -> float:
    """按「预警线 -> 红线」区间把观测值映射到 0~1。

    低于预警线记 0，达到红线记 1，这样「刚刚越线」和「远超红线」在排序上能拉开差距。

    `warn == critical` 是常见情况（某个条件键在配置里只出现一次，例如只有 P4 用了
    `z_score`）。此时不能简单地「一越线就记满 1.0」——那会让最轻微的那一档
    拿到最高幅度分。改为以阈值本身为尺度：触线记 0.2，达到阈值的 2 倍记满。
    """
    if critical <= warn:
        if observed < critical or critical <= 0:
            return 0.0
        return _clamp01(0.2 + 0.8 * min(1.0, (observed - critical) / abs(critical)))
    ratio = (observed - warn) / (critical - warn)
    return _clamp01(max(0.0, ratio))


def amplitude_from_z(z_score: float, saturation: float = 12.0) -> float:
    """按稳健 Z 分数映射幅度。3σ 以下基本无感，12σ 以上视为完全爆表。"""
    return _clamp01((abs(z_score) - 3.0) / max(1.0, saturation - 3.0))


def scale_levels(levels: list[LevelSpec], scale: float, keys: tuple[str, ...] = ("value",)) -> list[LevelSpec]:
    """按服务自己的阈值配置缩放分级条件。

    解决的问题：`config/thresholds.yaml` 允许为单个服务覆盖红线（网关的 P99 红线
    比默认值严、银行渠道的比默认值松），但规则里的分级阈值如果写死绝对值，
    这些覆盖就形同虚设——规则会拿网关的标准去要求银行渠道。

    做法：规则里的数字按「默认服务的尺度」写，运行时按 `服务红线 / 默认红线` 等比缩放。
    于是「网关 P99 超 500ms 算异常」和「银行渠道 P99 超 1500ms 才算异常」用同一段配置
    就能同时成立，而规则本身一行都不用改。
    """
    if abs(scale - 1.0) < 1e-9:
        return levels
    scaled: list[LevelSpec] = []
    for spec in levels:
        when = dict(spec.when)
        for key in keys:
            if key in when:
                when[key] = round(float(when[key]) * scale, 4)
        scaled.append(LevelSpec(level=spec.level, when=when, reason=spec.reason))
    return scaled


LEVEL_AMPLITUDE_BAND: dict[Severity, tuple[float, float]] = {
    Severity.P1: (0.75, 1.00),
    Severity.P2: (0.55, 0.84),
    Severity.P3: (0.35, 0.64),
    Severity.P4: (0.15, 0.40),
}
"""级别到幅度区间的映射 (下限, 上限)。

**级别是策略，幅度是排序**——这条边界必须划清楚，否则两者会互相打架。
典型冲突：CPU 从 47% 涨到 80%，绝对水位没到 85% 的红线所以只判 P4，
但相对基线涨了 70%、z 分数几十，若按纯幅度算会得到满分 1.0，
于是一个 P4 的排序权重压过了 P1。

解决办法是把原始幅度**压进该级别的区间内**：级别决定量级，幅度只在这个量级里
决定谁排前面。这样「刚好踩线的 P1」永远排在「爆表的 P4」前面，
与人工分级时的直觉一致。
"""


def banded_amplitude(raw: float, level: Severity) -> float:
    """把 [0,1] 的原始幅度压进该级别的区间内。"""
    low, high = LEVEL_AMPLITUDE_BAND[level]
    return round(low + (high - low) * _clamp01(raw), 4)


def bounds_for_key(levels: list[LevelSpec], key: str) -> tuple[float | None, float | None]:
    """从分级配置里推出某个条件键的 (预警线, 红线)。

    所有条件键的语义都是「越大越严重」，所以直接把配置里的阈值取 min/max 即可。
    有了这两个端点，任意量纲的观测值都能被映射到同一个 0~1 幅度区间——
    这是「多维度异常可比」的实现方式，不依赖任何硬编码的指标换算表。
    """
    values = [float(spec.when[key]) for spec in levels if key in spec.when]
    if not values:
        return None, None
    return min(values), max(values)


def build_reason_context(
    facts: dict[str, Any],
    *,
    metric_label: str = "",
    unit: str = "",
    service: str = "",
    instance: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, str]:
    """构造原因模板的渲染上下文。

    所有数值在这里就格式化好，模板里只做字符串拼接——
    这样「报告里出现 12345.6789ms」这类展示问题不会散落到每条规则里。
    """
    from ..utils.text import fmt, metric_value

    observed = facts.get("observed", facts.get("value"))
    baseline = facts.get("baseline")
    context: dict[str, str] = {
        "service": service,
        "instance": instance or service,
        "metric": metric_label,
        "observed": metric_value(_as_float(observed), metric_label, unit),
        "baseline": metric_value(_as_float(baseline), metric_label, unit),
        "observed_raw": fmt(_as_float(observed), 2),
        "baseline_raw": fmt(_as_float(baseline), 2),
        "deviation": fmt(abs(_as_float(facts.get("deviation_pct"), 0.0)), 2),
        "deviation_signed": fmt(_as_float(facts.get("deviation_pct"), 0.0), 2),
        # 百分点口径。比率型指标（成功率、业务错误率）要说「跌了 8 个百分点」，
        # 而不是「跌了 8%」——后者会被读成相对跌幅，含义完全不同。
        "drop": fmt(_as_float(facts.get("drop_points"), 0.0), 2),
        "rise": fmt(_as_float(facts.get("rise_points"), 0.0), 2),
        "z": fmt(abs(_as_float(facts.get("z_score"), 0.0)), 1),
        "ratio": fmt(_as_float(facts.get("ratio"), 1.0), 2),
        "multiplier": fmt(_as_float(facts.get("multiplier"), 1.0), 2),
        "samples": f"{_as_float(facts.get('samples'), 0.0):.0f}",
        "cv": fmt(_as_float(facts.get("cv"), 0.0), 2),
        "slope": fmt(abs(_as_float(facts.get("slope_pct_per_hour"), 0.0)), 1),
        "duration": f"{_as_float(facts.get('duration_minutes'), 0.0):.0f}",
        "threshold": fmt(_as_float(facts.get("threshold"), 0.0), 0),
        "slo": metric_value(_as_float(facts.get("slo"), 0.0), metric_label, unit),
        "path": str(facts.get("path", "")),
        "forecast": str(facts.get("forecast", "")),
        "instances": f"{_as_float(facts.get('instances'), 1.0):.0f}",
    }
    if extra:
        context.update({k: str(v) for k, v in extra.items()})
    return context


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result else default  # NaN 兜底
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def validate_conditions(levels: list[LevelSpec], rule_id: str) -> list[str]:
    """启动期校验：拦截配置里写错的条件键。

    这类错误如果等到运行时才发现，表现是「规则静默不报警」——最难排查的一种故障。
    所以宁可启动时就报错。
    """
    problems: list[str] = []
    for spec in levels:
        for key in spec.when:
            if key not in ALL_CONDITIONS:
                problems.append(
                    f"规则 {rule_id} 的 {spec.level.code} 级别使用了未知条件键 `{key}`，"
                    f"支持的条件：{sorted(ALL_CONDITIONS)}"
                )
    return problems
