"""规则引擎测试：给一段时序，断言判定结果。

这一组测试是整套代码里最有价值的部分——规则引擎是最容易在修改阈值、
调整算法时被悄悄改坏的地方，而它坏掉的表现是「不再报警」，
不会抛异常、不会报错，只有真正出事那天才会发现。
"""

from __future__ import annotations

import random

import pytest

from staguard.config import Settings
from staguard.models import BaselineMode, MetricName, MetricSeries, Severity
from staguard.rules import RuleEngine, build_context  # noqa: F401  (保持导出可用性)
from staguard.rules.engine import build_rules

from .conftest import FakeContext, make_baseline, make_series


def _engine(settings: Settings, rule_id: str):
    config = settings.rules.get(rule_id)
    assert config is not None, f"规则 {rule_id} 未配置"
    return next(rule for rule in build_rules(settings) if rule.id == rule_id)


def _context(settings: Settings, series_map, baseline_map=None, changes=None) -> FakeContext:
    return FakeContext(settings, series_map, baseline_map, changes)


def test_success_rate_drop_escalates_to_p1(settings: Settings):
    """跌 8 个百分点并持续 10 分钟 —— 这是事故，不是抖动。"""
    values = [99.97] * 10 + [92.0] * 10 + [99.97] * 10
    series = make_series(values, MetricName.SUCCESS_RATE)
    ctx = _context(
        settings,
        {("order-svc", "order-svc-0", MetricName.SUCCESS_RATE): series},
        {("order-svc", "order-svc-0", MetricName.SUCCESS_RATE): make_baseline(99.97, mad=0.01)},
    )

    findings = _engine(settings, "BIZ-01").evaluate(ctx)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.level is Severity.P1
    assert finding.duration_minutes >= 3
    assert "下跌" in finding.level_reason


def test_transient_dip_does_not_escalate(settings: Settings):
    """只掉 1 分钟的极端值不应该被判成 P1。

    这是「持续时长」这道闸门的核心价值：用同一个 duration 去匹配所有级别，
    瞬时毛刺就会被误判成重大故障。
    """
    values = [99.97] * 14 + [90.0] + [99.97] * 15
    series = make_series(values, MetricName.SUCCESS_RATE)
    ctx = _context(
        settings,
        {("order-svc", "order-svc-0", MetricName.SUCCESS_RATE): series},
        {("order-svc", "order-svc-0", MetricName.SUCCESS_RATE): make_baseline(99.97, mad=0.01)},
    )

    findings = _engine(settings, "BIZ-01").evaluate(ctx)
    levels = [f.level for f in findings]
    assert Severity.P1 not in levels and Severity.P2 not in levels, (
        "1 分钟的瞬时下跌不应触发紧急/严重级别"
    )


def test_traffic_drop_uses_minimum_not_maximum(settings: Settings):
    """突降规则必须看窗口内的最小值。

    QPS 是双向指标：突增要看最大值，突降要看最小值。
    如果两个方向都用同一个「最坏值」（定义成最大值），突降规则会**永远不触发**——
    而且不报错、不告警，只是静默失效。
    """
    values = [1000.0] * 8 + [250.0] * 16 + [1000.0] * 6
    series = make_series(values, MetricName.QPS)
    ctx = _context(
        settings,
        {("gateway", "gateway-0", MetricName.QPS): series},
        {("gateway", "gateway-0", MetricName.QPS): make_baseline(1000.0, mad=15.0, metric=MetricName.QPS)},
    )

    findings = _engine(settings, "BIZ-04").evaluate(ctx)
    assert findings, "QPS 下跌 75% 必须被识别"
    assert findings[0].level is Severity.P2
    assert "下跌" in findings[0].level_reason


def test_traffic_drop_ignores_clean_traffic(settings: Settings):
    values = [1000.0 + (i % 7) for i in range(30)]
    series = make_series(values, MetricName.QPS)
    ctx = _context(
        settings,
        {("gateway", "gateway-0", MetricName.QPS): series},
        {("gateway", "gateway-0", MetricName.QPS): make_baseline(1000.0, mad=2.0, metric=MetricName.QPS)},
    )
    assert not _engine(settings, "BIZ-04").evaluate(ctx)


def test_trend_rule_requires_statistical_significance(settings: Settings):
    """趋势规则必须带显著性检验。

    造一段纯噪声：它的最小二乘斜率可能「看起来」有百分之几的涨幅，
    但 t 统计量很低，说明完全可能是噪声。没有这道闸门，
    趋势规则会在干净数据上稳定误报——这是动态阈值落地最容易翻车的地方。
    """
    rng = random.Random(20260925)
    flat_noise = [500.0 + rng.gauss(0, 25) for _ in range(30)]
    series = make_series(flat_noise, MetricName.LATENCY_P99)
    ctx = _context(
        settings,
        {("order-svc", "order-svc-0", MetricName.LATENCY_P99): series},
        {("order-svc", "order-svc-0", MetricName.LATENCY_P99): make_baseline(500.0, mad=20.0, metric=MetricName.LATENCY_P99)},
    )
    assert not _engine(settings, "PERF-05").evaluate(ctx), "纯噪声不应触发性能劣化趋势"


