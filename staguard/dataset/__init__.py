"""数据集包：场景化模拟数据的生成与加载。"""

from .changes import build_change_events, load_change_specs
from .generator import (
    ARCHIVE_DATASET,
    BuildPlan,
    BuildReport,
    GenerationResult,
    ScenarioWorld,
    WorldGrid,
    build_plans,
)
from .profiles import COUPLING_NOTES, SERVICE_PROFILES

__all__ = [
    "ARCHIVE_DATASET",
    "COUPLING_NOTES",
    "SERVICE_PROFILES",
    "BuildPlan",
    "BuildReport",
    "GenerationResult",
    "ScenarioWorld",
    "WorldGrid",
    "build_change_events",
    "build_plans",
    "load_change_specs",
]
