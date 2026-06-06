"""QueryService Protocol + shared result dataclasses.

MCP / Web / CLI depend on this Protocol so they all go through a single
facade instead of separate DbStore call paths.  `GraphQueryService` is
the default implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


SearchMode = Literal["hybrid", "events", "entities"]


@dataclass
class SearchHit:
    kind: Literal["event", "entity"]
    id: str
    score: float
    domain: str
    name_or_summary: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EntityMatch:
    entity: dict  # {id, name, domain, type, user_tag?}
    events: list[dict] = field(default_factory=list)
    total: int = 0


@dataclass
class ContextDetail:
    cg_id: str
    name: str
    narrative_summary: str = ""
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    ancestors: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)


@runtime_checkable
class QueryService(Protocol):
    """Read-only query facade shared by MCP / Web / CLI."""

    def search(
        self,
        query: str,
        *,
        domain: str,
        mode: SearchMode = "hybrid",
        top_k: int = 10,
    ) -> list[SearchHit]: ...

    def entity_lookup(
        self, name: str, *, domain: str,
    ) -> list[EntityMatch]: ...

    def context_detail(
        self, cg_id: str, *, depth: int = 2,
    ) -> ContextDetail | None: ...


__all__ = [
    "QueryService",
    "SearchHit",
    "EntityMatch",
    "ContextDetail",
    "SearchMode",
]
