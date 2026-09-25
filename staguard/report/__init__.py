"""报告输出包：控制台 / Markdown / JSON，以及历史对比。"""

from . import compare, console, markdown
from .compare import anomaly_signature, build_comparison, build_history
from .markdown import render as render_markdown

__all__ = [
    "anomaly_signature",
    "build_comparison",
    "build_history",
    "compare",
    "console",
    "markdown",
    "render_markdown",
]
