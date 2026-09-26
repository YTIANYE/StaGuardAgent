"""配置加载与聚合。

`Settings` 是一次进程内所有配置的唯一入口，避免各处散落 `yaml.safe_load`。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..models import Topology
from .app import AppConfig
from .schemas import (
    RulesConfig,
    ScenariosConfig,
    ScoringConfig,
    ServicesConfig,
    ThresholdConfig,
)

CONFIG_FILES: dict[str, str] = {
    "services": "services.yaml",
    "thresholds": "thresholds.yaml",
    "rules": "rules.yaml",
    "scoring": "scoring.yaml",
    "scenarios": "scenarios.yaml",
}


class ConfigError(RuntimeError):
    """配置文件缺失或格式非法。"""


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射: {path}")
    return data


class Settings(BaseModel):
    """聚合配置对象。编排器与各模块只依赖它。"""

    app: AppConfig = Field(default_factory=AppConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    scenarios: ScenariosConfig = Field(default_factory=ScenariosConfig)

    @property
    def topology(self) -> Topology:
        return self.services.to_topology()

    # ---- 常用透传，减少调用方的 settings.app.xxx 噪音 ----
    @property
    def window_minutes(self) -> int:
        return self.app.window_minutes

    @property
    def granularity_seconds(self) -> int:
        return self.app.granularity_seconds

    @property
    def baseline_days(self) -> int:
        return self.app.baseline_days

    @property
    def baseline_min_samples(self) -> int:
        return self.app.baseline_min_samples


def build_settings(config_dir: Path | None = None, **app_overrides: Any) -> Settings:
    """加载全部 YAML 配置并叠加环境变量覆盖。"""
    app = AppConfig(**app_overrides)
    directory = Path(config_dir) if config_dir else app.config_dir

    raw = {key: load_yaml(directory / filename) for key, filename in CONFIG_FILES.items()}

    services = ServicesConfig(**raw["services"])
    dangling = services.undefined_clusters()
    if dangling:
        detail = "；".join(f"{name}({cluster})" for name, cluster in dangling)
        raise ConfigError(
            f"服务引用了未定义的集群，请在 services.yaml 顶层 clusters 里补上或改正：{detail}"
        )

    return Settings(
        app=app,
        services=services,
        thresholds=ThresholdConfig(**raw["thresholds"]),
        rules=RulesConfig(**raw["rules"]),
        scoring=ScoringConfig(**raw["scoring"]),
        scenarios=ScenariosConfig(**raw["scenarios"]),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级缓存。测试里用 `get_settings.cache_clear()` 重置。"""
    return build_settings()
