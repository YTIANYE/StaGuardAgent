"""数据库表结构（SQLAlchemy Core）。

刻意不用 ORM：巡检的写入是「批量灌数据 + 批量写结果」，读取是「按序列扫区间」，
这两类操作用 Core 的表达式更直观、更可控，而且换 SQLite / PostgreSQL 只需换 URL。

时间列统一用 `ts_epoch`（整数）做索引与范围查询，另存一列可读的 ISO 串供人肉排查。
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

# --------------------------------------------------------------------------- 指标
metric_points = Table(
    "metric_points",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # dataset_id: `archive` 表示长期归档（干净历史，供动态基线取样本），
    # 其余为场景标识（含故障注入的高精度数据）。两类数据分开存，是为了防止
    # 「故障数据污染自己的基线」——这是动态阈值落地时最容易出错的地方。
    Column("dataset_id", String(32), nullable=False),
    Column("granularity_seconds", Integer, nullable=False),
    Column("ts_epoch", Integer, nullable=False),
    Column("ts", String(32), nullable=False),
    Column("service", String(64), nullable=False),
    Column("instance", String(64), nullable=False),
    Column("metric", String(32), nullable=False),
    Column("value", Float, nullable=False),
)

Index(
    "ix_mp_series",
    metric_points.c.dataset_id,
    metric_points.c.metric,
    metric_points.c.service,
    metric_points.c.instance,
    metric_points.c.ts_epoch,
)
Index("ix_mp_scan", metric_points.c.dataset_id, metric_points.c.granularity_seconds, metric_points.c.ts_epoch)

# --------------------------------------------------------------------------- 巡检运行
runs = Table(
    "runs",
    metadata,
    Column("run_id", String(96), primary_key=True),
    Column("scenario_id", String(32)),
    Column("window_start", String(32), nullable=False),
    Column("window_end", String(32), nullable=False),
    Column("granularity_seconds", Integer, nullable=False),
    Column("started_at", String(32), nullable=False),
    Column("finished_at", String(32)),
    Column("status", String(16), nullable=False),
    Column("source", String(16), nullable=False),
    Column("anomaly_count", Integer, default=0),
    Column("level_counts", JSON),
    Column("score", Float),
    Column("ai_degraded", Boolean, default=False),
    Column("duration_ms", Integer, default=0),
    Column("started_at_epoch", Integer, nullable=False),
)

Index("ix_runs_started", runs.c.started_at_epoch)

# --------------------------------------------------------------------------- 异常与簇
anomalies = Table(
    "anomalies",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(96), nullable=False),
    Column("anomaly_id", String(160), nullable=False),
    Column("rule_id", String(16), nullable=False),
    Column("rule_name", String(64), nullable=False),
    Column("dimension", String(16), nullable=False),
    Column("service", String(64), nullable=False),
    Column("instance", String(64), nullable=False),
    Column("metric", String(32), nullable=False),
    Column("level", String(4), nullable=False),
    Column("observed", Float),
    Column("baseline_value", Float),
    Column("deviation_pct", Float),
    Column("z_score", Float),
    Column("deviation_score", Float),
    Column("duration_minutes", Float),
    Column("first_seen", String(32)),
    Column("last_seen", String(32)),
    Column("level_reason", Text),
    Column("baseline_mode", String(20)),
    Column("baseline_confidence", String(10)),
    Column("cluster_id", String(16)),
    Column("merged_count", Integer, default=1),
    Column("is_suppressed", Boolean, default=False),
    Column("suppressed_by", String(160)),
    Column("evidence_json", JSON),
)
Index("ix_anomalies_run", anomalies.c.run_id)

clusters = Table(
    "clusters",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(96), nullable=False),
    Column("cluster_id", String(16), nullable=False),
    Column("signature", String(512), nullable=False),
    Column("primary_anomaly_id", String(160), nullable=False),
    Column("max_level", String(4), nullable=False),
    Column("size", Integer, default=1),
    Column("propagation_path", JSON),
    Column("services", JSON),
    Column("hypothesis", Text),
    Column("root_cause_category", String(32)),
    Column("root_cause", Text),
    Column("created_at_epoch", Integer, nullable=False),
)
Index("ix_clusters_run", clusters.c.run_id)
# 签名索引用于「跨 run 复发识别」：同样的异常组合再次出现时能立刻关联到历史。
# 归因的「记忆」不需要向量库——一个稳定的签名加一个索引就够了。
Index("ix_clusters_signature", clusters.c.signature)

# --------------------------------------------------------------------------- AI 结果
ai_results = Table(
    "ai_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(96), nullable=False),
    Column("payload", JSON),
    Column("meta", JSON),
)
Index("ix_ai_run", ai_results.c.run_id)

# --------------------------------------------------------------------------- 基线快照
baselines = Table(
    "baselines",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(96), nullable=False),
    Column("service", String(64), nullable=False),
    Column("instance", String(64), nullable=False),
    Column("metric", String(32), nullable=False),
    Column("median", Float),
    Column("mad", Float),
    Column("p95", Float),
    Column("sample_size", Integer),
    Column("mode", String(20)),
)
Index("ix_baselines_run", baselines.c.run_id)

# --------------------------------------------------------------------------- 报告
reports = Table(
    "reports",
    metadata,
    Column("run_id", String(96), primary_key=True),
    Column("score", Float),
    Column("summary", Text),
    Column("markdown", Text),
    Column("payload", JSON),
    Column("created_at_epoch", Integer, nullable=False),
)

# --------------------------------------------------------------------------- 变更事件
changes = Table(
    "changes",
    metadata,
    Column("change_id", String(48), primary_key=True),
    Column("scenario_id", String(32)),
    Column("ts_epoch", Integer, nullable=False),
    Column("ts", String(32), nullable=False),
    Column("service", String(64), nullable=False),
    Column("type", String(24), nullable=False),
    Column("version", String(32)),
    Column("operator", String(64)),
    Column("description", Text),
    Column("note", Text),
)
Index("ix_changes_ts", changes.c.ts_epoch)
