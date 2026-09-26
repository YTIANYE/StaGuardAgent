"""Markdown 巡检报告渲染。

报告结构刻意固定为八章——固定的结构意味着阅读者知道去哪里找什么，
这也正是「标准化报告」相对自由文本的核心价值：

    1. 巡检概览      覆盖范围、评分卡（含评分来源口径）、结论一句话
    2. 风险总结      AI 的总结（或规则归因的总结）
    3. 异常清单      按等级排序，含实测 vs 基线、起始时刻、级别理由
    4. 多粒度统计    服务维度 / 集群维度的巡检统计
    5. 根因分析      按根因簇，含传播路径、证据引用、与规则假设的一致性
    6. 修复建议      分「立即止血 / 短期 / 长期治理」三档，带责任方
    7. 稳定性趋势    与上次的对比、评分历史
    8. 附录          数据质量、规则命中统计、阶段耗时、AI 调用元信息

第 5 章会同时展示「规则假设」和「AI 结论」，两者不一致时会显式标注
`⚠️ 与规则假设不一致`——这种分歧恰恰是最需要人工看一眼的地方，
把它藏起来等于丢掉了一次发现问题的机会。
"""

from __future__ import annotations

from ..models import InspectionReport
from ..utils.text import fmt, sparkline, truncate
from ..utils.timeutil import format_duration, format_ts

LEVEL_BADGE = {"P1": "🔴 P1 紧急", "P2": "🟠 P2 严重", "P3": "🟡 P3 一般", "P4": "🔵 P4 轻微"}


def render(report: InspectionReport) -> str:
    lines: list[str] = []
    lines.extend(_header(report))
    lines.extend(_overview(report))
    lines.extend(_summary(report))
    lines.extend(_anomalies(report))
    lines.extend(_granularity(report))
    lines.extend(_clusters(report))
    lines.extend(_suggestions(report))
    lines.extend(_trend(report))
    lines.extend(_appendix(report))
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- 概览
def _header(report: InspectionReport) -> list[str]:
    run = report.run
    return [
        "# 业务稳定性巡检报告",
        "",
        f"> 巡检编号 `{run.run_id}`　|　场景 `{run.scenario_id or 'default'}`　|　"
        f"数据源 `{run.source}`　|　状态 `{run.status.label}`",
        "",
    ]


