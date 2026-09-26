"""稳定性评分模型。

**核心主张：分数由规则算出，AI 只能微调 ±5。** 三个理由：

1. **可复现**：同一份数据必须得到同一个分数。否则「多次巡检结果对比」毫无意义，
   同一时刻重跑两次会得到两个分数，谁也没法判断业务到底变好了还是变差了；
2. **可解释**：报告要能逐项回答「这 12 分扣在哪」；
3. **抗幻觉**：大模型对数值的直觉并不可靠，它适合做归因和给建议，
   不适合承担打分这个需要一致性的职责。

评分结构（权重合计 100）：

    可用性 40  对照 SLO 换算错误预算消耗——这是 SRE 最标准的可用性口径，
               不用「成功率低于 X 就扣 Y 分」这种拍脑袋映射
    性能   25  超阈幅度 x 受影响面
    资源   20  超阈幅度 x 受影响面
    稳定性 15  流量异常、错误量异常、实例倾斜

封顶规则的用意：单个 P1 的存在本身就说明「这不是小问题」，
不允许被其它维度的高分掩盖掉。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from .config import Settings
from .models import (
    Anomaly,
    AnomalyCluster,
    DataQualityReport,
    ScoreBreakdownItem,
    ScoreResult,
    Severity,
    Topology,
)

logger = logging.getLogger(__name__)

DIMENSION_RULES: dict[str, frozenset[str]] = {
    "availability": frozenset({"BIZ-01", "BIZ-02", "BIZ-06"}),
    "performance": frozenset({"PERF-01", "PERF-02", "PERF-03", "PERF-04", "PERF-05"}),
    "resource": frozenset({"RES-01", "RES-02", "RES-03", "RES-04"}),
    "stability": frozenset({"BIZ-03", "BIZ-04", "BIZ-05", "RES-05"}),
}
"""子项与规则的归属关系。放在这里而不是散在代码里，是为了让人一眼看清
「哪些规则会影响哪一部分分数」——评分模型出问题时这是第一个要查的表。"""

AMPLITUDE_SHARE = 0.7
"""子项扣分里「严重程度」与「影响面」的相对比重。"""

BUDGET_FAILURE_RATIO = 10.0
"""错误率超过 SLO 允许值 10 倍，即视为该维度可用性完全失守。

