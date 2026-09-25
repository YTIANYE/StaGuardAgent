"""历史事件记忆与复发识别。

**不用向量库**。巡检的历史检索有个很好的性质：异常簇已经被归一成一组
`服务.指标` 的 token 签名，两次同类故障的签名天然高度重合。
于是「相似度」用 Jaccard 就够了，而且**完全可解释**——
报告里能直接写出「与 3 天前的 run-xxx 相似度 0.86，当时根因是依赖故障」，
向量相似度反而说不出这句话。

复杂度换来的是：零额外依赖、零额外存储组件、结果可审计。
对一个单机可跑的巡检系统来说，这是明显更划算的取舍。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RECURRENCE_THRESHOLD = 0.6
"""判定为复发的相似度门槛。太低会把无关历史当成复发，太高则认不出同类问题。"""


@dataclass
class HistoryMatch:
    signature: str
    run_id: str
    root_cause_category: str | None
    root_cause: str | None
    similarity: float
    max_level: str

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "run_id": self.run_id,
            "root_cause_category": self.root_cause_category or "unknown",
            "root_cause": self.root_cause,
            "similarity": round(self.similarity, 2),
            "level": self.max_level,
        }


def signature_tokens(signature: str) -> set[str]:
    return {token for token in signature.split("&") if token}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union)


def find_matches(
    signature: str,
    recent_clusters: list[dict[str, Any]],
    limit: int = 3,
) -> list[HistoryMatch]:
    """在历史簇里找与当前签名最相似的几条。"""
    tokens = signature_tokens(signature)
    if not tokens:
        return []
    scored: list[HistoryMatch] = []
    for row in recent_clusters:
        similarity = jaccard(tokens, signature_tokens(str(row.get("signature") or "")))
        if similarity <= 0:
            continue
        scored.append(
            HistoryMatch(
                signature=str(row.get("signature") or ""),
                run_id=str(row.get("run_id") or ""),
                root_cause_category=row.get("root_cause_category"),
                root_cause=row.get("root_cause"),
                similarity=similarity,
                max_level=str(row.get("max_level") or ""),
            )
        )
    scored.sort(key=lambda m: m.similarity, reverse=True)
    return scored[:limit]


def is_recurring(matches: list[HistoryMatch]) -> bool:
    return bool(matches) and matches[0].similarity >= RECURRENCE_THRESHOLD
