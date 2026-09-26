"""巡检编排器。

把十个阶段串成一条可观测、可降级、可重跑的流水线：

    collect → normalize → store → baseline → rule_engine → aggregate
            → score → ai_analysis → report → notify

三条工程约束，每一条都是为了「巡检本身不能成为新的故障源」：

1. **阶段隔离**：任一阶段抛异常只让该阶段标记失败，后续阶段在数据允许的范围内继续。
   一条规则的 bug、一次 LLM 超时，都不应该让整轮巡检白跑；
2. **全程留痕**：每个阶段的耗时、状态、失败原因都记进 run，报告附录里原样展示。
   线上排查「为什么今天没报警」时，这份阶段流水就是唯一的抓手；
3. **状态诚实**：`succeeded` / `partial` / `failed` 如实反映结果，
   绝不用「部分成功」伪装成「成功」——巡检结果的可信度是它的全部价值。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .ai import AIAnalyzer
from .collector import CollectionResult, CollectRequest, MetricSource, build_source
from .config import Settings, build_settings
from .dataset import build_change_events
from .dataset.generator import ARCHIVE_DATASET, required_history_minutes
from .detect import aggregate, build_clusters, build_granularity_stats
from .models import (
    AIAnalysis,
    AIMeta,
    Anomaly,
    AnomalyCluster,
    BaselineSet,
    InspectionReport,
    InspectionRun,
    MetricName,
    RunStatus,
    ScoreResult,
    StageRecord,
    StageStatus,
    TimeWindow,
)
from .normalize import NormalizeConfig, clean
from .notify.webhook import AlertNotifier
from .report import build_comparison, build_history, render_markdown
from .rules import EvalContext, RuleEngine, build_context
from .scoring import ScoreCalculator
from .store import Database, Repository
from .utils.timeutil import now, parse_iso, run_id_for, to_epoch

logger = logging.getLogger(__name__)

CHANGES_FILE = "changes.yaml"


class StageTracker:
    """按阶段记录状态与耗时。

    阶段异常一律**只记录、不向上抛**：巡检报告的一部分失效（例如变更事件没同步上）
    不应该让整份报告变成空白。需要中断时由调用方在自己的阶段外显式处理，
    而不是在通用上下文管理器里留一个开关——那只会变成读代码时的干扰项。
    """

    def __init__(self, run: InspectionRun) -> None:
        self.run = run

    @contextmanager
    def stage(self, name: str) -> Iterator[StageRecord]:
        record = StageRecord(name=name, status=StageStatus.RUNNING)
        started = time.perf_counter()
        try:
            yield record
            if record.status is StageStatus.RUNNING:
                record.status = StageStatus.SUCCEEDED
        except Exception as exc:  # noqa: BLE001 - 阶段隔离是刻意设计
            record.status = StageStatus.FAILED
            record.error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("巡检阶段 %s 执行失败", name)
        finally:
            record.duration_ms = int((time.perf_counter() - started) * 1000)
            self.run.upsert_stage(record)


class InspectionOrchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        repo: Repository | None = None,
        source: MetricSource | None = None,
        analyzer: AIAnalyzer | None = None,
    ) -> None:
        self.settings = settings or build_settings()
        if repo is None:
            database = Database(self.settings.app.db_url)
            database.init_schema()
            repo = Repository(database)
        self.repo = repo
        self.source = source
        self.engine = RuleEngine(self.settings)
        self.scorer = ScoreCalculator(self.settings)
        self.analyzer = analyzer or AIAnalyzer(self.settings)

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        scenario_id: str | None = None,
        window_end: datetime | None = None,
        source_name: str | None = None,
        run_id: str | None = None,
        notify: bool = True,
    ) -> InspectionReport:
        settings = self.settings
        anchor = window_end or self._resolve_window_end()
        window = TimeWindow(
            start=anchor - timedelta(minutes=settings.window_minutes),
            end=anchor,
        )
        run_id = run_id or run_id_for(anchor, scenario_id)
        source = self.source or build_source(settings, source_name)
        detect_dataset = scenario_id or run_id

        run = InspectionRun(
            run_id=run_id,
            scenario_id=scenario_id,
            window=window,
            granularity_seconds=settings.granularity_seconds,
            started_at=now(),
            source=source.name,
            scope_services=len(settings.topology.names()),
            scope_instances=len(settings.topology.all_instances()),
        )
        tracker = StageTracker(run)
        logger.info("开始巡检 %s（场景 %s / 数据源 %s）", run_id, scenario_id or "-", source.name)

        collected: CollectionResult | None = None
        context: EvalContext | None = None
        anomalies: list[Anomaly] = []
        clusters: list[AnomalyCluster] = []
        analysis: AIAnalysis | None = None
        meta = AIMeta()
        score: ScoreResult | None = None
        baselines = BaselineSet()
        comparison = None
        history: list = []

        # ---------------------------------------------------------- 1. 采集
        with tracker.stage("collect") as stage:
            request = CollectRequest(
                dataset_id=detect_dataset,
                window_end=anchor,
                window_minutes=settings.window_minutes,
                history_minutes=self._history_minutes(scenario_id),
                granularity_seconds=settings.granularity_seconds,
            )
            collected = source.fetch(request)
            stage.detail = collected.summary()
            if collected.warnings:
                stage.status = StageStatus.DEGRADED
                stage.detail = "；".join(collected.warnings)

        # ---------------------------------------------------------- 2. 清洗
        normalized = None
        if collected is not None and collected.points:
            with tracker.stage("normalize") as stage:
                normalized = clean(
                    collected.points,
                    NormalizeConfig(
                        granularity_seconds=settings.granularity_seconds,
                        max_gap_buckets=3,
                    ),
                    declared_units=collected.declared_units,
                    clip_window=TimeWindow(start=request.fetch_start, end=request.fetch_end),
                )
                stage.detail = (
                    f"{len(normalized.points)} 个有效点 / {len(normalized.stats)} 条时序；"
                    f"缺失率 {normalized.quality.missing_ratio:.2%}，置信度 {normalized.quality.confidence.label}"
                )

        # ---------------------------------------------------------- 3. 入库
        if normalized is not None and normalized.points:
            with tracker.stage("store") as stage:
                inserted = self._persist_points(detect_dataset, normalized.points, stage)
                self._sync_changes(anchor, collected)
                stage.detail = f"数据集 {detect_dataset} 就绪（{inserted} 个指标点）"

        self.repo.save_run(run)

        if collected is None or normalized is None or not normalized.points:
            return self._finish_without_data(run, tracker, "采集或清洗阶段未获得有效数据")

        # ---------------------------------------------------------- 4~6. 基线 / 规则 / 聚合
        with tracker.stage("baseline") as stage:
            context = build_context(
                settings, self.repo, window,
                detect_dataset=detect_dataset,
                archive_dataset=ARCHIVE_DATASET,
                scenario_id=scenario_id,
            )
            baselines = context.provider.collect(
                context.instances(), list(MetricName)
            )
            stage.detail = (
                f"计算 {len(baselines.baselines)} 条基线，动态基线覆盖率 "
                f"{baselines.dynamic_ratio():.0%}（归档 {self.repo.count_points(ARCHIVE_DATASET)} 点）"
            )
            if baselines.dynamic_ratio() < 0.5:
                stage.status = StageStatus.DEGRADED
                stage.detail += "；超过一半序列回退静态阈值，结论置信度下降"

        findings: list = []
        with tracker.stage("rule_engine") as stage:
            findings = self.engine.evaluate(context)
            stage.detail = (
                f"{len(self.engine.rules)} 条规则命中 {len(findings)} 项原始发现"
                f"（{self.engine.last_elapsed_ms}ms）"
            )
            if self.engine.failures:
                stage.status = StageStatus.DEGRADED
                stage.detail += f"；{len(self.engine.failures)} 条规则执行失败"

        with tracker.stage("aggregate") as stage:
            result = aggregate(findings, settings.topology)
            anomalies = result.anomalies
            clusters = build_clusters(anomalies, settings.topology, window)
            service_stats, cluster_stats = build_granularity_stats(
                settings.topology, anomalies, clusters
            )
            run.anomaly_count = len(result.active)
            run.count_levels([a.level for a in result.active])
            stage.detail = (
                f"{len(anomalies)} 条异常（抑制 {len(result.suppressed)} 条）"
                f"聚成 {len(clusters)} 个根因簇"
                f"；多粒度统计 {len(service_stats)} 个服务 / {len(cluster_stats)} 个集群"
            )
            self.repo.save_anomalies(run_id, anomalies)
            self.repo.save_baselines(run_id, baselines)

        # ---------------------------------------------------------- 7. 规则算分
        with tracker.stage("score") as stage:
            score = self.scorer.compute(anomalies, clusters, ai_adjust=0, data_quality=normalized.quality)
            run.score = score.total
            stage.detail = (
                f"规则算分 {score.total:.1f}（{score.grade}）"
                + (f"；{score.capped_by}" if score.capped_by else "")
            )

        # ---------------------------------------------------------- 8. AI 分析
        with tracker.stage("ai_analysis") as stage:
            analysis, meta = self.analyzer.analyze(
                clusters, context, repo=self.repo, run_id=run_id,
                data_quality=normalized.quality, score=score,
            )
            run.ai_degraded = meta.degraded
            stage.status = StageStatus.DEGRADED if meta.degraded else StageStatus.SUCCEEDED
            stage.detail = (
                f"{meta.status_label}；{len(analysis.findings)} 条根因结论，"
                f"token {meta.total_tokens}，耗时 {meta.latency_ms}ms"
            )
            self.repo.save_ai(run_id, analysis.model_dump(mode="json"), meta.to_row())
            self.repo.save_clusters(
                run_id, clusters,
                root_causes={
                    f.cluster_id: {
                        "category": f.root_cause_category.value,
                        "root_cause": f.root_cause,
                    }
                    for f in analysis.findings
                },
            )

            if analysis.overall_score_adjust:
                score = self.scorer.compute(
                    anomalies, clusters,
                    ai_adjust=analysis.overall_score_adjust,
                    data_quality=normalized.quality,
                )
                run.score = score.total
                stage.detail += f"；AI 微调 {analysis.overall_score_adjust:+d} 分 -> {score.total:.1f}"

        # ---------------------------------------------------------- 9. 报告
        history, comparison = self._history_and_comparison(run, anomalies)
        report = InspectionReport(
            run=run, score=score, anomalies=anomalies, clusters=clusters,
            ai=analysis, ai_meta=meta, data_quality=normalized.quality,
            baselines=baselines, changes=context.changes(), comparison=comparison,
            history=history, rule_stats=self.engine.stats(findings),
            service_stats=service_stats, cluster_stats=cluster_stats,
        )

        with tracker.stage("report") as stage:
            markdown = render_markdown(report)
            stage.detail = f"Markdown 报告 {len(markdown)} 字符"
            try:
                path = self._write_report(run, markdown)
                stage.detail += f"，已写入 {path.name}"
            except OSError as exc:
                stage.status = StageStatus.DEGRADED
                stage.detail += f"；文件写入失败（{exc}），但仍可通过接口获取"
            self.repo.save_report(
                run_id, score.total, analysis.summary if analysis else "",
                markdown, report.model_dump(mode="json"),
            )

        if notify:
            with tracker.stage("notify") as stage:
                result = self._notifier().notify(report)
                stage.detail = result.summary
                if result.saved_to:
                    stage.detail += f"，报文落盘 {result.saved_to.name}"

        # 定稿：状态与结束时刻只能在这里确定（notify 阶段也可能降级），
        # 所以报告要重渲染一次——文件里的「状态：运行中 / 耗时 0.0s」会让读者
        # 以为巡检没跑完，这比缺字段更糟。
        run.status = self._final_status(run)
        run.finished_at = now()
        self.repo.save_run(run)

        markdown = render_markdown(report)
        try:
            self._write_report(run, markdown)
        except OSError:
            logger.warning("定稿报告覆盖写入失败，磁盘上仍是过程态版本", exc_info=True)
        self.repo.save_report(
            run_id, score.total, analysis.summary if analysis else "",
            markdown, report.model_dump(mode="json"),
        )

        logger.info(
            "巡检 %s 完成：状态 %s，评分 %.1f，异常 %d 条，耗时 %dms",
            run_id, run.status.label, score.total, run.anomaly_count, run.duration_ms,
        )
        return report

    # ------------------------------------------------------------------ 辅助
    def _resolve_window_end(self) -> datetime:
        """巡检窗口结束时刻。

        配置里固定了 `dataset_end` 就用它——**这是可复现性的根基**：
        同一份配置跑两次必须得到同一份报告，否则「多次巡检对比」无从谈起。
        生产环境把这一项留空，就会取当前时间。
        """
        configured = self.settings.scenarios.dataset_end
        return parse_iso(configured) if configured else now()

    def _persist_points(self, dataset_id: str, points: list, stage: StageRecord) -> int:
        """写入检测数据集，**内容未变时跳过重写**。

        定时巡检在同一时间窗内可能被执行多次（窗口每 15 分钟才滑一次），
        每次都删掉二十万行再写回同样二十万行，既浪费 IO 又让数据库文件持续膨胀。
        指纹（点数 + 时间范围）一致就直接复用。
        """
        fingerprint = _fingerprint(points)
        if self.repo.dataset_fingerprint(dataset_id) == fingerprint:
            stage.detail = "数据与库中一致，跳过写入"
            return fingerprint[0]
        self.repo.delete_dataset(dataset_id)
        return self.repo.insert_metric_points(dataset_id, self.settings.granularity_seconds, points)

    def _history_minutes(self, scenario_id: str | None) -> int:
        base = required_history_minutes(self.settings)
        if scenario_id:
            scenario = self.settings.scenarios.get(scenario_id)
            if scenario and scenario.history_minutes:
                base = max(base, scenario.history_minutes)
        return base

    def _sync_changes(self, anchor: datetime, collected: CollectionResult | None) -> None:
        """把变更事件同步进库。变更量很小，全量替换比增量 upsert 简单可靠。"""
        path = self.settings.app.data_dir / CHANGES_FILE
        events, scenario_map = build_change_events(path, anchor)
        if events:
            self.repo.replace_changes(events, scenario_map)
        del collected  # 变更来源独立于指标源，这里只借用地锚点

    def _history_and_comparison(self, run: InspectionRun, anomalies: list[Anomaly]):
        try:
            snapshots = self.repo.score_history(limit=12)
            previous = self.repo.previous_run(run.run_id)
            previous_rows = self.repo.load_anomalies(previous.run_id) if previous else None
            comparison = build_comparison(run, anomalies, previous, previous_rows)
            history = build_history(
                snapshots, run.run_id, run.score or 0.0,
                run.started_at.isoformat(timespec="minutes"), run.scenario_id,
            )
            return history, comparison
        except Exception:  # noqa: BLE001 - 对比失败不应阻断报告生成
            logger.warning("历史对比生成失败，报告将不含趋势章节", exc_info=True)
            return [], None

    def _write_report(self, run: InspectionRun, markdown: str) -> Path:
        directory = self.settings.app.report_dir / (run.scenario_id or "adhoc")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run.run_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def _notifier(self) -> AlertNotifier:
        app = self.settings.app
        return AlertNotifier(
            webhook_url=app.alert_webhook_url,
            min_level=app.alert_min_level,
            dry_run=app.alert_dry_run,
            output_dir=app.report_dir / "alerts",
        )

    @staticmethod
    def _final_status(run: InspectionRun) -> RunStatus:
        statuses = {stage.status for stage in run.stages}
        if StageStatus.FAILED in statuses:
            return RunStatus.PARTIAL if any(
                stage.status is StageStatus.SUCCEEDED for stage in run.stages
            ) else RunStatus.FAILED
        if StageStatus.DEGRADED in statuses:
            return RunStatus.PARTIAL
        return RunStatus.SUCCEEDED

    def _finish_without_data(
        self, run: InspectionRun, tracker: StageTracker, reason: str
    ) -> InspectionReport:
        logger.error("巡检 %s 未能获得有效数据：%s", run.run_id, reason)
        run.status = RunStatus.FAILED
        run.error = reason
        run.finished_at = now()
        self.repo.save_run(run)
        score = ScoreResult(
            rule_score=0.0, ai_adjust=0, total=0.0, grade="无法评估",
            breakdown=[], capped_by=None,
        )
        return InspectionReport(run=run, score=score, ai=None, ai_meta=AIMeta(
            provider="none", model="none", degraded=True, degraded_reason=reason,
        ))


def _fingerprint(points: list) -> tuple[int, int, int]:
    epochs = [to_epoch(point.ts) for point in points]
    return (len(points), min(epochs) if epochs else 0, max(epochs) if epochs else 0)


def run_inspection(
    scenario_id: str | None = None,
    source_name: str | None = None,
    settings: Settings | None = None,
) -> InspectionReport:
    """便捷入口：CLI / HTTP 接口 / 调度器共用。"""
    return InspectionOrchestrator(settings).run(scenario_id=scenario_id, source_name=source_name)


def report_to_json(report: InspectionReport) -> str:
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, default=str)