为什么用「倍数」而不是「绝对值」：SLO 是 99.9% 的服务和 99.0% 的服务，
「跌到 98%」对前者是灾难、对后者只是轻微劣化。用相对错误预算倍数才能同口径比较。
"""


class ScoreCalculator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.topology: Topology = settings.topology

    # ------------------------------------------------------------------ 入口
    def compute(
        self,
        anomalies: list[Anomaly],
        clusters: list[AnomalyCluster],
        ai_adjust: int = 0,
        data_quality: DataQualityReport | None = None,
    ) -> ScoreResult:
        active = [a for a in anomalies if not a.is_suppressed]
        total_instances = max(1, len(self.topology.all_instances()))

        breakdown: list[ScoreBreakdownItem] = []
        for spec in self.settings.scoring.dimensions:
            if spec.key == "availability":
                item = self._availability(spec, active)
            else:
                item = self._by_dimension(spec, active, total_instances)
            breakdown.append(item)

        rule_score = round(sum(item.score for item in breakdown), 1)
        capped, cap_reason = self._apply_caps(rule_score, active, clusters)
        limit = self.settings.scoring.ai_adjust_limit
        adjust = max(-limit, min(limit, int(ai_adjust)))
        total = round(max(0.0, min(100.0, capped + adjust)), 1)

        if data_quality is not None and data_quality.confidence.value == "low":
            # 数据本身不可信时，分数只做参考。这一点必须写在报告里，
            # 否则一个「看起来 95 分」的结论会被当成事实。
            logger.warning("数据质量偏低（缺失率 %.2f%%），评分仅供参考", data_quality.missing_ratio * 100)

        return ScoreResult(
            rule_score=rule_score,
            ai_adjust=adjust,
            total=total,
            grade=ScoreResult.grade_of(total),
            capped_by=cap_reason,
            breakdown=breakdown,
        )

    # ------------------------------------------------------------------ 可用性
    def _availability(self, spec, anomalies: list[Anomaly]) -> ScoreBreakdownItem:  # noqa: ANN001
        """按错误预算消耗率扣分（SLO 口径）。"""
        worst_ratio = 0.0
        worst_detail: dict[str, Any] = {}
        for anomaly in anomalies:
            if anomaly.rule_id not in DIMENSION_RULES["availability"]:
                continue
            ratio = self._budget_ratio(anomaly)
            if ratio > worst_ratio:
                worst_ratio = ratio
                worst_detail = {
                    "service": anomaly.service,
                    "rule": anomaly.rule_id,
                    "observed": round(anomaly.observed, 3),
                    "budget_ratio": round(ratio, 2),
                }

        normalized = min(1.0, worst_ratio / BUDGET_FAILURE_RATIO)
        deduction = round(spec.weight * normalized, 1)
        if not worst_detail:
            reason = "全部服务成功率与业务错误率均在 SLO 之内，错误预算未消耗"
        else:
            reason = (
                f"{worst_detail['service']} 错误预算消耗达允许值的 {worst_detail['budget_ratio']:.1f} 倍"
                f"（{worst_detail['rule']}）"
            )
        return ScoreBreakdownItem(
            key=spec.key, name=spec.name, weight=spec.weight,
            deduction=deduction, score=round(spec.weight - deduction, 1),
            reason=reason, details=worst_detail,
        )

    def _budget_ratio(self, anomaly: Anomaly) -> float:
        """实际错误率相对 SLO 允许错误率的倍数。"""
        slo = self.settings.services.slo_for(anomaly.service)
        if anomaly.metric.value == "business_error_rate":
            allowed = max(1e-6, slo.business_error_rate)
            return max(0.0, anomaly.observed / allowed)
        if anomaly.metric.value == "success_rate":
            allowed = max(1e-6, 100.0 - slo.success_rate)
            actual = max(0.0, 100.0 - anomaly.observed)
            return actual / allowed
        # 链路健康度：把乘积口径换算成「被削弱了多少」
        if anomaly.rule_id == "BIZ-06":
            degrade = max(0.0, 100.0 - anomaly.observed)
            return degrade / max(1e-6, 100.0 - min(99.99, slo.success_rate * 4))
        return 0.0

    # ------------------------------------------------------------------ 通用子项
    def _by_dimension(
        self, spec, anomalies: list[Anomaly], total_instances: int
    ) -> ScoreBreakdownItem:  # noqa: ANN001
        related = [a for a in anomalies if a.rule_id in DIMENSION_RULES.get(spec.key, frozenset())]
        if not related:
            return ScoreBreakdownItem(
                key=spec.key, name=spec.name, weight=spec.weight,
                deduction=0.0, score=spec.weight, reason="本维度未发现异常",
            )

        worst = max(related, key=lambda a: a.amplitude)
        affected_instances = len({a.instance for a in related})
        scope = min(1.0, affected_instances / total_instances)
        deduction = round(spec.weight * (AMPLITUDE_SHARE * worst.amplitude + (1 - AMPLITUDE_SHARE) * scope), 1)
        reason = (
            f"{len(related)} 条异常、影响 {affected_instances} 个实例"
            f"（占全量 {scope:.0%}），最严重为 {worst.service}/{worst.instance} "
            f"{worst.metric.label}（{worst.level.display()}，幅度 {worst.amplitude:.2f}）"
        )
        return ScoreBreakdownItem(
            key=spec.key, name=spec.name, weight=spec.weight,
            deduction=deduction, score=round(spec.weight - deduction, 1),
            reason=reason,
            details={"anomalies": len(related), "instances": affected_instances, "scope": round(scope, 3)},
        )

    # ------------------------------------------------------------------ 封顶
    def _apply_caps(
        self,
        score: float,
        anomalies: list[Anomaly],
        clusters: list[AnomalyCluster],
    ) -> tuple[float, str | None]:
        p1s = [a for a in anomalies if a.level is Severity.P1]
        facts = {
            "has_p1": bool(p1s),
            "p1_on_critical": any(self.topology.criticality_of(a.service) >= 0.9 for a in p1s),
            "p1_count_ge": len(p1s),
        }
        # 取所有命中封顶条件里最严格的那个。
        # 不能「首个命中即返回」：配置里 has_p1 在最前面，如果直接返回，
        # 后面更严格的 p1_on_critical（45 分）永远没有机会生效——
        # 而「核心链路的 P1」恰恰是更需要拉低分数的情形。
        applicable = [cap for cap in self.settings.scoring.caps if _cap_matches(cap.condition, facts)]
        if not applicable:
            return score, None
        strictest = min(applicable, key=lambda cap: cap.max)
        return (strictest.max, strictest.reason) if score > strictest.max else (score, None)


def _cap_matches(condition: str, facts: dict[str, Any]) -> bool:
    """解析封顶条件。支持 `has_p1`、`p1_on_critical`、`p1_count_ge:2` 三种形式。"""
    if ":" in condition:
        key, _, raw = condition.partition(":")
        try:
            threshold = float(raw)
        except ValueError:
            return False
        return float(facts.get(key, 0.0)) >= threshold
    return bool(facts.get(condition, False))


def score_grade(score: float) -> str:
    return ScoreResult.grade_of(score)


def safe_ratio(numerator: float, denominator: float) -> float:
    if not denominator or math.isnan(denominator):
        return 0.0
    return numerator / denominator
