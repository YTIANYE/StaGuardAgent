"""AI 智能分析编排：证据 -> 模型 -> 校验 -> 降级。

降级链是**两级**，每一级都必须产出**完整可用的结论**：

    1. 大模型       正常路径，给上下文推理与自然语言表达；
    2. 规则决策树   AI 不可用 / 调用失败 / 返回非法时的兜底，确定性且永不失败。
                    树内对「没有定位性证据」的簇返回 unknown，只陈述事实、不做因果断言。

这里刻意没有第三级。早期文档写过「大模型 → 规则决策树 → 纯统计」三级，
但决策树按构造不会失败，独立第三级是一段永远不会被走到的死代码；
「只报事实」这种能力由决策树内部的 unknown 分支承担，不需要另起一级。

`AIMeta.degraded` 会如实记录走的是哪一级、为什么。
**报告绝不因为 AI 不可用而产不出来**——这是本模块最硬的一条约束。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import ValidationError

from ..config import Settings
from ..models import (
    AIAnalysis,
    AIMeta,
    AnomalyCluster,
    DataQualityReport,
    Finding,
    RootCauseCategory,
    ScoreResult,
)
from ..rules import EvalContext
from ..store import Repository
from ..utils.text import truncate
from .evidence import build_clean_evidence, build_evidence, collect_evidence_ids
from .fallback import FallbackAttributor
from .history import HistoryMatch, find_matches, is_recurring
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from .provider import LLMProvider, ProviderError, ProviderUnavailable, build_provider

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 1.5


class AIAnalyzer:
    def __init__(self, settings: Settings, provider: LLMProvider | None = None) -> None:
        self.settings = settings
        self.provider = provider or build_provider(settings.app)
        self.last_evidence: dict[str, Any] = {}

    # ------------------------------------------------------------------ 入口
    def analyze(
        self,
        clusters: list[AnomalyCluster],
        ctx: EvalContext | None,
        repo: Repository | None = None,
        run_id: str | None = None,
        data_quality: DataQualityReport | None = None,
        score: ScoreResult | None = None,
    ) -> tuple[AIAnalysis, AIMeta]:
        """执行归因分析。`ctx` 允许为空——无上下文时自动退化为规则归因。"""
        owner_lookup = getattr(ctx, "owner", None)
        fallback = FallbackAttributor(
            self._changes_for(ctx),
            owner_lookup=owner_lookup,
            topology=getattr(ctx, "topology", None) or self.settings.topology,
        )

        if not clusters:
            return self._clean_result(ctx, data_quality), AIMeta(
                provider="rule", model="none", degraded=False, prompt_version=PROMPT_VERSION,
            )

        history_map = self._history_map(ctx, repo, run_id, clusters)
        _mark_recurrence(clusters, history_map)

        # 没有上下文时无法组装证据包，直接走规则归因。
        # 这条路径在测试里会用，在生产里对应「采集阶段就失败」的极端情况。
        if ctx is None:
            return fallback.analyze(clusters, "缺少巡检上下文，无法组装证据包"), AIMeta(
                provider="rule", model="none", degraded=True,
                degraded_reason="缺少巡检上下文", prompt_version=PROMPT_VERSION,
            )

        self.last_evidence = build_evidence(ctx, clusters, history_map, data_quality, score)

        available, reason = self.provider.available()
        if not available:
            logger.warning("AI 不可用，降级为规则归因：%s", reason)
            return fallback.analyze(clusters, reason or "未知原因"), AIMeta(
                provider=self.provider.name,
                model=self.provider.model,
                degraded=True,
                degraded_reason=reason,
                prompt_version=PROMPT_VERSION,
            )

        analysis, meta = self._call_model(clusters, fallback)
        return analysis, meta

    # ------------------------------------------------------------------ 模型调用
    def _call_model(
        self,
        clusters: list[AnomalyCluster],
        fallback: FallbackAttributor,
    ) -> tuple[AIAnalysis, AIMeta]:
        user_prompt = build_user_prompt(self.last_evidence)
        attempts = max(1, self.settings.app.llm_max_attempts)
        last_error = ""
        started = time.perf_counter()

        for attempt in range(1, attempts + 1):
            try:
                response = self.provider.complete_json(SYSTEM_PROMPT, user_prompt)
            except ProviderUnavailable as exc:
                return self._degraded(clusters, fallback, str(exc), attempt)
            except ProviderError as exc:
                last_error = str(exc)
                logger.warning("AI 调用第 %d/%d 次失败：%s", attempt, attempts, last_error)
                if attempt < attempts:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            analysis, repairs = self._validate(response.payload, clusters, fallback)
            if analysis is None:
                last_error = "模型返回的结构无法通过校验"
                if attempt < attempts:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue

            meta = AIMeta(
                provider=response.provider or self.provider.name,
                model=response.model or self.provider.model,
                degraded=bool(repairs),
                degraded_reason="；".join(repairs) if repairs else None,
                attempts=attempt,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms or int((time.perf_counter() - started) * 1000),
                prompt_version=PROMPT_VERSION,
                raw_preview=truncate(response.raw, 400),
            )
            if repairs:
                logger.warning("AI 结论经修复后采用：%s", "；".join(repairs))
                analysis.uncertainties = [*analysis.uncertainties, *repairs]
            return analysis, meta

        return self._degraded(clusters, fallback, last_error or "模型不可用", attempts)

    def _degraded(
        self,
        clusters: list[AnomalyCluster],
        fallback: FallbackAttributor,
        reason: str,
        attempts: int,
    ) -> tuple[AIAnalysis, AIMeta]:
        logger.warning("AI 分析降级为规则归因：%s", reason)
        return fallback.analyze(clusters, reason), AIMeta(
            provider=self.provider.name,
            model=self.provider.model,
            degraded=True,
            degraded_reason=reason,
            attempts=attempts,
            prompt_version=PROMPT_VERSION,
        )

    # ------------------------------------------------------------------ 校验与修复
    def _validate(
        self,
        payload: dict[str, Any],
        clusters: list[AnomalyCluster],
        fallback: FallbackAttributor,
    ) -> tuple[AIAnalysis | None, list[str]]:
        """校验并修复模型输出。

        **不做「全有或全无」的校验**：模型偶尔写错一个枚举值，
        不应该导致整份分析被丢弃。能修的就修（并记录下来），
        只有结构性错误（顶层不是对象、findings 不是列表）才判为失败并触发重试。
        """
        repairs: list[str] = []
        cleaned = dict(payload)
        cleaned["findings"] = [f for f in payload.get("findings") or [] if isinstance(f, dict)]

        valid_categories = {c.value for c in RootCauseCategory}
        valid_cluster_ids = {c.cluster_id for c in clusters}
        kept: list[dict[str, Any]] = []
        for finding in cleaned["findings"]:
            cluster_id = str(finding.get("cluster_id") or "")
            if cluster_id not in valid_cluster_ids:
                repairs.append(f"忽略了引用不存在簇的结论（cluster_id={cluster_id or '空'}）")
                continue
            category = str(finding.get("root_cause_category") or "")
            if category not in valid_categories:
                repairs.append(f"{cluster_id} 的根因类别 `{category}` 非枚举值，已归为「证据不足」")
                finding["root_cause_category"] = RootCauseCategory.UNKNOWN.value
            kept.append(finding)
        cleaned["findings"] = kept

        try:
            analysis = AIAnalysis.model_validate(cleaned)
        except ValidationError as exc:
            logger.warning("AI 输出校验失败：%s", exc.errors()[:3])
            return None, repairs

        repairs.extend(self._guard_evidence(analysis, clusters))
        repairs.extend(self._fill_missing(analysis, clusters, fallback))
        repairs.extend(self._sync_recurrence(analysis, clusters))
        return analysis, repairs

    def _sync_recurrence(
        self, analysis: AIAnalysis, clusters: list[AnomalyCluster]
    ) -> list[str]:
        """把模型声明的复发标记校正为**历史检索的结果**。

        复发与否是可验证的事实，历史检索已经算出来了（`_mark_recurrence`），
        不该由模型自行声明——和「评分由规则算、AI 只微调」是同一个道理。
        实测踩过：在一个没有任何历史同类事件的场景里，模型写了 `is_recurring=true`，
        报告于是出现「历史复发，说明上次的处置并未根治」，
        属于典型的「看起来很专业、但站不住」的结论。
        """
        computed = {
            cluster.cluster_id: any(a.is_recurring for a in cluster.all_anomalies())
            for cluster in clusters
        }
        repairs: list[str] = []
        for finding in analysis.findings:
            actual = computed.get(finding.cluster_id, False)
            if finding.is_recurring != actual:
                repairs.append(
                    f"{finding.cluster_id} 的复发标记与历史检索不一致"
                    f"（模型 {finding.is_recurring} / 检索 {actual}），已按检索结果校正"
                )
                finding.is_recurring = actual
        return repairs

    def _guard_evidence(self, analysis: AIAnalysis, clusters: list[AnomalyCluster]) -> list[str]:
        """把模型编造的 evidence_id 清掉。

        这一步是在防幻觉：模型很擅长给出「看起来很专业」的引用，
        如果不校验，报告里就会出现指向不存在证据的结论——
        读者去核对时发现对不上，整份报告的可信度就没了。
        """
        valid = collect_evidence_ids(clusters)
        services = {service for cluster in clusters for service in cluster.services}
        repairs: list[str] = []
        for finding in analysis.findings:
            unknown = [eid for eid in finding.evidence_ids if eid not in valid]
            if unknown:
                finding.evidence_ids = [eid for eid in finding.evidence_ids if eid in valid]
                repairs.append(
                    f"{finding.cluster_id} 引用了 {len(unknown)} 个不存在的 evidence_id，已剔除"
                )
            if not finding.evidence_ids:
                repairs.append(f"{finding.cluster_id} 未给出有效证据引用，其结论请谨慎采信")
                finding.confidence = min(finding.confidence, 0.4)
            # 根因服务名同样要校验：模型可能顺手写一个拓扑里没有的名字，
            # 而报告里那一行是要给运维照着执行的，写错了会把人带到沟里
            if finding.root_cause_service and finding.root_cause_service not in services:
                repairs.append(
                    f"{finding.cluster_id} 给出的根因服务 `{finding.root_cause_service}` "
                    "不在本次涉及的服务范围内，已忽略该定位"
                )
                finding.root_cause_service = None
        return repairs

    def _fill_missing(
        self,
        analysis: AIAnalysis,
        clusters: list[AnomalyCluster],
        fallback: FallbackAttributor,
    ) -> list[str]:
        """补齐模型漏掉的簇。

        模型只回答了一部分簇是常见情况（尤其是簇很多时）。漏掉的簇不能从报告里消失，
        用规则归因补上并标注来源——**覆盖完整性优先于来源纯净**。
        """
        covered = analysis.covered_cluster_ids()
        missing = [c for c in clusters if c.cluster_id not in covered]
        if not missing:
            return []
        for cluster in missing:
            finding: Finding = fallback._attribute(cluster)  # noqa: SLF001 - 复用同一棵决策树，避免两套逻辑漂移
            analysis.findings.append(finding)
        return [f"{len(missing)} 个根因簇未被模型覆盖，已用规则归因补齐"]

    # ------------------------------------------------------------------ 辅助
    def _history_map(
        self,
        ctx: EvalContext | None,
        repo: Repository | None,
        run_id: str | None,
        clusters: list[AnomalyCluster],
    ) -> dict[str, list[HistoryMatch]]:
        if repo is None or ctx is None:
            return {}
        # 排除同一时间窗口的历史：复发意味着「同类问题在之后的窗口里又出现」，
        # 同一个窗口被反复看（重跑、回归、评测、不同切片）都不是复发。
        # 判据只能是窗口本身——按场景或按数据切片排除都挡不住跨场景的同窗口匹配：
        # S3 的流量突增与 S1 的依赖故障共享一批 `服务.指标` token，
        # 于是 S3 的报告里会写「历史复发，说明上次的处置并未根治」。
        exclude_window_end = ctx.window.end.isoformat(timespec="seconds")
        try:
            recent = repo.recent_clusters(
                limit=200, exclude_run_id=run_id, exclude_window_end=exclude_window_end
            )
        except Exception:  # noqa: BLE001 - 历史检索失败不应阻断归因
            logger.warning("历史事件检索失败，跳过复发识别", exc_info=True)
            return {}
        return {cluster.cluster_id: find_matches(cluster.signature(), recent) for cluster in clusters}

    @staticmethod
    def _changes_for(ctx: EvalContext | None):
        if ctx is None:
            return []
        try:
            return ctx.changes()
        except Exception:  # noqa: BLE001
            logger.warning("变更事件读取失败，归因将缺少变更证据", exc_info=True)
            return []

    def _clean_result(self, ctx: EvalContext | None, data_quality: DataQualityReport | None) -> AIAnalysis:
        """无异常时的结论。**不调用大模型**——为「一切都好」花一次调用是不必要的成本。"""
        instances = len(ctx.instances()) if ctx is not None else 0
        if ctx is not None:
            self.last_evidence = build_clean_evidence(ctx, data_quality)
        quality_note = ""
        if data_quality is not None and data_quality.confidence.value != "high":
            quality_note = f"需要说明的是，本次数据质量置信度为「{data_quality.confidence.label}」，结论请结合该因素评估。"
        return AIAnalysis(
            overall_score_adjust=0,
            summary=(
                f"本次巡检覆盖 {instances} 个实例、10 类核心指标，"
                f"未发现越线异常，各服务指标处于历史同时段正常区间内。{quality_note}"
            ),
            findings=[],
            trend={
                "direction": "stable",
                "reasoning": "成功率、延时、资源水位与流量均未偏离动态基线，未检测到趋势性劣化",
                "forecast": "按当前水位，未来 1 小时无预期风险",
            },
            stability_advice=["保持现有容量水位，继续观察业务量变化对资源水位的影响"],
            uncertainties=["未发现异常不等于没有隐患，慢故障需要更长的观察窗才能显现"],
        )


def _mark_recurrence(clusters: list[AnomalyCluster], history_map: dict[str, list[HistoryMatch]]) -> None:
    for cluster in clusters:
        recurring = is_recurring(history_map.get(cluster.cluster_id, []))
        for anomaly in cluster.all_anomalies():
            anomaly.is_recurring = recurring
