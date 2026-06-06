"""k2g.reader — read-only query facade over DbStore.

MCP / Web / CLI inject this Protocol and perform queries through a common path.

Public API:
    QueryService (Protocol), SearchMode
    SearchHit / EntityMatch / ContextDetail (result dataclasses)
    GraphQueryService (default DbStore-based implementation)
"""

from k2g.reader.base import (
    ContextDetail,
    EntityMatch,
    QueryService,
    SearchHit,
    SearchMode,
)
from k2g.reader.graph_query import GraphQueryService
from k2g.reader.influence import EventInfluenceService

__all__ = [
    "QueryService",
    "SearchMode",
    "SearchHit",
    "EntityMatch",
    "ContextDetail",
    "GraphQueryService",
    "EventInfluenceService",
]
