"""证据包组装。

**这是整个 AI 归因模块最关键的产物**：喂给大模型的东西决定了它能得出什么结论。
喂结论，它就复述结论；喂事实，它才可能做推理。

证据包里只放五类客观事实：

1. **异常簇**：每个簇的传播路径、各条异常的实测值/基线/偏离，以及证据 ID。
   注意里面**没有**规则给的分级理由之外的任何判断——
   连 `rule_hypothesis`（规则侧的推测）也单独放一个字段，
   在提示词里明确说明它只是待验证的猜想，不是结论；
2. **依赖拓扑**：谁调用谁，让模型能自己判断传播方向；
3. **变更记录**：窗口内的发布/配置变更，这是因果推理里最强的一类证据；
4. **时序摘要**：每个相关指标在观察窗内的分布与趋势，让模型能看见「还在涨」；
5. **历史同类事件**：带相似度的历史根因，让模型能识别复发。

刻意**不放原始时序点**：30 个点的一条序列对判断趋势的边际价值很低，
但几十条序列全塞进去会挤爆上下文、拉高成本，还会让模型淹死在数字里。
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Anomaly, AnomalyCluster, DataQualityReport, ScoreResult
from ..rules import EvalContext
from ..utils.text import truncate
from .history import HistoryMatch

logger = logging.getLogger(__name__)

MAX_SERIES_DIGESTS = 40
"""时序摘要条数上限。超出时只保留最严重的若干条，控制上下文长度。"""


def build_evidence(
    ctx: EvalContext,
    clusters: list[AnomalyCluster],
    history: dict[str, list[HistoryMatch]],
    data_quality: DataQualityReport | None,
    score: ScoreResult | None,
) -> dict[str, Any]:
    return {
        "run": {
            "window": {
                "start": ctx.window.start.isoformat(timespec="minutes"),
                "end": ctx.window.end.isoformat(timespec="minutes"),
                "duration_minutes": round(ctx.window.duration_minutes, 1),
            },
            "granularity_minutes": ctx.step_minutes,
            "scope": {
                "services": len(ctx.all_services()),
                "instances": len(ctx.instances()),
                "monitored_metrics": 10,
            },
            "baseline": {
                "method": "历史同时段（同星期几 + 同小时）稳健统计 median ± 4 × 1.4826 × MAD",
                "lookback_days": ctx.settings.app.baseline_days,
            },
        },
        "topology": {
            "description": "service -> 它依赖的下游服务（调用方向）",
            "graph": ctx.topology.as_prompt_dict(),
            "criticality": {name: ctx.criticality(name) for name in ctx.all_services()},
        },
        "rule_score": _score_dict(score),
        "data_quality": _quality_dict(data_quality),
        "clusters": [_cluster_dict(ctx, cluster, history.get(cluster.cluster_id, [])) for cluster in clusters],
        "changes_in_lookback": [
            change.to_prompt_dict() for change in _relevant_changes(ctx, clusters)
        ],
        "instructions_note": (
            "证据包中所有数值均为实测事实。clusters[].rule_hypothesis 是巡检规则给出的"
            "**待验证猜想**，请独立判断，不必与它一致；若你的结论与之不同，"
            "请在 uncertainties 中说明依据。"
        ),
    }


def _cluster_dict(
    ctx: EvalContext,
    cluster: AnomalyCluster,
    matches: list[HistoryMatch],
) -> dict[str, Any]:
    payload = cluster.to_prompt_dict()
    anomalies = cluster.all_anomalies()[:MAX_SERIES_DIGESTS]
    payload["evidence"] = [_evidence_item(ctx, anomaly) for anomaly in anomalies]
    payload["temporal"] = {
        "first_seen": cluster.primary.first_seen.isoformat(timespec="minutes"),
        "duration_minutes": round(cluster.primary.duration_minutes, 1),
        "affected_instances": len({a.instance for a in cluster.all_anomalies()}),
    }
    if matches:
        payload["similar_history"] = [match.to_prompt_dict() for match in matches]
    return payload


def _evidence_item(ctx: EvalContext, anomaly: Anomaly) -> dict[str, Any]:
    item = anomaly.evidence_ref()
    try:
        series = ctx.series(anomaly.service, anomaly.instance, anomaly.metric)
        item["series"] = series.digest()
    except Exception:  # noqa: BLE001 - 摘要缺失不应阻断证据包组装
        logger.debug("时序摘要生成失败: %s", anomaly.anomaly_id, exc_info=True)
    item["level_reason"] = truncate(anomaly.level_reason, 160)
    return item


def _relevant_changes(ctx: EvalContext, clusters: list[AnomalyCluster]):
    """只保留与异常服务相关的变更，减少干扰项。

    背景变更（与本次异常无关的历史发布）会被这里过滤掉大部分——
    但**保留它们的存在感仍然重要**：真正的干扰项来自「时间接近但因果无关」的变更，
    那类会被保留下来，正是它们让「看到变更就说是变更引入」的懒惰推理暴露出来。
    """
    services = {service for cluster in clusters for service in cluster.services}
    return [change for change in ctx.changes() if change.service in services or not clusters]


def _score_dict(score: ScoreResult | None) -> dict[str, Any] | None:
    if score is None:
        return None
    return {
        "total": score.total,
        "grade": score.grade,
        "breakdown": [
            {"name": item.name, "weight": item.weight, "deduction": item.deduction, "reason": item.reason}
            for item in score.breakdown
        ],
    }


def _quality_dict(report: DataQualityReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "missing_ratio": round(report.missing_ratio, 4),
        "interpolated_ratio": round(report.interpolated_ratio, 4),
        "confidence": report.confidence.value,
        "issues": [issue.detail for issue in report.issues[:5]],
    }


def collect_evidence_ids(clusters: list[AnomalyCluster]) -> set[str]:
    """证据包里实际存在的 evidence_id 全集，用于校验模型有没有编造引用。"""
    return {
        ref["evidence_id"]
        for cluster in clusters
        for ref in cluster.evidence_refs()
    }


def build_clean_evidence(ctx: EvalContext, data_quality: DataQualityReport | None) -> dict[str, Any]:
    """无异常时的证据包。仍然保留，便于排查「为什么本次没报警」。"""
    return {
        "run": {
            "window": {
                "start": ctx.window.start.isoformat(timespec="minutes"),
                "end": ctx.window.end.isoformat(timespec="minutes"),
            },
            "scope": {"services": len(ctx.all_services()), "instances": len(ctx.instances())},
        },
        "data_quality": _quality_dict(data_quality),
        "clusters": [],
    }
