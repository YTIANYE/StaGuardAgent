"""AI 智能分析包：证据包 -> 大模型归因 -> 校验 -> 降级兜底（两级）。"""

from .analyzer import AIAnalyzer
from .evidence import build_evidence
from .fallback import FallbackAttributor
from .history import HistoryMatch, find_matches, is_recurring, jaccard
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from .provider import (
    DisabledProvider,
    LLMProvider,
    LLMResponse,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderUnavailable,
    build_provider,
)

__all__ = [
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "AIAnalyzer",
    "DisabledProvider",
    "FallbackAttributor",
    "HistoryMatch",
    "LLMProvider",
    "LLMResponse",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderUnavailable",
    "build_evidence",
    "build_provider",
    "build_user_prompt",
    "find_matches",
    "is_recurring",
    "jaccard",
]
