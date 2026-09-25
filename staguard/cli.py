"""命令行入口。

四个入口共用同一个编排器，不存在「CLI 走一套、接口走另一套」的分叉：

    staguard gen-data     初始化模拟数据集（归档入库 + 场景文件）
    staguard run          单次巡检
    staguard schedule     定时巡检（常驻）
    staguard serve        启动 HTTP 服务（含健康探针与手动触发）
    staguard mock-monitor 启动模拟监控数据接口（HTTP 采集通道的对端）
    staguard eval         跑归因评测集，量化 AI 归因质量
    staguard sample       导出报告样例（按 AI 模式区分文件名，便于对比）
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import build_settings
from .dataset.sync import build_all
from .orchestrator import InspectionOrchestrator
from .report import render_markdown
from .store import Database, Repository
from .utils.logging import setup_logging
from .utils.timeutil import format_ts, parse_iso

app = typer.Typer(
    name="staguard",
    help="StaGuardAgent - AI 驱动的业务稳定性自动化巡检 Agent",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
logger = logging.getLogger(__name__)


def _bootstrap(config_dir: Path | None, log_level: str, log_format: str):
    setup_logging(log_level, log_format)
    settings = build_settings(config_dir) if config_dir else build_settings()
    database = Database(settings.app.db_url)
    database.init_schema()
    return settings, Repository(database)


@app.command("gen-data")
def gen_data(
    config_dir: Annotated[Path | None, typer.Option("--config-dir", help="配置目录")] = None,
    archive: Annotated[bool, typer.Option("--archive/--no-archive", help="是否重建长期归档历史")] = True,
    files: Annotated[bool, typer.Option("--files/--no-files", help="是否导出场景数据文件")] = True,
    log_level: Annotated[str, typer.Option("--log-level")] = "INFO",
) -> None:
    """生成模拟巡检数据集：14 天归档历史入库，7 个故障场景导出为结构化文件。"""
    settings, repo = _bootstrap(config_dir, log_level, "console")
    started = time.perf_counter()
    with console.status("[bold cyan]生成模拟数据集..."):
        report = build_all(settings, repo, archive=archive, files=files)
    elapsed = time.perf_counter() - started

    table = Table(title="数据集生成完成", header_style="bold")
    table.add_column("数据集")
    table.add_column("点数", justify="right")
    table.add_column("时间范围")
    for dataset_id, points, min_epoch in repo.datasets():
        from .utils.timeutil import from_epoch

        table.add_row(dataset_id, f"{points:,}", format_ts(from_epoch(min_epoch)))
    for result in report.results:
        table.add_row(result.dataset_id + "（归档）", f"{result.points:,}", f"{result.start:%m-%d %H:%M}~{result.end:%m-%d %H:%M}")
    console.print(table)

    from .dataset.store_file import FileMetricStore

    store = FileMetricStore(settings.app.data_dir / "metrics")
    console.print(f"[green]场景文件 {len(store.datasets())} 个 -> {store.root}[/green]")
    console.print(f"[dim]耗时 {elapsed:.1f}s，数据库 {settings.app.db_file()} "
                  f"（{settings.app.db_file().stat().st_size / 1e6:.1f} MB）[/dim]")


@app.command("run")
def run_inspection_cmd(
    scenario: Annotated[str | None, typer.Option("--scenario", "-s", help="场景编号，如 S1")] = None,
    source: Annotated[str | None, typer.Option("--source", help="数据源：file / http")] = None,
    window_end: Annotated[str | None, typer.Option("--window-end", help="巡检窗口结束时刻（ISO）")] = None,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="以 JSON 输出完整报告")] = False,
    log_level: Annotated[str, typer.Option("--log-level")] = "WARNING",
) -> None:
    """执行一次巡检，输出控制台报告并导出 Markdown。"""
    settings, repo = _bootstrap(config_dir, log_level, "console")
    orchestrator = InspectionOrchestrator(settings, repo)
    anchor = parse_iso(window_end) if window_end else None
    report = orchestrator.run(scenario_id=scenario, window_end=anchor, source_name=source)

    if json_out:
        console.print_json(data=report.model_dump(mode="json"))
        return

    from .report import console as console_render

    console_render.render(report, verbose=True)
    if report.run.score is not None:
        path = settings.app.report_dir / (scenario or "adhoc") / f"{report.run.run_id}.md"
        console.print(f"[dim]Markdown 报告：{path}[/dim]")


@app.command("report")
def show_report(
    run_id: Annotated[str, typer.Argument(help="巡检编号")],
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """查看某次历史巡检的 Markdown 报告。"""
    settings, repo = _bootstrap(config_dir, "WARNING", "console")
    stored = repo.load_report(run_id)
    if not stored:
        console.print(f"[red]未找到巡检记录 {run_id}[/red]")
        raise typer.Exit(code=1)
    console.print(stored["markdown"])


@app.command("runs")
def list_runs(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """列出历史巡检记录。"""
    settings, repo = _bootstrap(config_dir, "WARNING", "console")
    rows = repo.list_runs(limit)
    if not rows:
        console.print("[yellow]暂无巡检记录，先执行 `staguard run`[/yellow]")
        return
    table = Table(title=f"最近 {len(rows)} 次巡检", header_style="bold")
    for column in ("编号", "场景", "窗口", "状态", "评分", "异常", "AI", "耗时"):
        table.add_column(column, justify="right" if column in ("评分", "异常", "耗时") else "left")
    for row in rows:
        table.add_row(
            row["run_id"],
            row["scenario_id"] or "-",
            row["window_end"][5:16].replace("T", " "),
            row["status"],
            f"{row['score']:.1f}" if row["score"] is not None else "-",
            str(row["anomaly_count"]),
            "[yellow]降级[/yellow]" if row["ai_degraded"] else "正常",
            f"{row['duration_ms']}ms",
        )
    console.print(table)


@app.command("stats")
def stats(config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None) -> None:
    """查看数据库与规则配置概览。"""
    settings, repo = _bootstrap(config_dir, "WARNING", "console")
    info = repo.stats()
    console.print(f"[bold]数据库[/bold] {info['database']}")
    console.print(f"巡检 {info['runs']} 次　异常 {info['anomalies']} 条　指标点 {info['metric_points']:,}")
    table = Table(title="已注册规则", header_style="bold")
    table.add_column("ID")
    table.add_column("名称")
    table.add_column("维度")
    table.add_column("级别")
    table.add_column("实现")
    orchestrator = InspectionOrchestrator(settings, repo)
    for item in orchestrator.engine.describe():
        table.add_row(
            item["id"], item["name"], item["dimension_label"],
            "/".join(item["levels"]), item["implementation"],
        )
    console.print(table)
    console.print(f"[dim]数据集：{', '.join(d[0] for d in repo.datasets())}[/dim]")


@app.command("mock-monitor")
def mock_monitor(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8090,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """启动模拟监控数据接口（供 HTTP 采集通道使用）。"""
    import uvicorn

    build_settings(config_dir) if config_dir else build_settings()
    setup_logging("INFO", "console")
    console.print(f"[green]模拟监控接口：http://{host}:{port}/docs[/green]")
    uvicorn.run("staguard.mockapi.app:app", host=host, port=port, log_level="warning")


@app.command("serve")
def serve(
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port")] = None,
    with_scheduler: Annotated[bool, typer.Option("--scheduler/--no-scheduler", help="同时启动定时巡检")] = False,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """启动 HTTP 服务：健康探针、手动触发巡检、报告查询。"""
    import uvicorn

    settings, _ = _bootstrap(config_dir, "INFO", "console")
    from .api import create_app

    if with_scheduler:
        from .scheduler import InspectionScheduler

        InspectionScheduler(settings).start()
        console.print("[green]定时巡检调度器已启动[/green]")
    target_host = host or settings.app.api_host
    target_port = port or settings.app.api_port
    console.print(f"[green]StaGuard API：http://{target_host}:{target_port}/docs[/green]")
    uvicorn.run(create_app(settings), host=target_host, port=target_port, log_level="info")


@app.command("schedule")
def schedule(
    interval_minutes: Annotated[int, typer.Option("--interval", "-i", help="巡检间隔（分钟）")] = 30,
    cron: Annotated[str | None, typer.Option("--cron", help="cron 表达式，如 '*/15 * * * *'")] = None,
    scenario: Annotated[str | None, typer.Option("--scenario")] = None,
    source: Annotated[str | None, typer.Option("--source", help="数据源：file / http")] = None,
    run_now: Annotated[bool, typer.Option("--run-now/--no-run-now", help="启动时立即跑一次")] = True,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """以常驻进程方式定时巡检（K8s 里可选 CronJob 或常驻 Deployment 两种形态）。"""
    from .scheduler import InspectionScheduler

    settings, repo = _bootstrap(config_dir, "INFO", "console")
    scheduler = InspectionScheduler(settings, repo, scenario_id=scenario, source_name=source)
    scheduler.start(interval_minutes=interval_minutes, cron=cron, run_immediately=run_now)

    if cron:
        console.print(f"[green]已按 cron `{cron}` 调度巡检，Ctrl+C 退出[/green]")
    else:
        console.print(f"[green]已按每 {interval_minutes} 分钟调度巡检，Ctrl+C 退出[/green]")

    stop = {"flag": False}

    def _handle(signum, frame) -> None:  # noqa: ANN001, ARG001
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        scheduler.shutdown()
        console.print("[yellow]调度器已停止[/yellow]")


@app.command("sample")
def export_samples(
    output_dir: Annotated[Path, typer.Option("--out", "-o", help="样例输出目录")] = Path("docs/samples"),
    scenarios: Annotated[str, typer.Option("--scenarios", help="逗号分隔的场景编号")] = "S0,S1,S3,S4,S6",
    source: Annotated[str | None, typer.Option("--source")] = None,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
) -> None:
    """导出巡检报告样例。

    文件名带上 AI 模式后缀（`-llm` / `-rule-fallback`），
    这样「规则兜底版本」和「真实模型版本」可以并存，
    直接 diff 两份报告就能看出接入大模型到底带来了什么差异——
    比口头说「接了模型效果更好」有说服力得多。
    """
    settings, repo = _bootstrap(config_dir, "WARNING", "console")
    output_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = InspectionOrchestrator(settings, repo)
    table = Table(title=f"报告样例 -> {output_dir}", header_style="bold")
    table.add_column("场景")
    table.add_column("文件")
    table.add_column("评分", justify="right")
    table.add_column("异常", justify="right")
    table.add_column("AI 模式")

    for raw in scenarios.split(","):
        scenario_id = raw.strip()
        if not scenario_id:
            continue
        report = orchestrator.run(scenario_id=scenario_id, source_name=source, notify=False)
        mode = "rule-fallback" if report.ai_meta.degraded else "llm"
        path = output_dir / f"{scenario_id}-{mode}.md"
        path.write_text(render_markdown(report), encoding="utf-8")
        table.add_row(
            scenario_id, path.name, f"{report.score.total:.1f}",
            str(report.run.anomaly_count),
            f"[yellow]{mode}[/yellow]" if mode == "rule-fallback" else f"[green]{mode}[/green]",
        )
    console.print(table)
    if any("rule-fallback" in p.name for p in output_dir.glob("*.md")):
        console.print(
            "[dim]提示：配置 STAGUARD_LLM_API_KEY 后重跑本命令，会额外产出 -llm 版本用于对比。[/dim]"
        )


@app.command("eval")
def eval_attribution(
    source: Annotated[str | None, typer.Option("--source")] = None,
    config_dir: Annotated[Path | None, typer.Option("--config-dir")] = None,
    log_level: Annotated[str, typer.Option("--log-level")] = "WARNING",
) -> None:
    """跑归因评测集：逐场景巡检并核对根因类别与级别，输出准确率。"""
    from .evaluation import run_evaluation

    settings, repo = _bootstrap(config_dir, log_level, "console")
    report = run_evaluation(settings, repo, source_name=source)
    console.print(report.render())


if __name__ == "__main__":
    app()
