"""多次巡检结果对比。

「支持多次巡检结果对比」这个要求，真正有价值的部分不是把两份报告并排放，
而是回答运维最关心的四个问题：

    新增了哪些问题？（上一次没有，这一次有了 -> 可能是刚发生的故障）
    哪些已经恢复？（上一次有，这一次没有了 -> 处置生效，可以确认闭环）
    哪些还在？（两次都有 -> 没解决，或者根本没人在处理）
    哪些是复发？（历史上出现过、修好过、又回来了 -> 修复不彻底的信号）

第四条尤其重要：**复发比首次发生更值得警惕**，它说明上一次的处置只治了症状。
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Anomaly, InspectionRun, RunComparison, ScoreSnapshot

logger = logging.getLogger(__name__)


def anomaly_signature(anomaly: Anomaly) -> str:
    return f"{anomaly.rule_id}|{anomaly.service}|{anomaly.instance}|{anomaly.metric.value}"


def row_signature(row: dict[str, Any]) -> str:
    return f"{row.get('rule_id')}|{row.get('service')}|{row.get('instance')}|{row.get('metric')}"


def build_comparison(
    current: InspectionRun,
    current_anomalies: list[Anomaly],
    previous: InspectionRun | None,
    previous_rows: list[dict[str, Any]] | None,
) -> RunComparison | None:
    if previous is None:
        return None

    current_map = {anomaly_signature(a): a for a in current_anomalies}
    previous_signatures = {row_signature(row) for row in (previous_rows or [])}

    new = sorted(set(current_map) - previous_signatures)
    persistent = sorted(set(current_map) & previous_signatures)
    recovered = sorted(previous_signatures - set(current_map))
    recurring = sorted(sig for sig, anomaly in current_map.items() if anomaly.is_recurring)

    delta = round((current.score or 0.0) - (previous.score or 0.0), 1)
    trend = "flat"
    if delta > 1.0:
        trend = "up"
    elif delta < -1.0:
        trend = "down"

    return RunComparison(
        prev_run_id=previous.run_id,
        prev_started_at=previous.started_at.isoformat(timespec="minutes"),
        score_delta=delta,
        score_trend=trend,
        new_anomalies=[_describe(sig, current_map) for sig in new],
        recovered=[_describe_plain(sig) for sig in recovered],
        persistent=[_describe(sig, current_map) for sig in persistent],
        recurring=[_describe(sig, current_map) for sig in recurring],
        summary=_summary(trend, delta, new, recovered, persistent, recurring),
    )


def build_history(
    snapshots: list[ScoreSnapshot],
    current_run_id: str,
    current_score: float,
    current_started_at: str,
    scenario_id: str | None,
) -> list[ScoreSnapshot]:
    """把本次结果并入历史序列（去掉重复项），供趋势图使用。"""
    history = [s for s in snapshots if s.run_id != current_run_id]
    history.append(
        ScoreSnapshot(
            run_id=current_run_id,
            started_at=current_started_at,
            score=current_score,
            scenario_id=scenario_id,
        )
    )
    return history[-12:]


def _describe(signature: str, mapping: dict[str, Anomaly]) -> str:
    anomaly = mapping.get(signature)
    if anomaly is None:
        return signature
    return f"[{anomaly.level.display()}] {anomaly.service}/{anomaly.instance} {anomaly.metric.label}"


def _describe_plain(signature: str) -> str:
    parts = signature.split("|")
    return f"{parts[1]}/{parts[2]} {parts[3]}" if len(parts) == 4 else signature


def _summary(
    trend: str,
    delta: float,
    new: list[str],
    recovered: list[str],
    persistent: list[str],
    recurring: list[str],
) -> str:
    direction = {"up": "上升", "down": "下降", "flat": "基本持平"}[trend]
    parts = [
        f"稳定性评分较上次{direction} {abs(delta):.1f} 分",
        f"新增异常 {len(new)} 项、已恢复 {len(recovered)} 项、持续未解决 {len(persistent)} 项",
    ]
    if recurring:
        parts.append(f"其中 {len(recurring)} 项为历史复发，说明上次的处置并未根治")
    elif persistent:
        parts.append("持续未解决的项需要确认是否有人在跟进")
    return "；".join(parts) + "。"
