"""时间工具。

统一约定：
- **存储**一律用 epoch 整数（`ts_epoch`），它是绝对时间，不受时区与夏令时影响；
- **内存**里所有 datetime 都带同一时区的 tzinfo（默认 Asia/Shanghai）；
- **展示**直接用内存里的 datetime，不做二次转换。

这条约定看起来啰嗦，但它消灭了时序系统里最常见的一类 bug：
SQLite 的 DateTime 列不支持时区，一旦混用「有时区」和「无时区」的 datetime 做范围查询，
结果会静默地偏移数小时，而且很难被发现。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"


@lru_cache(maxsize=4)
def get_tz(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or os.getenv("STAGUARD_TIMEZONE", DEFAULT_TZ))


def now(tz_name: str | None = None) -> datetime:
    return datetime.now(get_tz(tz_name))


def to_epoch(ts: datetime) -> int:
    """带时区/无时区都能安全转 epoch：无时区时按配置时区解释，避免被当成 UTC。"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=get_tz())
    return int(ts.timestamp())


def from_epoch(epoch: int, tz_name: str | None = None) -> datetime:
    return datetime.fromtimestamp(epoch, tz=get_tz(tz_name))


def ensure_aware(ts: datetime, tz_name: str | None = None) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=get_tz(tz_name))


def floor_to(ts: datetime, seconds: int) -> datetime:
    """向下对齐到粒度的整数倍（按 epoch 对齐，跨时区也一致）。"""
    epoch = to_epoch(ts)
    return from_epoch(epoch - epoch % seconds, str(ts.tzinfo or get_tz()))


def parse_iso(raw: str, tz_name: str | None = None) -> datetime:
    """解析 ISO 时间串。不带时区偏移时按配置时区解释——这是最容易踩的坑。"""
    text = raw.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return ensure_aware(dt, tz_name)


def hour_of_day(ts: datetime) -> int:
    return ts.hour


def hour_of_week(ts: datetime) -> int:
    """0~167，周一 0 点为 0。动态基线按它取「历史同时段」样本。"""
    return ts.weekday() * 24 + ts.hour


def hour_of_week_label(slot: int) -> str:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return f"{weekdays[slot // 24]} {slot % 24:02d}:00"


def format_ts(ts: datetime | None, with_date: bool = True) -> str:
    if ts is None:
        return "-"
    return ts.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M")


def format_duration(minutes: float) -> str:
    if minutes < 1:
        return f"{int(minutes * 60)}秒"
    if minutes < 60:
        return f"{minutes:.0f}分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}小时"
    return f"{hours / 24:.1f}天"


def humanize_epoch(epoch: int) -> str:
    return format_ts(from_epoch(epoch))


def iter_buckets(start: datetime, end: datetime, step_seconds: int):
    """左闭右开地迭代时间桶起点。"""
    cur = start
    while cur < end:
        yield cur
        cur = cur + timedelta(seconds=step_seconds)


def run_id_for(ts: datetime, scenario_id: str | None = None) -> str:
    """可读的 run_id：run-20260925-100000-S1。便于日志检索与报告归档。"""
    base = f"run-{ts.strftime('%Y%m%d-%H%M%S')}"
    return f"{base}-{scenario_id}" if scenario_id else base
