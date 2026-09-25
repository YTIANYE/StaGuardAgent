"""规则引擎：注册表、启动期校验、批量求值。

规则 ID 到实现的映射是**显式的**：默认走通用阈值规则，只有真正有独立逻辑的
（链路健康度、长尾比值、趋势、实例不均衡）才登记自己的实现。
好处是「新增一条阈值型规则」不需要动这个文件——
在 `config/rules.yaml` 里加一段配置就完事了，这正是「策略可配、代码稳定」的体现。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import ConfigError, RuleConfig, Settings
from ..models import AnomalyFinding, Dimension, MetricName
from .base import Rule
from .context import EvalContext
from .generic import GenericThresholdRule
from .leveling import validate_conditions
from .link import ChainHealthRule
from .performance import LongTailRule, TrendRule
from .resource import InstanceSkewRule

logger = logging.getLogger(__name__)

RULE_IMPLEMENTATIONS: dict[str, type[Rule]] = {
    "BIZ-06": ChainHealthRule,
    "PERF-04": LongTailRule,
    "PERF-05": TrendRule,
    "RES-03": TrendRule,
    "RES-05": InstanceSkewRule,
}
"""有独立算法的规则。其余规则一律由 GenericThresholdRule 承载。"""

DEFAULT_RULE_CLASS: type[Rule] = GenericThresholdRule


def build_rules(settings: Settings) -> list[Rule]:
    """按配置装配规则实例，并做启动期校验。"""
    rules: list[Rule] = []
    problems: list[str] = []
    for config in settings.rules.enabled():
        problems.extend(validate_conditions(config.levels, config.id))
        problems.extend(_validate_params(config))
        if not config.levels:
            problems.append(f"规则 {config.id} 没有任何分级条件，永远不会命中")
        rules.append(_instantiate(config))
    if problems:
        # 配置错误必须启动即失败：否则表现是「规则静默不报警」，线上几乎无法定位
        raise ConfigError("规则配置校验失败：\n  - " + "\n  - ".join(problems))
    return rules


def _instantiate(config: RuleConfig) -> Rule:
    rule_class = RULE_IMPLEMENTATIONS.get(config.id, DEFAULT_RULE_CLASS)
    return rule_class(config)


def _validate_params(config: RuleConfig) -> list[str]:
    problems: list[str] = []
    for key in ("metric", "health_metric", "numerator", "denominator"):
        raw = config.params.get(key)
        if raw is None:
            continue
        try:
            MetricName(raw)
        except ValueError:
            problems.append(f"规则 {config.id} 的参数 `{key}={raw}` 不是已知指标")
    for key in ("metrics",):
        for raw in config.params.get(key, []) or []:
            try:
                MetricName(raw)
            except ValueError:
                problems.append(f"规则 {config.id} 的参数 `{key}` 含未知指标 `{raw}`")
    return problems


class RuleEngine:
    def __init__(self, settings: Settings, rules: list[Rule] | None = None) -> None:
        self.settings = settings
        self.rules: list[Rule] = rules if rules is not None else build_rules(settings)
        self.last_elapsed_ms = 0
        self.failures: list[str] = []

    def rule_ids(self) -> list[str]:
        return [rule.id for rule in self.rules]

    def rules_by_dimension(self) -> dict[Dimension, list[Rule]]:
        grouped: dict[Dimension, list[Rule]] = {}
        for rule in self.rules:
            grouped.setdefault(rule.dimension, []).append(rule)
        return grouped

    def evaluate(self, ctx: EvalContext) -> list[AnomalyFinding]:
        """顺序执行所有规则。

        **单条规则失败不影响其它规则**：巡检的价值在于「尽可能多地把问题找出来」，
        一条规则的 bug 不应该让整次巡检白跑。失败被记录并在报告里显式暴露，
        而不是被静默吞掉——静默失败比失败本身更危险。
        """
        started = time.perf_counter()
        self.failures = []
        findings: list[AnomalyFinding] = []
        for rule in self.rules:
            rule_started = time.perf_counter()
            try:
                produced = rule.evaluate(ctx)
            except Exception as exc:  # noqa: BLE001 - 规则隔离是刻意设计
                # 用 getattr 兜底：规则对象本身可能有问题（装配出错、属性缺失），
                # 而**记录失败这一步绝不能再抛异常**——否则一条坏规则会连带
                # 干掉落后的所有规则，隔离设计就白做了。
                rule_id = getattr(rule, "id", repr(rule))
                rule_name = getattr(rule, "name", "unknown")
                self.failures.append(f"{rule_id} {rule_name}: {exc.__class__.__name__}: {exc}")
                logger.exception("规则 %s 执行失败，已跳过", rule_id)
                continue
            if produced:
                findings.extend(produced)
            logger.debug(
                "规则 %s 命中 %d 条（%.0fms）",
                rule.id, len(produced), (time.perf_counter() - rule_started) * 1000,
            )
        self.last_elapsed_ms = int((time.perf_counter() - started) * 1000)
        return findings

    def stats(self, findings: list[AnomalyFinding]) -> dict[str, int]:
        """规则命中统计。报告附录里会展示这张表——它能直接回答
        「今天到底是哪几条规则在报」以及「哪些规则从来不报」这两个运维问题。"""
        counts = {rule.id: 0 for rule in self.rules}
        for finding in findings:
            counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
        return counts

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": rule.id,
                "name": rule.name,
                "dimension": rule.dimension.value,
                "dimension_label": rule.dimension.label,
                "levels": [level.level.code for level in rule.levels],
                "implementation": rule.__class__.__name__,
            }
            for rule in self.rules
        ]


__all__ = ["DEFAULT_RULE_CLASS", "RULE_IMPLEMENTATIONS", "RuleEngine", "build_rules"]
