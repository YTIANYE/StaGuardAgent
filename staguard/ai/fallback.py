"""规则归因兜底（AI 不可用时的第二级降级）。

**这是本项目工程可靠性的关键一环**：大模型会因为没配 key、网络抖动、限流、
返回格式异常而不可用，但巡检报告不能因此产不出来。
金融场景里「因为 AI 挂了所以今天不出巡检报告」是不可接受的。

兜底不是「返回空结果」，而是一棵按证据特征判定的决策树——
它当然不如大模型会读上下文，但它**永远可用、永远确定性、永远不会编造**。
报告里会明确标注 `degraded`，让读者知道这份结论的成色。
"""

from __future__ import annotations

import logging

from ..models import (
    AIAnalysis,
    AnomalyCluster,
    ChangeEvent,
    Finding,
    RootCauseCategory,
    TrendJudgement,
)
from ..utils.text import truncate

logger = logging.getLogger(__name__)

CHANGE_WINDOW_MINUTES = 30.0
"""异常起始时刻前多久内的变更算「高度相关」。

真实发布事故的暴露通常很快（分钟级），放宽到 30 分钟能覆盖大部分情况，
又不会把几小时前的无关变更牵连进来。
"""


class FallbackAttributor:
    """基于证据特征的规则归因。"""

    def __init__(self, changes: list[ChangeEvent] | None = None, owner_lookup=None) -> None:  # noqa: ANN001
        self.changes = changes or []
        self.owner_lookup = owner_lookup

    def analyze(self, clusters: list[AnomalyCluster], reason: str) -> AIAnalysis:
        findings = [self._attribute(cluster) for cluster in clusters]
        p1 = [f for f, c in zip(findings, clusters, strict=False) if c.max_level.value == 1]
        summary = self._summary(clusters, findings)
        return AIAnalysis(
            overall_score_adjust=0,
            summary=summary,
            findings=findings,
            trend=self._trend(clusters),
            stability_advice=self._advice(findings, p1),
            uncertainties=[
                f"当前结论由规则归因生成（原因：{reason}），未经大模型复核，"
                "对复杂链路的因果判断能力有限",
                "如需更深入的根因推理，请配置 LLM 凭据后重跑本次巡检",
            ],
        )

    # ------------------------------------------------------------------ 单个簇
    def _attribute(self, cluster: AnomalyCluster) -> Finding:
        category, reason = self._classify(cluster)
        anomalies = cluster.all_anomalies()
        owner = None
        if self.owner_lookup is not None:
            try:
                owner = self.owner_lookup(cluster.primary.service)
            except Exception:  # noqa: BLE001 - 归因不应因为查责任人失败而中断
                owner = None
        return Finding(
            cluster_id=cluster.cluster_id,
            root_cause_category=category,
            root_cause_service=cluster.primary.service,
            root_cause=f"{reason}；证据：{truncate(cluster.primary.level_reason, 110)}",
            confidence=self._confidence(cluster, category),
            evidence_ids=[ref["evidence_id"] for ref in cluster.evidence_refs()],
            impact=self._impact(cluster),
            suggestions=self._suggestions(cluster, category),
            is_recurring=any(a.is_recurring for a in anomalies),
            owner_hint=owner,
        )

    def _classify(self, cluster: AnomalyCluster) -> tuple[RootCauseCategory, str]:
        """决策树。**顺序即优先级**，把最强的判别特征放前面。

        判别顺序的关键是**先看主根因这条异常本身是什么**，再看簇里还有哪些信号。
        反过来先看「簇里有没有 RES-04」会经常误判：
        一次资源瓶颈造成的雪崩里，连接池打满往往只是被影响的一方，
        而真正打满 CPU 的那个服务才是根因。
        """
        rules = {a.rule_id for a in cluster.all_anomalies()}
        primary = cluster.primary
        rid = primary.rule_id

        # 1. 变更时间窗重合 —— 最强证据，优先级最高
        change = self._recent_change(cluster)
        if change is not None:
            return (
                RootCauseCategory.CHANGE_INDUCED,
                f"异常起始于 {primary.first_seen:%H:%M}，与 {change.service} 的"
                f"{change.type.label}{change.version or ''}（{change.ts:%H:%M}）高度重合",
            )

        # 2. 主根因就是流量异常 —— 排除掉「把受影响服务的资源压力当根因」的误判
        if rid in {"BIZ-03", "BIZ-04"}:
            direction = "上涨" if rid == "BIZ-03" else "下跌"
            pressure = "，同时已造成下游资源水位升高" if {"RES-01", "RES-02", "RES-04"} & rules else ""
            return (
                RootCauseCategory.TRAFFIC_FLUCTUATION,
                f"全链路 {len(cluster.services)} 个服务的流量同向{direction}，无局部故障特征{pressure}",
            )

        # 3. 连接池打满 + 延时恶化 —— 典型的「下游慢拖垮上游」
        if rid == "RES-04" and ({"PERF-01", "PERF-02"} & rules):
            return (
                RootCauseCategory.DEPENDENCY_FAILURE,
                f"{primary.service} 连接池被慢请求占满且延时同步恶化，"
                "指向其下游依赖响应退化而非自身容量不足",
            )

        # 4. CPU / 内存 / 连接水位越线 —— 资源瓶颈
        if rid in {"RES-01", "RES-02", "RES-04"}:
            return (
                RootCauseCategory.RESOURCE_BOTTLENECK,
                f"{primary.service} 的 {primary.metric.label} 越线，实例资源已接近打满",
            )

        # 5. 内存单调增长 —— 泄漏，属于代码缺陷
        if rid == "RES-03":
            return (
                RootCauseCategory.CODE_DEFECT,
                f"{primary.service} 内存随时间单调增长，符合内存泄漏特征，需要走代码修复而非扩容",
            )

        # 6. 实例间严重倾斜 —— 单实例问题
        if rid == "RES-05":
            return (
                RootCauseCategory.CAPACITY_CONFIG,
                f"{primary.service} 同服务实例间指标严重倾斜，疑似单实例异常或负载不均",
            )

        # 7. 业务错误率飙升 —— 接口可用、业务不可用，多半是代码或配置问题
        if rid == "BIZ-02":
            return (
                RootCauseCategory.CODE_DEFECT,
                f"{primary.service} 技术成功率基本正常但业务错误率飙升，"
                "请求被成功服务却在业务层失败，怀疑代码逻辑或依赖返回异常",
            )

        # 8. 纯性能劣化 —— 归为资源/容量问题
        if primary.dimension.value == "performance":
            return (
                RootCauseCategory.RESOURCE_BOTTLENECK,
                f"{primary.service} 延时显著劣化但未见资源水位越线，"
                "疑似下游慢调用或线程池/连接池排队",
            )

        # 9. 只有成功率或链路健康度异常 —— 缺少定位性证据，交给人工
        if {"BIZ-01", "BIZ-06"} & rules:
            return (
                RootCauseCategory.UNKNOWN,
                f"{primary.service} 出现成功率劣化，但缺少资源、依赖、变更等定位性证据",
            )
        return RootCauseCategory.UNKNOWN, f"{primary.service} 的 {primary.metric.label} 异常，证据不足以定性"

    def _recent_change(self, cluster: AnomalyCluster) -> ChangeEvent | None:
        services = set(cluster.services) | {cluster.primary.service}
        anchor = cluster.primary.first_seen
        best: tuple[float, ChangeEvent] | None = None
        for change in self.changes:
            delta = (anchor - change.ts).total_seconds() / 60.0
            if not (0 <= delta <= CHANGE_WINDOW_MINUTES):
                continue
            if change.service not in services and change.service != cluster.primary.service:
                continue
            if best is None or delta < best[0]:
                best = (delta, change)
        return best[1] if best else None

    def _confidence(self, cluster: AnomalyCluster, category: RootCauseCategory) -> float:
        base = {RootCauseCategory.CHANGE_INDUCED: 0.85}.get(category, 0.55)
        if category is RootCauseCategory.UNKNOWN:
            return 0.25
        if cluster.size >= 3:
            base += 0.05
        if cluster.primary.baseline_mode.value == "dynamic":
            base += 0.05
        return round(min(0.9, base), 2)

    def _impact(self, cluster: AnomalyCluster) -> str:
        upstream = [s for s in cluster.services if s != cluster.primary.service]
        if not upstream:
            return f"{cluster.primary.service} 的 {cluster.primary.metric.label} 局部劣化，暂未扩散"
        return (
            f"影响 {len(cluster.services)} 个服务（{'、'.join(upstream)}），"
            f"波及 {len(cluster.all_anomalies())} 项指标"
        )

    def _suggestions(self, cluster: AnomalyCluster, category: RootCauseCategory) -> list[str]:
        service = cluster.primary.service
        rules = {a.rule_id for a in cluster.all_anomalies()}
        immediate = {
            RootCauseCategory.DEPENDENCY_FAILURE: f"检查 {service} 下游依赖的健康度与连接池占用，必要时熔断或降级",
            RootCauseCategory.RESOURCE_BOTTLENECK: f"先摘除 {cluster.primary.instance} 观察整体水位是否回落，再评估扩容",
            RootCauseCategory.CHANGE_INDUCED: f"回滚 {service} 最近一次变更，验证恢复后再做灰度复盘",
            RootCauseCategory.CODE_DEFECT: f"拉取 {service} 异常日志与堆栈，结合最近变更定位失败路径",
            RootCauseCategory.TRAFFIC_FLUCTUATION: "核对容量水位与限流阈值，确认是否为预期活动流量",
            RootCauseCategory.CAPACITY_CONFIG: f"对比 {service} 各实例的负载分布，排查负载均衡与实例健康",
            RootCauseCategory.UNKNOWN: "补充日志与链路追踪数据后重新巡检",
        }.get(category)
        result = [immediate] if immediate else []

        if category is RootCauseCategory.TRAFFIC_FLUCTUATION and {"RES-01", "RES-02", "RES-04"} & rules:
            # 流量确实是根因，但资源已经打到 99% 就不能只说「这是预期流量」——
            # 首轮峰值抗住了，下一轮放量未必扛得住，这个信息必须给出来。
            result.append(
                "本次虽然业务未受损，但下游实例资源已接近打满；"
                "按当前流量峰值反推所需容量并预留 30% 余量，避免下一轮放量时直接击穿"
            )
        result.append(category.playbook)
        result.append(f"为 {cluster.primary.metric.label} 配置对应的告警与自动化处置预案，缩短下次的发现时长")
        return [item for item in result if item]

    # ------------------------------------------------------------------ 汇总
    def _summary(self, clusters: list[AnomalyCluster], findings: list[Finding]) -> str:
        if not clusters:
            return "本次巡检未发现越线异常，各服务指标处于历史同时段正常区间内。"
        worst = clusters[0]
        categories = sorted({f.root_cause_category.label for f in findings})
        return (
            f"本次巡检共识别 {len(clusters)} 个根因簇，最严重的是 {worst.primary.service} 的"
            f"{worst.primary.metric.label} 异常（{worst.max_level.display()}），"
            f"已扩散至 {len(worst.services)} 个服务。"
            f"归因类别集中于：{'、'.join(categories)}。"
        )

    def _trend(self, clusters: list[AnomalyCluster]) -> TrendJudgement:
        if not clusters:
            return TrendJudgement(direction="stable", reasoning="未发现越线异常，稳定性处于正常区间")
        worst = clusters[0]
        seen: list[str] = []
        for anomaly in worst.all_anomalies():
            label = f"{anomaly.service} {anomaly.metric.label}"
            if label not in seen:
                seen.append(label)
            if len(seen) >= 3:
                break
        lines = seen
        return TrendJudgement(
            direction="degrading",
            reasoning=f"当前仍有 {len(clusters)} 个根因簇未恢复，涉及 {'、'.join(lines)}",
            forecast="若未在窗口内处置，异常会继续沿依赖链向上游扩散，建议在下一轮巡检前完成止血",
        )

    def _advice(self, findings: list[Finding], p1_findings: list[Finding]) -> list[str]:
        advice = [
            "把本次命中的规则阈值反哺到容量规划：反复越线的指标说明当前水位与业务量已经不匹配",
            "为高频根因（依赖故障、资源瓶颈）沉淀标准处置手册，把平均恢复时间从小时级压到分钟级",
        ]
        if any(f.root_cause_category is RootCauseCategory.CHANGE_INDUCED for f in findings):
            advice.append("补齐变更管控：发布后自动触发定向巡检，异常时支持一键回滚")
        if p1_findings:
            advice.append("对核心链路补充依赖隔离与熔断降级能力，避免单点依赖故障放大成全链路事故")
        return advice