def _overview(report: InspectionReport) -> list[str]:
    run, score = report.run, report.score
    counts = run.level_counts
    lines = [
        "## 一、巡检概览",
        "",
        f"- **巡检窗口**：{run.window.label(with_date=True)}（{format_duration(run.window.duration_minutes)}），"
        f"粒度 {run.granularity_seconds}s",
        f"- **覆盖范围**：{run.scope_services} 个服务 / {run.scope_instances} 个实例",
        f"- **执行耗时**：{run.duration_ms / 1000:.1f}s",
        "",
        "### 稳定性评分",
        "",
        "| 评分 | 等级 | 异常分布 |",
        "| --- | --- | --- |",
        (
            f"| **{score.total:.1f} / 100** | {score.grade} | "
            f"{LEVEL_BADGE['P1']}: {counts.get('P1', 0)}　"
            f"{LEVEL_BADGE['P2']}: {counts.get('P2', 0)}　"
            f"{LEVEL_BADGE['P3']}: {counts.get('P3', 0)}　"
            f"{LEVEL_BADGE['P4']}: {counts.get('P4', 0)} |"
        ),
        "",
        "### 扣分明细",
        "",
        "| 维度 | 权重 | 得分 | 扣分 | 依据 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for item in score.breakdown:
        lines.append(
            f"| {item.name} | {item.weight:g} | {item.score:g} | {item.deduction:g} | {item.reason} |"
        )
    lines.append("")
    if score.capped_by:
        lines.append(f"> **封顶生效**：{score.capped_by}")
        lines.append("")
    if score.ai_adjust:
        lines.append(
            f"> 规则算分 {score.rule_score:.1f}，AI 微调 {score.ai_adjust:+d} 分，最终 {score.total:.1f} 分。"
        )
        lines.append("")
    elif report.ai_meta.degraded:
        reason = report.ai_meta.degraded_reason or "原因未记录"
        lines.append(f"> AI 降级运行（{reason}），评分完全由规则算出，未经模型调整。")
        lines.append("")
    elif report.ai is not None:
        lines.append("> 评分为规则算分（逐项依据见上表），AI 未调整分数。")
        lines.append("")
    return lines


def _summary(report: InspectionReport) -> list[str]:
    lines = ["## 二、风险总结", ""]
    if report.ai is None:
        lines.extend(["（本次未生成 AI 分析）", ""])
        return lines
    lines.append(report.ai.summary)
    lines.append("")
    trend = report.ai.trend
    lines.append(f"- **趋势判断**：{trend.label}")
    if trend.reasoning:
        lines.append(f"  - 依据：{trend.reasoning}")
    if trend.forecast:
        lines.append(f"  - 预测：{trend.forecast}")
    if report.ai.uncertainties:
        lines.append("- **证据不足之处**：")
        for item in report.ai.uncertainties:
            lines.append(f"  - {item}")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- 异常清单
def _anomalies(report: InspectionReport) -> list[str]:
    lines = ["## 三、异常清单", ""]
    active = [a for a in report.anomalies if not a.is_suppressed]
    if not active:
        lines.extend(["本次巡检未发现越线异常。", ""])
        return lines

    lines.append(
        "| 级别 | 服务 / 实例 | 指标 | 实测 | 基线 | 偏离 | 持续 | 起始 | 级别理由 |"
    )
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |")
    for anomaly in active:
        baseline = fmt(anomaly.baseline_value, 2)
        deviation = f"{anomaly.deviation_pct:+.2f}%"
        lines.append(
            f"| {LEVEL_BADGE[anomaly.level.code]} "
            f"| {anomaly.service} / {anomaly.instance} "
            f"| {anomaly.metric.label} "
            f"| {fmt(anomaly.observed, 2)} "
            f"| {baseline} "
            f"| {deviation} "
            f"| {format_duration(anomaly.duration_minutes)} "
            f"| {format_ts(anomaly.first_seen, with_date=False)} "
            f"| {truncate(anomaly.level_reason, 70)} |"
        )
    lines.append("")

    suppressed = [a for a in report.anomalies if a.is_suppressed]
    if suppressed:
        lines.append(
            f"<details><summary>另有 {len(suppressed)} 条低级别异常被同指标更严重的问题抑制</summary>"
        )
        lines.append("")
        for anomaly in suppressed:
            lines.append(
                f"- {anomaly.rule_id} {anomaly.service}/{anomaly.instance} {anomaly.metric.label}"
                f"（{anomaly.level.display()}）— 已由 {anomaly.suppressed_by} 覆盖"
            )
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- 统计
def _granularity(report: InspectionReport) -> list[str]:
    """服务维度与集群维度统计。

    只统计未抑制异常：被抑制项与代表它的那条是同一服务同一指标上的同一类问题，
    计入会让同一个故障被数两遍。
    """
    lines = ["## 四、多粒度巡检统计", ""]
    if not report.service_stats:
        lines.extend(["本次未生成多粒度统计。", ""])
        return lines

    active_services = [s for s in report.service_stats if not s.is_healthy]
    lines.append(
        f"> 统计对象为**未抑制异常**；影响面 = 该维度下出现异常的实例数 / 实例总数。"
        f"本次覆盖 {len(report.service_stats)} 个服务（其中 {len(active_services)} 个有异常）、"
        f"{len(report.cluster_stats)} 个集群。"
    )
    lines.append("")

    lines.append("### 服务维度")
    lines.append("")
    lines.append("| 服务 | 集群 | 异常数 | 最严重 | 受影响实例 | 影响面 | 主要指标 | 命中规则 |")
    lines.append("| --- | --- | ---: | --- | --- | ---: | --- | --- |")
    for stat in report.service_stats:
        lines.append(
            f"| {stat.service} "
            f"| {stat.cluster_label} "
            f"| {stat.anomaly_count} "
            f"| {LEVEL_BADGE[stat.max_level.code] if stat.max_level else '—'} "
            f"| {_instance_cell(stat)} "
            f"| {_ratio_cell(stat.affected_ratio)} "
            f"| {stat.worst_metric or '—'} "
            f"| {'、'.join(stat.rule_ids) if stat.rule_ids else '—'} |"
        )
    lines.append("")

    lines.append("### 集群维度")
    lines.append("")
    lines.append("| 集群 | 服务数 | 异常服务 | 异常数 | 最严重 | 受影响实例 | 影响面 | 根因簇 |")
    lines.append("| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |")
    for cluster in report.cluster_stats:
        lines.append(
            f"| {cluster.label} "
            f"| {cluster.service_count} "
            f"| {cluster.affected_service_count} "
            f"| {cluster.anomaly_count} "
            f"| {LEVEL_BADGE[cluster.max_level.code] if cluster.max_level else '—'} "
            f"| {cluster.affected_instances} / {cluster.total_instances} "
            f"| {_ratio_cell(cluster.affected_ratio)} "
            f"| {cluster.root_cause_count} |"
        )
    lines.append("")
    return lines


def _instance_cell(stat) -> str:  # noqa: ANN001
    if not stat.total_instances:
        return "—"
    shown = "、".join(stat.affected_instances) if stat.affected_instances else "—"
    return f"{shown}（{len(stat.affected_instances)} / {stat.total_instances}）"


def _ratio_cell(ratio: float | None) -> str:
    return "—" if ratio is None else f"{ratio:.0%}"


# --------------------------------------------------------------------------- 根因
def _clusters(report: InspectionReport) -> list[str]:
    lines = ["## 五、根因分析", ""]
    if not report.clusters:
        lines.extend(["未形成根因簇。", ""])
        return lines

    active = [a for a in report.anomalies if not a.is_suppressed]
    suppressed = len(report.anomalies) - len(active)
    coverage = (
        f"> 本次 {len(active)} 条未抑制异常全部归入 {len(report.clusters)} 个根因簇，逐簇给出根因。"
    )
    if suppressed:
        coverage += f"另有 {suppressed} 条被抑制异常不单独归因（见异常清单折叠区）。"
    lines.append(coverage)
    lines.append("")

    for cluster in report.clusters:
        finding = report.ai.finding_for(cluster.cluster_id) if report.ai else None
        lines.append(f"### {cluster.cluster_id}　{cluster.primary.service}　[{cluster.max_level.display()}]")
        lines.append("")
        lines.append(
            f"- **信号规模**：{cluster.size} 条异常，涉及 {len(cluster.services)} 个服务"
            f"（{'、'.join(cluster.services)}）"
        )
        lines.append(
            f"- **传播路径**：`{' → '.join(cluster.propagation_path)}`"
            f"　（数据流方向，故障影响由此向上游扩散）"
        )
        lines.append(f"- **规则假设**：{cluster.hypothesis}")
        if finding is None:
            lines.append("- **AI 结论**：（本次未获取到该簇的 AI 结论）")
            lines.append("")
            continue

        lines.append(
            f"- **AI 根因**：**{finding.category_label}** — {finding.root_cause}"
            f"（置信度 {finding.confidence:.0%}"
            f"{'，**历史复发**' if finding.is_recurring else ''}）"
        )
        if finding.root_cause_service:
            lines.append(f"- **AI 认定的根因服务**：`{finding.root_cause_service}`")
        if finding.impact:
            lines.append(f"- **影响面**：{finding.impact}")
        if finding.owner_hint:
            lines.append(f"- **建议责任方**：{finding.owner_hint}")
        if finding.root_cause_service and finding.root_cause_service != cluster.primary.service:
            lines.append(
                f"- ⚠️ **根因定位分歧**：规则聚类判定根因在 `{cluster.primary.service}`"
                f"（依据：{truncate(cluster.hypothesis, 60)}），"
                f"AI 判定在 `{finding.root_cause_service}`。"
                "两者依据不同，这种分歧值得人工确认一次。"
            )
        lines.append("")
        lines.append(f"<details><summary>证据引用（{len(finding.evidence_ids)} 条）</summary>")
        lines.append("")
        for evidence_id in finding.evidence_ids[:12]:
            lines.append(f"- `{evidence_id}`")
        if len(finding.evidence_ids) > 12:
            lines.append(f"- …其余 {len(finding.evidence_ids) - 12} 条同类证据（见 JSON 报告）")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- 建议
def _suggestions(report: InspectionReport) -> list[str]:
    lines = ["## 六、修复建议", ""]
    if report.ai is None or not report.ai.findings:
        lines.extend(["本次无需要处置的异常。", ""])
        return lines

    lines.append("### 立即止血（本次窗口内）")
    lines.append("")
    for finding in report.ai.findings:
        if not finding.suggestions:
            continue
        lines.append(f"- **{finding.cluster_id} · {finding.category_label}**")
        for suggestion in finding.suggestions[:1]:
            lines.append(f"  - {suggestion}")
    lines.append("")

    lines.append("### 短期加固（本周内）")
    lines.append("")
    for finding in report.ai.findings:
        for suggestion in finding.suggestions[1:]:
            lines.append(f"- [{finding.cluster_id}] {suggestion}")
    lines.append("")

    lines.append("### 长期治理")
    lines.append("")
    for advice in report.ai.stability_advice:
        lines.append(f"- {advice}")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- 趋势
def _trend(report: InspectionReport) -> list[str]:
    lines = ["## 七、稳定性趋势", ""]
    comparison = report.comparison
    if comparison is None:
        lines.extend(["本次为首次巡检，暂无可对比的历史记录。", ""])
    else:
        lines.append(f"**对比对象**：`{comparison.prev_run_id}`（{comparison.prev_started_at}）")
        lines.append("")
        lines.append(
            f"- 评分变化：**{comparison.score_delta:+.1f} 分**（{comparison.score_trend_label}）"
        )
        lines.append(f"- {comparison.summary}")
        lines.append("")
        for title, items in (
            ("新增异常", comparison.new_anomalies),
            ("历史复发", comparison.recurring),
            ("持续未解决", comparison.persistent),
            ("已恢复", comparison.recovered),
        ):
            if not items:
                continue
            lines.append(f"**{title}（{len(items)}）**")
            lines.append("")
            for item in items[:12]:
                lines.append(f"- {item}")
            lines.append("")

    if report.history:
        scores = [snapshot.score for snapshot in report.history]
        lines.append("### 评分历史")
        lines.append("")
        lines.append(f"`{sparkline(scores)}`　最近 {len(scores)} 次：{' → '.join(f'{s:.0f}' for s in scores)}")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- 附录
def _appendix(report: InspectionReport) -> list[str]:
    lines = ["## 八、附录", ""]

    lines.append("### 数据质量")
    lines.append("")
    if report.data_quality is None:
        lines.append("- 未采集数据质量信息")
    else:
        quality = report.data_quality
        lines.append(f"- {quality.summary}")
        lines.append(
            f"- 有效样本 {quality.total_points} / 应有 {quality.expected_points}"
            f"（缺失率 {quality.missing_ratio:.2%}，插值占比 {quality.interpolated_ratio:.2%}）"
        )
        lines.append(f"- 结论置信度：**{quality.confidence.label}**")
    lines.append("")

    lines.append("### 规则命中统计")
    lines.append("")
    hits = {rule_id: count for rule_id, count in report.rule_stats.items() if count}
    if hits:
        lines.append("| 规则 | 命中 |")
        lines.append("| --- | ---: |")
        for rule_id, count in sorted(hits.items()):
            lines.append(f"| {rule_id} | {count} |")
    else:
        lines.append("本次无规则命中。")
    lines.append("")

    if report.baselines is not None:
        lines.append("### 动态基线覆盖")
        lines.append("")
        lines.append(
            f"- 动态基线覆盖率：**{report.baselines.dynamic_ratio():.0%}**"
            f"（其余为样本不足时的静态阈值回退）"
        )
        lines.append("")

    lines.append("### 变更事件")
    lines.append("")
    if report.changes:
        lines.append("| 时间 | 服务 | 类型 | 版本 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for change in report.changes:
            lines.append(
                f"| {format_ts(change.ts, with_date=False)} | {change.service} "
                f"| {change.type.label} | {change.version or '-'} | {truncate(change.description or '', 40)} |"
            )
    else:
        lines.append("观察窗内无变更事件。")
    lines.append("")

    lines.append("### 执行阶段与 AI 调用")
    lines.append("")
    lines.append("| 阶段 | 状态 | 耗时 | 说明 |")
    lines.append("| --- | --- | ---: | --- |")
    for stage in report.run.stages:
        detail = truncate(stage.error or stage.detail, 60)
        lines.append(f"| {stage.label} | {stage.status.label} | {stage.duration_ms}ms | {detail} |")
    lines.append("")
    meta = report.ai_meta
    lines.append(
        f"- AI 分析：{meta.status_label}；调用 {meta.attempts} 次，"
        f"耗时 {meta.latency_ms}ms，token {meta.total_tokens}，提示词版本 {meta.prompt_version}"
    )
    lines.append("")
    return lines
