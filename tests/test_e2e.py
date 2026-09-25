"""端到端测试：真实数据集 + 真实数据库 + 完整编排链路。

单元测试保证「每个零件是对的」，这一组保证「装起来是能跑的」。
两者都不能少：只做单元测试会漏掉集成问题，只做端到端又定位不到具体原因。

这组测试会真实生成 14 天归档 + 7 个场景数据（约 20 秒），
所以单独标记为 slow；`make test` 全跑，`make test-fast` 跳过。
"""

from __future__ import annotations

import pytest

from staguard.config import Settings
from staguard.dataset.generator import ARCHIVE_DATASET
from staguard.dataset.store_file import FileMetricStore
from staguard.dataset.sync import build_all
from staguard.evaluation import run_evaluation
from staguard.orchestrator import InspectionOrchestrator
from staguard.store import Database, Repository

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def prepared(settings: Settings) -> Repository:
    """确保数据集就绪。已生成过就复用，避免每次跑测试都重来一遍。"""
    database = Database(settings.app.db_url)
    database.init_schema()
    repo = Repository(database)
    store = FileMetricStore(settings.app.data_dir / "metrics")
    if repo.count_points(ARCHIVE_DATASET) == 0 or not store.exists("S1"):
        build_all(settings, repo)
    return repo


def test_all_scenarios_meet_attribution_baseline(settings: Settings, prepared: Repository):
    """七个场景的根因命中率与级别命中率必须达标，且正常态不能误报。

    这里断言的是「规则兜底路径」的质量——真实模型接入后只会更好，
    所以这个基线同时也是「AI 挂了也不会退化成不可用」的保证。
    """
    report = run_evaluation(settings, prepared)

    assert report.root_cause_accuracy >= 0.85, f"根因命中率过低：{report.render()}"
    assert report.level_accuracy >= 0.85, f"级别命中率过低：{report.render()}"
    assert report.false_positive_rate == 0.0, f"正常态被误判成故障：{report.render()}"


def test_clean_scenario_stays_quiet(settings: Settings, prepared: Repository):
    """对照组：正常态不能出现 P1/P2。

    误报的代价比漏报更高——告警疲劳会让真正的 P1 一起被忽略。
    """
    report = InspectionOrchestrator(settings, prepared).run(scenario_id="S0", notify=False)
    assert report.run.level_counts.get("P1", 0) == 0
    assert report.run.level_counts.get("P2", 0) == 0
    assert report.score.total >= 90, "干净数据的评分应接近满分"


def test_dependency_cascade_identifies_downstream_root_cause(settings: Settings, prepared: Repository):
    """S1：级联故障的根因必须落在最下游的银行渠道，且受影响面覆盖整条链。"""
    report = InspectionOrchestrator(settings, prepared).run(scenario_id="S1", notify=False)
    assert report.clusters, "级联故障必须能聚出根因簇"
    cluster = report.clusters[0]
    assert cluster.primary.service == "bank-channel"
    assert {"gateway", "order-svc", "payment-svc"} <= set(cluster.services)
    assert report.score.capped_by is None or report.score.total <= 60.0
    assert report.run.level_counts.get("P1", 0) > 0


def test_slow_fault_needs_long_lookback(settings: Settings, prepared: Repository):
    """S6：内存泄漏只在 24 小时观察窗下才看得见 —— 验证采集范围由规则反推。"""
    report = InspectionOrchestrator(settings, prepared).run(scenario_id="S6", notify=False)
    rules = {a.rule_id for a in report.anomalies}
    assert "RES-03" in rules, "内存增长趋势规则必须命中"
    trend = next(a for a in report.anomalies if a.rule_id == "RES-03")
    assert trend.duration_minutes > 600, "趋势判定应基于小时级观察窗而非 30 分钟窗口"


def test_inspection_is_reproducible(settings: Settings, prepared: Repository):
    """同一份数据跑两次必须得到同一份结论。

    这是「多次巡检对比」能成立的前提：如果同一时刻重跑会得到不同的分数，
    趋势对比就没有任何意义。
    """
    orchestrator = InspectionOrchestrator(settings, prepared)
    first = orchestrator.run(scenario_id="S2", notify=False)
    second = orchestrator.run(scenario_id="S2", notify=False)

    assert first.score.total == second.score.total
    assert first.run.level_counts == second.run.level_counts
    assert {a.anomaly_id for a in first.anomalies} == {a.anomaly_id for a in second.anomalies}
    assert [c.signature() for c in first.clusters] == [c.signature() for c in second.clusters]


def test_both_sources_produce_identical_verdicts(settings: Settings, prepared: Repository):
    """文件通道与 HTTP 通道的结果必须完全一致。

    这正是「两条接入通道共用同一份底层数据」的价值：
    一致性是被构造出来的，不是靠嘴保证的。HTTP 通道不可用时跳过。
    """
    from staguard.collector import HttpMetricSource

    source = HttpMetricSource(settings.app.mock_api_base_url, timeout_s=5.0, trust_env=False)
    if not source.ping():
        pytest.skip("模拟监控接口未启动，跳过双通道一致性验证（make monitor 可启动）")

    orchestrator = InspectionOrchestrator(settings, prepared)
    from_file = orchestrator.run(scenario_id="S5", source_name="file", notify=False)
    from_http = orchestrator.run(scenario_id="S5", source_name="http", notify=False)

    assert from_file.score.total == from_http.score.total
    assert from_file.run.level_counts == from_http.run.level_counts


def test_report_contains_required_sections(settings: Settings, prepared: Repository):
    """报告必须包含题目要求的所有章节，且不留未渲染的模板占位符。"""
    import re

    report = InspectionOrchestrator(settings, prepared).run(scenario_id="S1", notify=False)
    from staguard.report import render_markdown

    markdown = render_markdown(report)
    for section in ("巡检概览", "风险总结", "异常清单", "根因分析", "修复建议", "稳定性趋势", "附录"):
        assert section in markdown, f"报告缺少章节：{section}"
    assert not re.search(r"\{[a-z_]+\}", markdown), "报告里出现了未渲染的模板占位符"
    assert str(report.score.total) in markdown
