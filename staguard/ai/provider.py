"""大模型接入层。

只做一件事：把「结构化证据包」送进去、把「结构化 JSON 结论」取回来。
提示词、校验、降级都在上层，这里只负责通信与重试——这样换模型（DeepSeek / OpenAI /
任何 OpenAI 兼容网关）不需要动归因逻辑一行代码。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import AppConfig

logger = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    """未配置凭据、被显式禁用，或依赖库缺失。上层据此走规则降级。"""


class ProviderError(RuntimeError):
    """调用失败（网络、限流、服务端错误）。可重试。"""


@dataclass
class LLMResponse:
    payload: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    raw: str = ""
    model: str = ""
    provider: str = ""
    notes: list[str] = field(default_factory=list)


class LLMProvider(Protocol):
    name: str
    model: str

    def available(self) -> tuple[bool, str | None]:
        """返回 (是否可用, 不可用原因)。"""

    def complete_json(self, system: str, user: str) -> LLMResponse:
        """要求模型返回 JSON。失败抛 ProviderError。"""


class DisabledProvider:
    """未配置模型时使用。返回不可用，让上层走规则归因。"""

    name = "disabled"
    model = "none"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def available(self) -> tuple[bool, str | None]:
        return False, self.reason

    def complete_json(self, system: str, user: str) -> LLMResponse:  # noqa: ARG002
        raise ProviderUnavailable(self.reason)


class OpenAICompatibleProvider:
    """OpenAI 兼容接口（DeepSeek / OpenAI / 各类网关）。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.name = config.llm_provider
        self.model = config.llm_model
        self._client = None

    def available(self) -> tuple[bool, str | None]:
        if not self.config.llm_enabled:
            return False, "配置中已关闭 AI 分析（STAGUARD_LLM_ENABLED=false）"
        if not self.config.llm_api_key:
            return False, "未配置 llm_api_key，无法调用大模型"
        try:
            self._ensure_client()
        except ProviderUnavailable as exc:
            return False, str(exc)
        return True, None

    def _ensure_client(self):  # noqa: ANN202
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 依赖缺失属于环境问题
            raise ProviderUnavailable("未安装 openai SDK") from exc
        self._client = OpenAI(
            api_key=self.config.llm_api_key,
            base_url=self.config.llm_base_url,
            timeout=self.config.llm_timeout_s,
            max_retries=0,  # 重试策略由本模块统一控制，避免双重退避
        )
        return self._client

    def complete_json(self, system: str, user: str) -> LLMResponse:
        client = self._ensure_client()
        started = time.perf_counter()
        try:
            completion = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - SDK 异常层次不稳定，统一转成可重试错误
            raise ProviderError(f"{exc.__class__.__name__}: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        content = (completion.choices[0].message.content or "").strip()
        usage = getattr(completion, "usage", None)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"模型返回的内容不是合法 JSON：{content[:200]}") from exc
        if not isinstance(payload, dict):
            raise ProviderError("模型返回的 JSON 顶层不是对象")

        return LLMResponse(
            payload=payload,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=latency_ms,
            raw=content,
            model=self.model,
            provider=self.name,
        )


def build_provider(config: AppConfig) -> LLMProvider:
    if config.llm_provider == "mock":
        return DisabledProvider("当前为 mock 模式，未接入真实模型")
    return OpenAICompatibleProvider(config)
