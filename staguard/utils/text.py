"""文本与数值格式化工具，集中处理报告里的展示细节。"""

from __future__ import annotations

import math
import re
from typing import Any


def fmt(value: float | int | None, digits: int = 2, dash: str = "-") -> str:
    """数值格式化。None / NaN 统一显示为占位符，避免报告里出现 nan。"""
    if value is None:
        return dash
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return dash
    if isinstance(value, float) and value == int(value) and abs(value) < 1e9:
        return str(int(value))
    return f"{value:.{digits}f}"


def fmt_signed(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:+.{digits}f}{suffix}"


def fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:.{digits}f}%"


def metric_value(value: float | None, metric_label: str = "", unit: str = "") -> str:
    """按单位粒度选择小数位：延时用 ms 整数、比率保留 2 位、QPS 保留 1 位。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if unit == "ms":
        return f"{value:.0f}ms"
    if unit in ("%",):
        return f"{value:.2f}%"
    if unit == "req/s":
        return f"{value:.1f}"
    if unit == "count":
        return f"{value:.0f}"
    return f"{value:.2f}"


def short_service(service: str, max_len: int = 18) -> str:
    return service if len(service) <= max_len else service[: max_len - 1] + "…"


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render_template(template: str, context: dict[str, Any]) -> str:
    """渲染原因模板。

    未知占位符保留原样而不是抛异常——配置是给人改的，
    写错一个键不应该让整次巡检崩掉，报告里能看到 {xxx} 反而便于排错。
    """
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            return match.group(0)
        return str(context[key])

    return _PLACEHOLDER.sub(repl, template)


def truncate(text: str, limit: int = 120, ellipsis: str = "…") -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - len(ellipsis)] + ellipsis


def bullet(lines: list[str], symbol: str = "-") -> str:
    return "\n".join(f"{symbol} {line}" for line in lines)


def sparkline(values: list[float]) -> str:
    """纯文本迷你趋势图，用于控制台与 Markdown 报告里的评分走势。"""
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    if hi == lo:
        return blocks[3] * len(values)
    span = hi - lo
    return "".join(blocks[min(len(blocks) - 1, int((v - lo) / span * (len(blocks) - 1)))] for v in values)
