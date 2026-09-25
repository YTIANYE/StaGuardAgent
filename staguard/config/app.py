"""运行期配置（环境变量 / .env）。

约定：所有配置项都可以用 `STAGUARD_` 前缀的环境变量覆盖，默认值保证「零配置可跑」。
路径类配置默认锚定项目根目录，避免从不同工作目录启动时行为不一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AppConfig(BaseSettings):
    """应用级配置。"""

    model_config = SettingsConfigDict(
        env_prefix="STAGUARD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: Path = Field(default=PROJECT_ROOT)
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    config_dir: Path = Field(default=PROJECT_ROOT / "config")
    report_dir: Path = Field(default=PROJECT_ROOT / "reports")
    log_dir: Path = Field(default=PROJECT_ROOT / "logs")

    timezone: str = "Asia/Shanghai"
    """全链路统一时区。存储用 epoch（绝对时间），内存里的 datetime 一律带这个时区的 tzinfo，
    这样 hour-of-week 基线与报告展示的自然日/小时都对得上，不会出现 UTC 偏移 8 小时的口径错乱。"""

    # ---- 存储 ----
    db_url: str = "sqlite:///./data/staguard.db"
    """SQLAlchemy URL。默认 SQLite（零依赖可跑），改一行即可切 PostgreSQL。"""

    # ---- 巡检 ----
    window_minutes: int = 30
    """巡检窗口长度。"""
    granularity_seconds: int = 60
    """指标粒度，同时决定时间桶对齐。"""
    baseline_days: int = 14
    """动态基线回看天数。hour-of-week 基线至少要 1 周样本，默认取 2 周。"""
    baseline_min_samples: int = 30
    """低于该样本量即回退静态阈值。"""
    max_instances_per_service: int = 6

    # ---- 数据源 ----
    default_source: Literal["file", "http"] = "file"
    mock_api_base_url: str = "http://127.0.0.1:8090"
    mock_api_timeout_s: float = 10.0
    http_source_trust_env: bool = False
    """是否让 HTTP 采集通道读取系统/环境代理设置。

    **默认关闭**，因为踩过一次：httpx 默认会读取操作系统代理配置，
    在有系统代理的机器上访问本机或内网的监控接口会被代理转发并返回 502，
    而错误信息看起来像「监控服务挂了」，排查方向完全被带偏。
    监控接口一般在内网直连，除非确有必要走代理，否则应保持关闭。
    """

    # ---- 日志 ----
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # ---- LLM ----
    llm_enabled: bool = True
    llm_provider: Literal["deepseek", "openai", "mock"] = "deepseek"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_timeout_s: float = 60.0
    llm_max_attempts: int = 2
    llm_temperature: float = 0.2
    llm_max_concurrency: int = 3
    llm_max_tokens: int = 4096
    """单次输出上限。

    实测教训：2048 在「一个簇里塞了 50 条异常」的场景下会被截断，
    表现为 JSON 解析失败——而且报错信息看起来像「模型格式不稳定」，
    很容易把人引向调提示词这种错误方向。代价只有几毛钱，留足余量。"""

    # ---- 告警 ----
    alert_webhook_url: str | None = None
    alert_min_level: str = "P2"
    alert_dry_run: bool = True

    # ---- 服务化 ----
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    @field_validator("data_dir", "config_dir", "report_dir", "log_dir", mode="after")
    @classmethod
    def _ensure_dir(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @property
    def has_llm_credentials(self) -> bool:
        if not self.llm_enabled:
            return False
        if self.llm_provider == "mock":
            return True
        return bool(self.llm_api_key)

    def db_file(self) -> Path:
        """SQLite 数据库文件路径（用于日志与健康检查展示）。"""
        if not self.db_url.startswith("sqlite"):
            return Path(self.db_url)
        raw = self.db_url.split("///", 1)[-1]
        path = Path(raw)
        return path if path.is_absolute() else (self.project_root / path).resolve()
