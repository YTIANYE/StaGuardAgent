"""不带场景的巡检（「当前水位」）端到端测试。

这一组锁的是两条容易被静默破坏的约束：

1. **不带场景也能巡检**——`scenario_id` 的职责是回放历史故障切片；
   定时巡检、接口手动触发问的是「现在什么水位」，得由 `live_dataset` 回答，
   而不是靠一个永远不存在的 dataset_id；
2. **「没取到数据」不能被说成「一切正常」**——一条数据都没拿到时，
   控制台和报告都必须写「无法评估 + 原因」，且报告仍然落盘留痕。
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from staguard.config import Settings
from staguard.dataset.generator import ARCHIVE_DATASET
from staguard.dataset.store_file import FileMetricStore
from staguard.dataset.sync import build_all
from staguard.models import InspectionReport, InspectionRun, RunStatus, ScoreResult, TimeWindow
from staguard.orchestrator import InspectionOrchestrator
from staguard.report import render_markdown
from staguard.store import Database, Repository

pytestmark = pytest.mark.slow

MISSING_DATASET = "nope"


@pytest.fixture(scope="module")
def prepared(settings: Settings) -> Repository:
    database = Database(settings.app.db_url)
    database.init_schema()
    repo = Repository(database)
    store = FileMetricStore(settings.app.data_dir / "metrics")
    if repo.count_points(ARCHIVE_DATASET) == 0 or not store.exists("S1"):
        build_all(settings, repo)
    return repo


@pytest.fixture(scope="module")
def offline(settings: Settings) -> Settings:
    """强制走规则兜底路径，让断言不依赖外部大模型。"""
    app = settings.app.model_copy(update={"llm_enabled": False})
    return settings.model_copy(update={"app": app})


@pytest.fixture(scope="module")
def broken_live(offline: Settings) -> Settings:
    """把「当前水位」指向一个不存在的数据集，模拟采集彻底拿不到数据。"""
    app = offline.app.model_copy(update={"live_dataset": MISSING_DATASET})
    return offline.model_copy(update={"app": app})


def test_adhoc_inspection_reads_live_dataset(offline: Settings, prepared: Repository):
    """不带场景的巡检必须能取到数据并给出可评估的结论。"""
    report = InspectionOrchestrator(offline, prepared).run(notify=False)

    assert report.run.status is not RunStatus.FAILED
    assert report.run.error is None
    assert report.score.total > 0, "「当前水位」巡检不应产出 0 分"
    assert report.run.scenario_id is None
    # 当前水位切片默认是正常态：不该报出 P1/P2（与 S0 的口径一致）
    assert report.run.level_counts.get("P1", 0) == 0
    assert report.run.level_counts.get("P2", 0) == 0


def test_adhoc_report_is_written_under_adhoc_dir(offline: Settings, prepared: Repository):
    """临时巡检的报告落在 reports/adhoc/，文件名是 run_id。"""
    report = InspectionOrchestrator(offline, prepared).run(notify=False)
    path = offline.app.report_dir / "adhoc" / f"{report.run.run_id}.md"

    assert path.exists(), f"临时巡检未写报告：{path}"
    assert "多粒度巡检统计" in path.read_text(encoding="utf-8")


def test_adhoc_detection_dataset_is_run_scoped(offline: Settings, prepared: Repository):
    """取数数据集与检测数据集必须是两个：检测数据按 run_id 隔离。

    这一条是防止「把 source_dataset 与 detect_dataset 合并成一个变量」的回归——
    合并之后定时巡检会反复写进 `live_dataset`，下一轮读到上一轮的残留。
    """
    report = InspectionOrchestrator(offline, prepared).run(notify=False)

    assert report.run.run_id != offline.app.live_dataset
    assert prepared.count_points(report.run.run_id) > 0, "检测数据应按 run_id 入库"


def test_same_window_is_not_recurrence(offline: Settings, prepared: Repository):
    """同一个时间窗口内的历史一律不算复发——复发是「跨窗口重现」。

    挡的是两类误报：

    1. 不带场景的巡检读的是 `live_dataset` 切片，与同名场景读的是同一份数据，
       只按 scenario_id 排除，两者会互相认成复发；
    2. 不同场景（S1 依赖故障 / S3 流量突增）共享一批 `服务.指标` token，
       Jaccard 相似度过线，于是 S3 的报告里写着「上次的处置并未根治」。
    """
    orchestrator = InspectionOrchestrator(offline, prepared)

    reports = [
        ("S1", orchestrator.run(scenario_id="S1", notify=False)),
        ("当前水位", orchestrator.run(notify=False)),
        ("S0", orchestrator.run(scenario_id="S0", notify=False)),
        ("S3", orchestrator.run(scenario_id="S3", notify=False)),
    ]
    for label, report in reports:
        flagged = [a.subject for a in report.anomalies if a.is_recurring]
        assert not flagged, f"{label} 被误判为复发：{flagged}"

    assert reports[1][1].run.dataset_id == offline.app.live_dataset


def test_cross_window_history_still_counts(prepared: Repository):
    """别把复发功能一起关掉：**更早窗口**的历史仍参与复发判定。"""
    from datetime import datetime, timedelta

    from staguard.models import (
        Anomaly,
        AnomalyCluster,
        Dimension,
        InspectionRun,
        MetricName,
        Severity,
        TimeWindow,
    )
    from staguard.utils.timeutil import from_epoch, to_epoch

    anchor = from_epoch(to_epoch(datetime(2026, 9, 25, 10, 0)))
    earlier = anchor - timedelta(hours=2)
    fake_run = InspectionRun(
        run_id="run-earlier-S9",
        scenario_id="S9",
        dataset_id="S9",
        window=TimeWindow(start=earlier - timedelta(minutes=30), end=earlier),
        started_at=earlier,
        finished_at=earlier,
    )
    member = Anomaly(
        anomaly_id="RES-02|payment-svc|payment-svc-0|mem_usage",
        rule_id="RES-02",
        rule_name="内存占用过高",
        dimension=Dimension.RESOURCE,
        service="payment-svc",
        instance="payment-svc-0",
        metric=MetricName.MEM_USAGE,
        level=Severity.P1,
        observed=95.0,
        first_seen=earlier - timedelta(minutes=10),
        last_seen=earlier,
    )
    prepared.save_run(fake_run)
    prepared.save_clusters(
        fake_run.run_id,
        [AnomalyCluster(cluster_id="CLUS-S9", primary=member, members=[member],
                        services=["payment-svc"])],
    )

    try:
        current_window = anchor.isoformat(timespec="seconds")
        rows = prepared.recent_clusters(limit=200, exclude_window_end=current_window)
        matched = [row for row in rows if row["run_id"] == fake_run.run_id]
        assert matched, "更早窗口的历史被误排除，复发识别会彻底失效"
    finally:
        _drop_run(prepared, fake_run.run_id)


def _drop_run(repo: Repository, run_id: str) -> None:
    """删掉测试造出来的 run 与簇。

    端到端测试用的是真实数据库（这是本项目的既有约定），伪造的历史必须自己收走：
    留着它，后面任何一份报告的复发判定都会命中这条假事件。
    """
    import sqlalchemy as sa

    from staguard.store.schema import clusters, runs

    with repo.db.begin() as conn:
        conn.execute(sa.delete(clusters).where(clusters.c.run_id == run_id))
        conn.execute(sa.delete(runs).where(runs.c.run_id == run_id))


def test_metric_points_has_planner_stats(prepared: Repository):
    """两百多万行的大表必须有查询计划统计信息（`sqlite_stat1`）。

    实测：没有统计信息时 SQLite 会给基线查询选错执行计划，单次巡检 **46 秒**；
    有了之后 **5 秒**——而日志里没有任何异常、结果也完全正确，
    只能靠耗时发现。所以这条断言值一个测试。
    """
    import sqlalchemy as sa

    with prepared.db.connect() as conn:
        rows = conn.execute(
            sa.text("select count(*) from sqlite_stat1 where tbl = 'metric_points'")
        ).scalar()

    assert rows, "metric_points 缺少查询计划统计信息（见 Database.optimize）"


def test_orphan_clusters_are_not_history(prepared: Repository):
    """没有对应 run 记录的簇不参与复发识别。

    表结构漂移自愈会重建单张表，重建 runs 之后 clusters 里会留下孤立行；
    它们没有窗口、没有切片，却能被签名匹配上——不挡住的话，
    恢复出厂设置后的第一份报告就写着「历史复发」。
    """
    from datetime import datetime, timedelta

    from staguard.models import (
        Anomaly,
        AnomalyCluster,
        Dimension,
        MetricName,
        Severity,
    )

    anchor = datetime(2026, 9, 25, 10, 0)
    member = Anomaly(
        anomaly_id="BIZ-01|order-svc|order-svc-0|success_rate",
        rule_id="BIZ-01",
        rule_name="接口成功率下跌",
        dimension=Dimension.BUSINESS,
        service="order-svc",
        instance="order-svc-0",
        metric=MetricName.SUCCESS_RATE,
        level=Severity.P2,
        observed=90.0,
        first_seen=anchor - timedelta(minutes=10),
        last_seen=anchor,
    )
    prepared.save_clusters(
        "run-ghost",
        [AnomalyCluster(cluster_id="CLUS-GHOST", primary=member, members=[member],
                        services=["order-svc"])],
    )

    try:
        rows = prepared.recent_clusters(limit=50)
        assert all(row["run_id"] != "run-ghost" for row in rows), "孤立簇不应进历史"
    finally:
        _drop_run(prepared, "run-ghost")


def test_missing_live_dataset_reports_unable_to_evaluate(
    broken_live: Settings, prepared: Repository
):
    """取不到数据时必须：状态失败、原因可执行、报告照写。"""
    report = InspectionOrchestrator(broken_live, prepared).run(notify=False)
    markdown = render_markdown(report)

    assert report.run.status is RunStatus.FAILED
    assert report.score.grade == "无法评估"
    assert MISSING_DATASET in (report.run.error or ""), "失败原因要指出是哪个数据集缺失"
    assert "可用数据集" in (report.run.error or ""), "失败原因要给出可执行的下一步"

    assert "无法评估" in markdown
    assert "未发现越线异常" not in markdown, "没取到数据不能说成「未发现越线异常」"
    assert "正常区间" not in markdown

    path = broken_live.app.report_dir / "adhoc" / f"{report.run.run_id}.md"
    assert path.exists(), "「无法评估」的报告同样要落盘留痕"


def test_missing_live_dataset_console_does_not_claim_normal(
    broken_live: Settings, prepared: Repository, monkeypatch: pytest.MonkeyPatch
):
    """控制台不能把「没数据」渲染成绿色「各指标正常」。"""
    from staguard.report import console as console_module

    buffer = io.StringIO()
    monkeypatch.setattr(console_module, "console", Console(file=buffer, width=160))

    report = InspectionOrchestrator(broken_live, prepared).run(notify=False)
    console_module.render(report, verbose=False)
    output = buffer.getvalue()

    assert "无法评估" in output
    assert "未发现越线异常" not in output
    assert "正常区间" not in output


def test_clean_console_still_says_normal(monkeypatch: pytest.MonkeyPatch):
    """对照组：真的都正常时，那句话还得说——修的是误用，不是删掉文案。"""
    from staguard.report import console as console_module

    buffer = io.StringIO()
    monkeypatch.setattr(console_module, "console", Console(file=buffer, width=160))

    console_module._anomaly_table(_empty_report())
    markdown = render_markdown(_empty_report())

    assert "未发现越线异常" in buffer.getvalue()
    assert "本次巡检未发现越线异常" in markdown
    assert "无法评估" not in markdown


def _empty_report(*, error: str | None = None) -> InspectionReport:
    """一条异常都没有的报告，用于对照「真正常」与「没数据」两种措辞。"""
    from datetime import datetime, timedelta

    anchor = datetime(2026, 9, 25, 10, 0)
    run = InspectionRun(
        run_id="run-test",
        window=TimeWindow(start=anchor - timedelta(minutes=30), end=anchor),
        started_at=anchor,
        finished_at=anchor,
        status=RunStatus.SUCCEEDED,
        error=error,
    )
    return InspectionReport(
        run=run,
        score=ScoreResult(rule_score=100.0, ai_adjust=0, total=100.0, grade="A 优秀"),
    )
