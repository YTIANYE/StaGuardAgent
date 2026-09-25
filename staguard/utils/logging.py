"""日志初始化。

两种输出形态：
- `console`：人读，带颜色与时间戳，开发与演示用；
- `json`：机器读，一行一个 JSON，采集到 ELK / Loki 后可以按 run_id 直接检索。

关键点是把 **stdlib logging 也接到 structlog 的处理链上**：
否则日志会出现两种格式并存——自己打的日志是 JSON，
第三方库（httpx、sqlalchemy）打的是纯文本，采集侧直接崩掉。
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def setup_logging(level: str = "INFO", fmt: str = "console") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty(), pad_event=28)
    )

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))


def bind_run_context(run_id: str, scenario_id: str | None = None) -> None:
    """把 run_id 绑到上下文变量上，之后所有日志自动带上它。

    巡检日志的价值一半在「这一轮发生了什么」，没有 run_id 的日志在并发场景下就是一团乱麻。
    """
    structlog.contextvars.bind_contextvars(run_id=run_id, scenario=scenario_id or "-")


def clear_run_context() -> None:
    structlog.contextvars.clear_contextvars()
