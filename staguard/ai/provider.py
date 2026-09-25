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
        choice = completion.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        content = _strip_code_fence((choice.message.content or "").strip())
        usage = getattr(completion, "usage", None)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderError(_describe_bad_json(content, finish_reason)) from exc
        if not isinstance(payload, dict):
            raise ProviderError(f"模型返回的 JSON 顶层不是对象（{type(payload).__name__}）")

        if finish_reason == "length":
            # JSON 侥幸解析成功但被截断，意味着后面的 findings 可能整段丢失。
            # 这种情况必须显式告警，否则报告会「看起来正常但内容少了一半」。
            logger.warning("模型输出因 max_tokens 上限被截断，结论可能不完整")

        return LLMResponse(
            payload=payload,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=latency_ms,
            raw=content,
            model=self.model,
            provider=self.name,
        )


def _strip_code_fence(content: str) -> str:
    """去掉模型自作主张包上的 Markdown 代码块围栏。

    提示词里已经明确要求「只输出 JSON」，但实测仍会偶发地把结果包在
    ```json ... ``` 里。为此重试一次是浪费——剥离围栏的成本是零。
    """
    if not content.startswith("```"):
        return content
    body = content[3:]
    if body.lower().startswith("json"):
        body = body[4:]
    return body.rsplit("```", 1)[0].strip()


def _describe_bad_json(content: str, finish_reason: str | None) -> str:
    """把「不是合法 JSON」翻译成能直接定位原因的信息。

    实测教训：只打印开头 200 字符毫无用处——JSON 若是被 max_tokens 截断，
    开头看起来完全正常，报错信息反而把人引向「模型格式不稳定」这种错误方向。
    真正有用的是**结尾**和 finish_reason。
    """
    head = content[:160].replace("\n", " ")
    tail = content[-160:].replace("\n", " ")
    if finish_reason == "length":
        cause = "输出被 max_tokens 上限截断（请调大 STAGUARD_LLM_MAX_TOKENS）"
    elif not content:
        cause = "模型返回了空内容"
    else:
        cause = "模型输出不是合法 JSON"
    return f"{cause}；finish_reason={finish_reason}；开头：{head}…；结尾：…{tail}"


def build_provider(config: AppConfig) -> LLMProvider:
    if config.llm_provider == "mock":
        return DisabledProvider("当前为 mock 模式，未接入真实模型")
    return OpenAICompatibleProvider(config)
