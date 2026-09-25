"""告警推送（钉钉 / 企业微信，模拟）。

两个刻意的设计取舍：

1. **只有 P1/P2 才推**。告警的价值在于「稀缺」，把 P3/P4 也推出去，
   迟早会演变成没人看的告警群——告警疲劳比漏报更致命，
   因为它让真正的 P1 一起被忽略掉；
2. **默认 dry-run**。机器人 webhook 一配错就会把测试消息发给全公司。
   默认落盘到 `reports/alerts/`，显式打开才真的发出去。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..models import InspectionReport, Severity
from ..utils.text import truncate
from ..utils.timeutil import format_duration, now

logger = logging.getLogger(__name__)

LEVEL_EMOJI = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🔵"}


@dataclass
class AlertResult:
    sent: bool
    channel: str
    reason: str = ""
    payload: dict = field(default_factory=dict)
    saved_to: Path | None = None

    @property
    def summary(self) -> str:
        if self.sent:
            return f"{self.channel} 告警已推送"
        return f"{self.channel} 未推送（{self.reason}）"


class AlertNotifier:
    def __init__(
        self,
        webhook_url: str | None,
        min_level: str = "P2",
        dry_run: bool = True,
        output_dir: Path | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.min_level = Severity.parse(min_level)
        self.dry_run = dry_run
        self.output_dir = output_dir
        self.timeout_s = timeout_s

    def notify(self, report: InspectionReport, run_timestamp: str | None = None) -> AlertResult:
        active = [a for a in report.anomalies if not a.is_suppressed]
        urgent = [a for a in active if a.level <= self.min_level]
        if not urgent:
            return AlertResult(sent=False, channel="webhook", reason=f"无 {self.min_level.code} 及以上异常")

        payload = self._build_payload(report, urgent)
        if self.dry_run or not self.webhook_url:
            saved = self._save(payload, report, run_timestamp)
            reason = "dry-run 模式" if self.dry_run else "未配置 webhook 地址"
            return AlertResult(sent=False, channel="webhook", reason=reason, payload=payload, saved_to=saved)

        try:
            response = httpx.post(self.webhook_url, json=payload, timeout=self.timeout_s)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("告警推送失败：%s", exc)
            return AlertResult(sent=False, channel="webhook", reason=f"推送失败：{exc}", payload=payload)
        return AlertResult(sent=True, channel="webhook", payload=payload)

    # ------------------------------------------------------------------ 报文
    def _build_payload(self, report: InspectionReport, urgent: list) -> dict:
        """钉钉 / 企业微信通用的 markdown 消息体。"""
        run = report.run
        lines = [
            f"### {LEVEL_EMOJI.get(urgent[0].level.code, '')} 稳定性巡检告警",
            "",
            f"- **评分**：{report.score.total:.1f} / 100（{report.score.grade}）",
            f"- **窗口**：{run.window.label(with_date=True)}",
            f"- **编号**：`{run.run_id}`",
            "",
            "**异常清单**",
            "",
        ]
        for anomaly in urgent[:10]:
            lines.append(
                f"- {LEVEL_EMOJI.get(anomaly.level.code, '')} **{anomaly.level.display()}** "
                f"{anomaly.service}/{anomaly.instance} {anomaly.metric.label} "
                f"实测 {anomaly.observed:.2f}（基线 {anomaly.baseline_value:.2f}）"
                f"，持续 {format_duration(anomaly.duration_minutes)}"
            )
        if report.ai and report.ai.findings:
            finding = report.ai.findings[0]
            lines.extend(["", f"**根因推断**：{finding.category_label} — {truncate(finding.root_cause, 120)}"])
            if finding.suggestions:
                lines.extend(["", f"**首要处置**：{truncate(finding.suggestions[0], 120)}"])
        lines.extend(["", f"> StaGuardAgent 自动巡检　{run.started_at:%Y-%m-%d %H:%M:%S}"])
        return {"msgtype": "markdown", "markdown": {"title": "稳定性巡检告警", "text": "\n".join(lines)}}

    def _save(self, payload: dict, report: InspectionReport, run_timestamp: str | None) -> Path | None:
        if self.output_dir is None:
            return None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = run_timestamp or now().strftime("%Y%m%d-%H%M%S")
        path = self.output_dir / f"alert-{stamp}-{report.run.run_id}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        logger.info("告警报文已落盘（dry-run）：%s", path)
        return path
