"""服务拓扑模型。

拓扑是「业务链路异常」判定与「根因簇传播聚类」的基础：只有知道谁依赖谁，
才能把一堆孤立异常收敛成「下游是根因、上游是影响面」。
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field


class ServiceNode(BaseModel):
    """一个服务（或外部依赖）。"""

    name: str
    instances: list[str] = Field(default_factory=list)
    tier: str | None = None
    criticality: float = 0.5
    """关键度 0~1，用于异常分级加权：命中核心链路的 P1 会触发更严格的评分封顶。"""
    external: bool = False
    depends_on: list[str] = Field(default_factory=list)
    """下游依赖列表（本服务调用它们）。"""
    owner: str | None = None
    description: str | None = None

    @property
    def instance_count(self) -> int:
        return len(self.instances)


class Topology(BaseModel):
    services: dict[str, ServiceNode] = Field(default_factory=dict)

    def node(self, name: str) -> ServiceNode | None:
        return self.services.get(name)

    def names(self) -> list[str]:
        return list(self.services)

    def all_instances(self) -> list[tuple[str, str]]:
        return [(s.name, i) for s in self.services.values() for i in s.instances]

    def downstream(self, service: str) -> list[str]:
        """直接下游。"""
        node = self.services.get(service)
        return list(node.depends_on) if node else []

    def upstream(self, service: str) -> list[str]:
        """直接上游（谁依赖我）。"""
        return [n.name for n in self.services.values() if service in n.depends_on]

    def ancestors(self, service: str) -> list[str]:
        """全部上游（含间接），BFS。"""
        return self._walk(service, self.upstream)

    def descendants(self, service: str) -> list[str]:
        """全部下游（含间接），BFS。"""
        return self._walk(service, self.downstream)

    def _walk(self, service: str, step) -> list[str]:  # noqa: ANN001
        seen: set[str] = set()
        queue = deque(step(service))
        order: list[str] = []
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            queue.extend(step(cur))
        return order

    def path_between(self, src: str, dst: str) -> list[str] | None:
        """src -> ... -> dst 的最短依赖路径（沿 depends_on 方向）。"""
        if src == dst:
            return [src]
        prev: dict[str, str] = {}
        queue = deque([src])
        seen = {src}
        while queue:
            cur = queue.popleft()
            for nxt in self.downstream(cur):
                if nxt in seen:
                    continue
                prev[nxt] = cur
                if nxt == dst:
                    path = [dst]
                    while path[-1] != src:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                seen.add(nxt)
                queue.append(nxt)
        return None

    def criticality_of(self, service: str) -> float:
        node = self.services.get(service)
        return node.criticality if node else 0.5

    def blast_radius(self, service: str) -> list[str]:
        """爆炸半径：受影响的上游服务（可能被连带打挂的那些）。"""
        return self.ancestors(service)

    def as_edges(self) -> list[dict[str, str]]:
        edges: list[dict[str, str]] = []
        for node in self.services.values():
            for dep in node.depends_on:
                edges.append({"source": node.name, "target": dep})
        return edges

    def as_prompt_dict(self) -> dict[str, list[str]]:
        """给 AI 证据包的紧凑形态：{服务: [下游...]}。"""
        return {n.name: list(n.depends_on) for n in self.services.values()}
