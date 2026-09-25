"""变更事件加载。

`data/changes.yaml` 用「相对巡检窗口结束时刻的偏移」描述时间，
这里把它换算成绝对时间——这样固定数据集时间点之后，变更时间也自动跟随，
不会出现「数据是 9 月 25 日、变更是 8 月」的错位。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..models import ChangeEvent, ChangeType

logger = logging.getLogger(__name__)

DEFAULT_FILE = "changes.yaml"


class ChangeLoadError(RuntimeError):
    pass


def load_change_specs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        logger.warning("变更事件文件不存在，AI 将拿不到变更证据: %s", path)
        return []
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    specs = data.get("changes") or []
    if not isinstance(specs, list):
        raise ChangeLoadError(f"changes.yaml 的 changes 字段必须是列表: {path}")
    return specs


def build_change_events(
    path: Path, window_end: datetime
) -> tuple[list[ChangeEvent], dict[str, str]]:
    """把相对时间换算成绝对时间。

    返回 (事件列表, {change_id: scenario_id})——scenario_id 为空的表示背景变更，
    对所有场景都可见（刻意的干扰项）。
    """
    events: list[ChangeEvent] = []
    scenario_map: dict[str, str] = {}
    for spec in load_change_specs(path):
        minutes = float(spec.get("minutes_before_end", 0))
        ts = window_end + timedelta(minutes=minutes)
        change_id = spec["change_id"]
        events.append(
            ChangeEvent(
                change_id=change_id,
                ts=ts,
                service=spec["service"],
                type=ChangeType(spec.get("type", "release")),
                version=spec.get("version"),
                operator=spec.get("operator"),
                description=spec.get("description"),
            )
        )
        if spec.get("scenario"):
            scenario_map[change_id] = str(spec["scenario"])
    return events, scenario_map
