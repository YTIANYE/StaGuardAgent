"""AI 归因质量评测集。

「AI 归因准不准」如果只靠翻两份报告说「看起来挺准的」，那是没有说服力的。
这个模块把七个场景变成七个可判定的题目，用真实模型跑一遍，
量化输出三个指标：

    **根因命中率**：AI 给出的根因类别是否落在期望集合内
    **级别命中率**：最严重异常的定级是否与期望一致
    **误报率**：正常态场景是否被误判成故障（这条最难，也最有价值）

同时输出每个场景的**降级状态与耗时**，让「AI 是否真的在干活」有据可查。

关于判定口径的两个说明：
1. 根因允许**多个正确答案**（`also_accept`）。内存泄漏既可归为「资源瓶颈」
   也可归为「代码异常」，两者都能推出正确的处置动作，不应判错；
2. 正常态场景的「干净」标准是**无 P1/P2 异常**，而不是「零异常」。
   要求零异常等于要求规则对噪声零敏感，那在真实系统里不可能做到；
   巡检系统的目标是把误报压到「不打扰人」的量级。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Settings
from .models import Severity
from .orchestrator import InspectionOrchestrator
from .store import Repository

logger = logging.getLogger(__name__)


@dataclass
class ScenarioOutcome:
    scenario_id: str
    name: str
    expected_cause: str
    accepted_causes: set[str]
    expected_level: str
    expect_clean: bool

    run_id: str = ""
    actual_causes: list[str] = field(default_factory=list)
    top_cause: str = "none"
    top_level: str = "none"
    score: float = 0.0
    anomaly_count: int = 0
    level_counts: dict[str, int] = field(default_factory=dict)
    ai_degraded: bool = False
    ai_reason: str | None = None
    duration_ms: int = 0
    error: str | None = None

    @property
    def cause_hit(self) -> bool:
        if self.expect_clean:
            # 正常态场景的判定看「有没有被误判成故障」，而不是「有没有归因结论」：
            # 一条 P4 也会产生一个归因结论，那不代表误报。
            return self.level_hit
        return bool(self.accepted_causes & set(self.actual_causes))

    @property
    def level_hit(self) -> bool:
        if self.expect_clean:
            return self.level_counts.get("P1", 0) == 0 and self.level_counts.get("P2", 0) == 0
        return self.top_level == self.expected_level

    @property
    def false_positive(self) -> bool:
        """正常态场景出现 P1/P2 —— 这是最需要盯住的失败模式。"""
        return self.expect_clean and not self.level_hit


@dataclass
class EvaluationReport:
    outcomes: list[ScenarioOutcome] = field(default_factory=list)

    @property
    def root_cause_accuracy(self) -> float:
        graded = [o for o in self.outcomes if not o.error]
        if not graded:
            return 0.0
        return sum(1 for o in graded if o.cause_hit) / len(graded)

    @property
    def level_accuracy(self) -> float:
        graded = [o for o in self.outcomes if not o.error]
        if not graded:
            return 0.0
        return sum(1 for o in graded if o.level_hit) / len(graded)

    @property
    def false_positive_rate(self) -> float:
        clean = [o for o in self.outcomes if o.expect_clean and not o.error]
        if not clean:
            return 0.0
        return sum(1 for o in clean if o.false_positive) / len(clean)

    @property
    def degraded_ratio(self) -> float:
        graded = [o for o in self.outcomes if not o.error]
        if not graded:
            return 0.0
        return sum(1 for o in graded if o.ai_degraded) / len(graded)

    def render(self) -> str:
        lines = [
            "",
            "归因评测结果",
            "=" * 96,
            f"{'场景':<6}{'期望根因':<22}{'实际根因':<22}{'期望级别':<8}{'实际级别':<8}{'评分':>7}{'命中':>6}",
            "-" * 96,
        ]
        for outcome in self.outcomes:
            if outcome.error:
                lines.append(f"{outcome.scenario_id:<6}ERROR: {outcome.error}")
                continue
            mark = "✅" if outcome.cause_hit and outcome.level_hit else ("🟡" if outcome.cause_hit else "❌")
            lines.append(
                f"{outcome.scenario_id:<6}{outcome.expected_cause:<22}{outcome.top_cause:<22}"
                f"{outcome.expected_level:<8}{outcome.top_level:<8}"
                f"{outcome.score:>7.1f}{mark:>6}"
            )
        lines.extend(
            [
                "-" * 96,
                f"根因命中率  {self.root_cause_accuracy:.0%}",
                f"级别命中率  {self.level_accuracy:.0%}",
                f"误报率（正常态被判为故障）  {self.false_positive_rate:.0%}",
                f"AI 降级比例  {self.degraded_ratio:.0%}",
                "=" * 96,
            ]
        )
        return "\n".join(lines)


def run_evaluation(
    settings: Settings,
    repo: Repository,
    source_name: str | None = None,
) -> EvaluationReport:
    report = EvaluationReport()
    orchestrator = InspectionOrchestrator(settings, repo)

    for scenario in settings.scenarios.scenarios:
        outcome = ScenarioOutcome(
            scenario_id=scenario.id,
            name=scenario.name,
            expected_cause=scenario.expected_root_cause,
            accepted_causes={scenario.expected_root_cause, *scenario.also_accept},
            expected_level=scenario.expected_level,
            expect_clean=scenario.expect_clean,
        )
        try:
            inspection = orchestrator.run(scenario_id=scenario.id, source_name=source_name, notify=False)
        except Exception as exc:  # noqa: BLE001 - 评测要跑完所有场景，单个失败不中断
            logger.exception("场景 %s 巡检失败", scenario.id)
            outcome.error = f"{exc.__class__.__name__}: {exc}"
            report.outcomes.append(outcome)
            continue

        outcome.run_id = inspection.run.run_id
        outcome.score = inspection.score.total
        outcome.anomaly_count = inspection.run.anomaly_count
        outcome.level_counts = inspection.run.level_counts
        outcome.ai_degraded = inspection.ai_meta.degraded
        outcome.ai_reason = inspection.ai_meta.degraded_reason
        outcome.duration_ms = inspection.run.duration_ms

        if inspection.clusters:
            outcome.top_level = inspection.clusters[0].max_level.code
        elif inspection.run.highest_level:
            outcome.top_level = inspection.run.highest_level.code
        if inspection.ai is not None:
            outcome.actual_causes = [f.root_cause_category.value for f in inspection.ai.findings]
            top_cluster = inspection.clusters[0] if inspection.clusters else None
            if top_cluster is not None:
                top_finding = inspection.ai.finding_for(top_cluster.cluster_id)
                outcome.top_cause = (
                    top_finding.root_cause_category.value if top_finding else "unknown"
                )
            elif outcome.actual_causes:
                outcome.top_cause = outcome.actual_causes[0]
        report.outcomes.append(outcome)

    return report


def summarize_levels(counts: dict[str, int]) -> str:
    return " ".join(f"{level}={counts.get(level, 0)}" for level in (s.code for s in Severity))