def test_trend_rule_detects_real_degradation(settings: Settings):
    """真实的单调劣化必须被抓住——这是唯一能提前发现慢故障的规则。"""
    rng = random.Random(7)
    degrading = [500.0 + i * 12 + rng.gauss(0, 8) for i in range(60)]
    window_minutes = 60
    series = make_series(degrading, MetricName.LATENCY_P99, step_seconds=60)
    series = MetricSeries(
        service="order-svc", instance="order-svc-0", metric=MetricName.LATENCY_P99,
        points=[(ts, v) for ts, v in series.points], unit="ms",
    )
    ctx = _context(
        settings,
        {("order-svc", "order-svc-0", MetricName.LATENCY_P99): series},
        {("order-svc", "order-svc-0", MetricName.LATENCY_P99): make_baseline(700.0, mad=40.0, metric=MetricName.LATENCY_P99)},
    )
    findings = _engine(settings, "PERF-05").evaluate(ctx)
    assert findings, f"持续劣化必须被识别（窗口 {window_minutes} 分钟）"
    assert findings[0].evidence["trend_t_stat"] > 3.0


def test_memory_leak_produces_oom_forecast(settings: Settings):
    """内存泄漏要给出 OOM 时间外推，而不只是「内存偏高」。"""
    leak = [40.0 + i * (54 / 60) for i in range(61)]
    series = make_series(leak, MetricName.MEM_USAGE, step_seconds=60)
    ctx = _context(
        settings,
        {("payment-svc", "payment-svc-0", MetricName.MEM_USAGE): series},
        {("payment-svc", "payment-svc-0", MetricName.MEM_USAGE): make_baseline(60.0, mad=2.0, metric=MetricName.MEM_USAGE)},
    )
    findings = _engine(settings, "RES-03").evaluate(ctx)
    assert findings
    assert "小时后触及" in findings[0].level_reason or "增长" in findings[0].level_reason


def test_instance_skew_distinguishes_single_instance(settings: Settings):
    """同服务 3 个实例，只有 1 个 CPU 打满 —— 必须能识别出是单实例问题。

    「全体一起高」是容量问题，「只有一个高」是坏节点问题，处置动作完全相反。
    分不清这两者，给出的建议就只能是「建议扩容」这种没法执行的废话。
    """
    cpu = MetricName.CPU_USAGE
    healthy = ["inventory-svc-0", "inventory-svc-2"]
    series_map = {
        ("inventory-svc", inst, cpu): make_series([40.0] * 30, cpu, "inventory-svc", inst)
        for inst in healthy
    }
    series_map[("inventory-svc", "inventory-svc-1", cpu)] = make_series(
        [96.0] * 30, cpu, "inventory-svc", "inventory-svc-1"
    )
    baselines = {
        (svc, inst, cpu): make_baseline(40.0, mad=2.0, metric=cpu, service=svc, instance=inst)
        for svc, inst, _ in (
            ("inventory-svc", "inventory-svc-0", cpu),
            ("inventory-svc", "inventory-svc-1", cpu),
            ("inventory-svc", "inventory-svc-2", cpu),
        )
    }
    ctx = _context(settings, series_map, baselines)
    findings = _engine(settings, "RES-05").evaluate(ctx)
    assert findings
    assert findings[0].instance == "inventory-svc-1"
    assert "中位数" in findings[0].level_reason


def test_thresholds_scale_with_service_slo(settings: Settings):
    """分级阈值必须跟着服务的红线缩放。

    网关的 P99 红线是 500ms、银行渠道是 1500ms。如果规则里写死 800ms，
    网关会被系统性误报，银行渠道会被系统性漏报。
    """
    rule = _engine(settings, "PERF-01")
    gateway_levels = rule.levels_for(_context(settings, {}), MetricName.LATENCY_P99, "gateway")
    bank_levels = rule.levels_for(_context(settings, {}), MetricName.LATENCY_P99, "bank-channel")

    gateway_p3 = next(spec.when["value"] for spec in gateway_levels if spec.level is Severity.P3)
    bank_p3 = next(spec.when["value"] for spec in bank_levels if spec.level is Severity.P3)

    assert gateway_p3 < 800 < bank_p3
    assert gateway_p3 == pytest.approx(500, rel=0.01)
    assert bank_p3 == pytest.approx(1500, rel=0.01)


def test_static_fallback_still_reports_absolute_breach(settings: Settings):
    """冷启动期没有历史样本，也不能不巡检 —— 靠静态红线兜底。"""
    metric = MetricName.LATENCY_P99
    series = make_series([2500.0] * 10, metric)
    baseline = make_baseline(0.0, metric=metric, mode=BaselineMode.STATIC_FALLBACK, sample_size=3)
    ctx = _context(settings, {("order-svc", "order-svc-0", metric): series}, {("order-svc", "order-svc-0", metric): baseline})

    findings = _engine(settings, "PERF-01").evaluate(ctx)
    assert findings
    assert findings[0].baseline_mode is BaselineMode.STATIC_FALLBACK


def test_short_series_is_rejected(settings: Settings):
    """样本太少时不下结论。

    只有 2 个点的窗口算出来的「持续 2 分钟」毫无统计意义。
    规则宁可漏，也不能在数据不足时给出看起来很确定的结论。
    """
    metric = MetricName.SUCCESS_RATE
    series = make_series([99.0, 95.0], metric)
    ctx = _context(settings, {("order-svc", "order-svc-0", metric): series}, {("order-svc", "order-svc-0", metric): make_baseline(99.97, mad=0.01)})
    assert not _engine(settings, "BIZ-01").evaluate(ctx)


def test_rule_engine_isolates_failures(settings: Settings):
    """单条规则抛异常不能让整轮巡检失败。

    巡检的价值在于「尽可能多地把问题找出来」，一条规则的 bug 不应该让整次巡检白跑。
    """
    engine = RuleEngine(settings)
    broken = object()
    engine.rules.insert(0, broken)  # type: ignore[arg-type]
    ctx = _context(settings, {})
    findings = engine.evaluate(ctx)
    assert findings == []
    assert engine.failures, "规则失败必须被记录，而不是被静默吞掉"
