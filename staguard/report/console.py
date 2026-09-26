"""控制台结构化输出。

面向「人盯着终端看」的场景，和 Markdown 报告的信息密度取法不同：
终端一次能看的行数有限，所以优先回答「最要紧的是什么」——
先给评分和等级分布，再给 Top 异常，最后给一句话结论。
细节留在 Markdown 报告里，不往终端堆。

多粒度统计上遵循同一条取舍：终端只给**集群维度**（最多 5 行），不给服务维度——
服务级明细在异常清单里已经能看到服务/实例列，再铺一张全服务表只会把 P1 挤下去。
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..models import InspectionReport, Severity
from ..utils.text import sparkline, truncate
from ..utils.timeutil import format_duration, format_ts

logger = logging.getLogger(__name__)

LEVEL_STYLE = {
    Severity.P1: "bold white on red",
    Severity.P2: "bold black on dark_orange",
    Severity.P3: "bold black on yellow",
    Severity.P4: "white on blue",
}

SCORE_STYLE = [
    (90, "bold green"),
    (80, "green"),
    (70, "yellow"),
    (60, "dark_orange"),
    (0, "bold red"),
]

TOP_ANOMALIES = 8

console = Console()


def render(report: InspectionReport, verbose: bool = False) -> None:
    console.print(_header(report))
    console.print(_score_panel(report))
    console.print(_level_table(report))
    _cluster_stats_table(report)
    _anomaly_table(report)
    _cluster_hints(report)
    _trend_panel(report)
    _footer(report, verbose)


def _header(report: InspectionReport) -> Panel:
    run = report.run
    body = Text()
    body.append(f"巡检编号  {run.run_id}\n", style="bold")
    body.append(f"时间窗口  {run.window.label(with_date=True)}（{format_duration(run.window.duration_minutes)}）\n")
    body.append(f"覆盖范围  {run.scope_services} 个服务 / {run.scope_instances} 个实例\n")
    body.append(f"数据源    {run.source}　粒度 {run.granularity_seconds}s　耗时 {run.duration_ms / 1000:.1f}s")
    title = f"StaGuard 业务稳定性巡检　·　{run.scenario_id or 'default'}"
    return Panel(body, title=title, border_style="cyan", expand=False)


def _score_panel(report: InspectionReport) -> Panel:
    score = report.score
    style = next(style for threshold, style in SCORE_STYLE if score.total >= threshold)
    body = Text()
    body.append(f"{score.total:.1f}", style=f"bold {style}")
    body.append(" / 100　")
    body.append(f"{score.grade}\n", style=style)
    for item in score.breakdown:
        bar_full = int(round(item.score / item.weight * 16)) if item.weight else 0
        body.append(f"{item.name:<4} ", style="bold")
        body.append("█" * bar_full, style="green" if bar_full > 12 else "yellow" if bar_full > 8 else "red")
        body.append("░" * (16 - bar_full), style="grey37")
        body.append(f" {item.score:>5.1f}/{item.weight:<4g}", style="dim")
        if item.deduction:
            body.append(f" -{item.deduction:g}", style="red")
        body.append("\n")
    if score.capped_by:
        body.append(f"\n封顶生效：{score.capped_by}\n", style="bold red")
    if score.ai_adjust:
        body.append(f"AI 微调 {score.ai_adjust:+d} 分（规则算分 {score.rule_score:.1f}）\n", style="dim")
    return Panel(body, title="稳定性评分", border_style="green", expand=False)


def _level_table(report: InspectionReport) -> Table:
    """等级分布做成一行四列，而不是四行一列。

    一行四列才能一眼看出「P1 有 10 条、P2 是 0」这种分布形态；
    四行一列只看得到四个孤立的数字，反而要多花一秒去比对。
    """
    counts = report.run.level_counts
    table = Table(title="异常分布", show_header=True, header_style="bold", expand=False)
    for severity in Severity:
        table.add_column(severity.display(), justify="center")
    table.add_row(
        *[
            Text(
                str(counts.get(severity.code, 0)),
                style=LEVEL_STYLE[severity] if counts.get(severity.code, 0) else "dim",
            )
            for severity in Severity
        ]
    )
    return table


CLUSTER_ROWS = 5


def _cluster_stats_table(report: InspectionReport) -> None:
    """集群维度统计。只列有异常的集群，最多 5 行。"""
    if not report.cluster_stats:
        return
    affected = [c for c in report.cluster_stats if not c.is_healthy]
    if not affected:
        return

    table = Table(
        title=f"集群维度（{len(report.cluster_stats)} 个集群 / {len(affected)} 个有异常）",
        header_style="bold",
        expand=False,
    )
    table.add_column("集群", width=14)
    table.add_column("服务", justify="right", width=5)
    table.add_column("异常服务", justify="right", width=8)
    table.add_column("异常数", justify="right", width=7)
    table.add_column("最严重", width=6)
    table.add_column("受影响实例", justify="right", width=11)
    table.add_column("影响面", justify="right", width=7)
    table.add_column("根因簇", justify="right", width=7)

    for cluster in affected[:CLUSTER_ROWS]:
        ratio = cluster.affected_ratio
        table.add_row(
            cluster.label,
            str(cluster.service_count),
            str(cluster.affected_service_count),
            str(cluster.anomaly_count),
            Text(cluster.max_level.code, style=LEVEL_STYLE[cluster.max_level])
            if cluster.max_level
            else "—",
            f"{cluster.affected_instances} / {cluster.total_instances}",
            "—" if ratio is None else f"{ratio:.0%}",
            str(cluster.root_cause_count),
        )
    console.print(table)


def _anomaly_table(report: InspectionReport) -> None:
    active = [a for a in report.anomalies if not a.is_suppressed]
    if not active:
        console.print(Panel("未发现越线异常，各指标处于历史同时段正常区间。", border_style="green"))
        return

    table = Table(
        title=f"异常清单（Top {min(len(active), TOP_ANOMALIES)} / 共 {len(active)}）",
        show_lines=False,
        header_style="bold",
    )
    table.add_column("级别", width=4)
    table.add_column("服务 / 实例", width=26)
    table.add_column("指标", width=12)
    table.add_column("实测", justify="right", width=10)
    table.add_column("基线", justify="right", width=10)
    table.add_column("偏离", justify="right", width=9)
    table.add_column("持续", justify="right", width=8)

    for anomaly in active[:TOP_ANOMALIES]:
        table.add_row(
            Text(anomaly.level.code, style=LEVEL_STYLE[anomaly.level]),
            f"{anomaly.service}/{anomaly.instance}",
            anomaly.metric.label,
            f"{anomaly.observed:.2f}",
            "-" if anomaly.baseline_value is None else f"{anomaly.baseline_value:.2f}",
            f"{anomaly.deviation_pct:+.1f}%",
            format_duration(anomaly.duration_minutes),
        )
    console.print(table)


def _cluster_hints(report: InspectionReport) -> None:
    if not report.clusters:
        return
    body = Text()
    for cluster in report.clusters[:4]:
        finding = report.ai.finding_for(cluster.cluster_id) if report.ai else None
        label = finding.category_label if finding else "未归因"
        body.append(f"{cluster.cluster_id}  ", style="bold cyan")
        body.append(f"{cluster.primary.service}  ", style="bold")
        body.append(f"[{cluster.max_level.code}] {label}", style=LEVEL_STYLE[cluster.max_level])
        trust = f"  置信度 {finding.confidence:.0%}" if finding else ""
        body.append(f"  {cluster.size} 条异常 / {len(cluster.services)} 个服务{trust}\n", style="dim")
        body.append(f"     路径 {' → '.join(cluster.propagation_path)}\n", style="grey62")
    console.print(Panel(body, title="根因簇", border_style="magenta", expand=False))


def _trend_panel(report: InspectionReport) -> None:
    if not report.history and report.comparison is None:
        return
    body = Text()
    if report.history:
        scores = [s.score for s in report.history]
        body.append(f"{sparkline(scores)}\n", style="cyan")
        body.append("最近评分：" + " → ".join(f"{s:.0f}" for s in scores) + "\n", style="dim")
    comparison = report.comparison
    if comparison:
        style = "green" if comparison.score_trend == "up" else "red" if comparison.score_trend == "down" else "yellow"
        body.append(f"较上次 {comparison.score_delta:+.1f} 分　", style=style)
        body.append(f"新增 {len(comparison.new_anomalies)}　恢复 {len(comparison.recovered)}　"
                    f"遗留 {len(comparison.persistent)}　复发 {len(comparison.recurring)}\n")
    if report.ai and report.ai.summary:
        body.append(f"\n{truncate(report.ai.summary, 220)}\n", style="italic")
    console.print(Panel(body, title="稳定性趋势", border_style="blue", expand=False))


def _footer(report: InspectionReport, verbose: bool) -> None:
    meta = report.ai_meta
    style = "yellow" if meta.degraded else "dim"
    line = (
        f"AI：{meta.status_label}　token {meta.total_tokens}　耗时 {meta.latency_ms}ms　"
        f"阶段耗时 " + " / ".join(f"{s.label} {s.duration_ms}ms" for s in report.run.stages[:6])
    )
    console.print(Text(line, style=style))
    failures = [s for s in report.run.stages if s.error]
    for stage in failures:
        console.print(Text(f"⚠️ 阶段 {stage.label} 失败：{stage.error}", style="bold red"))
    if verbose:
        console.print(Text(f"采集窗口起始 {format_ts(report.run.window.start)}", style="dim"))
