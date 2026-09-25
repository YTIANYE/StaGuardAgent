"""领域模型包：所有跨模块传递的数据结构都在这里定义，业务代码不直接碰裸 dict。"""

from .ai import SCORE_ADJUST_LIMIT, AIAnalysis, AIMeta, Finding, RootCauseCategory, TrendJudgement
from .anomaly import Anomaly, AnomalyCluster, AnomalyFinding
from .baseline import MAD_TO_SIGMA, Baseline, BaselineSet
from .common import (
    BaselineMode,
    Confidence,
    Dimension,
    Severity,
    TimeWindow,
    floor_to_bucket,
)
from .metric import (
    METRIC_LABEL,
    METRIC_UNIT,
    DataQualityIssue,
    DataQualityReport,
    MetricName,
    MetricPoint,
    MetricSeries,
    percentile,
)
from .report import (
    InspectionReport,
    RunComparison,
    ScoreBreakdownItem,
    ScoreResult,
    ScoreSnapshot,
)
from .run import (
    STAGE_ORDER,
    ChangeEvent,
    ChangeType,
    InspectionRun,
    RunStatus,
    StageRecord,
    StageStatus,
)
from .topology import ServiceNode, Topology

__all__ = [
    "MAD_TO_SIGMA",
    "METRIC_LABEL",
    "METRIC_UNIT",
    "SCORE_ADJUST_LIMIT",
    "STAGE_ORDER",
    "AIAnalysis",
    "AIMeta",
    "Anomaly",
    "AnomalyCluster",
    "AnomalyFinding",
    "Baseline",
    "BaselineMode",
    "BaselineSet",
    "ChangeEvent",
    "ChangeType",
    "Confidence",
    "DataQualityIssue",
    "DataQualityReport",
    "Dimension",
    "Finding",
    "InspectionReport",
    "InspectionRun",
    "MetricName",
    "MetricPoint",
    "MetricSeries",
    "RootCauseCategory",
    "RunComparison",
    "RunStatus",
    "ScoreBreakdownItem",
    "ScoreResult",
    "ScoreSnapshot",
    "ServiceNode",
    "Severity",
    "StageRecord",
    "StageStatus",
    "TimeWindow",
    "Topology",
    "TrendJudgement",
    "floor_to_bucket",
    "percentile",
]
