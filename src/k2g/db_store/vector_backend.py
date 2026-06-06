"""VectorBackend Protocol — vector backend abstraction for db_store.

The Qdrant implementation has been moved to legacy/. PgVectorStore and
SqliteVectorStore are the two implementations that structurally satisfy
this Protocol.

Method groups:
    (a) Event vector CRUD (upsert / search / get / get_vectors)
    (b) Entity vector CRUD (upsert_entity_vector / get_entity_vector /
        search_similar_entities / delete_entity_vector)
    (c) Lifecycle (get_collection_info / ping / close)

Note: VectorMetadata is a dataclass defined in k2g.core.models for
Qdrant payload serialization. The Postgres backend also accepts the
same object and maps it internally to JSONB / columns.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from k2g.core.models import VectorMetadata


@runtime_checkable
class VectorStoreProtocol(Protocol):
    """K2G VectorStore public contract.

    Any backend (Qdrant / pgvector) must satisfy this Protocol to be
    swappable at the MCP / CLI / build_agent / web layer.
    """

    # ------------------------------------------------------------------
    # (a) Event vector CRUD
    # ------------------------------------------------------------------

    def upsert(
        self,
        vector_id: str,
        vector: list[float],
        metadata: VectorMetadata,
    ) -> str:
        ...

    def search(
        self,
        query_vector: list[float],
        filter_domain: str | None = None,
        limit: int = 10,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        ...

    def get(self, vector_id: str) -> dict[str, Any] | None:
        ...

    def get_vectors(self, vector_ids: list[str]) -> dict[str, list[float]]:
        ...

    # ------------------------------------------------------------------
    # (b) Entity vector CRUD
    # ------------------------------------------------------------------

    def upsert_entity_vector(
        self,
        entity_id: str,
        vector: list[float],
        metadata: dict[str, Any],
    ) -> None:
        ...

    def get_entity_vector(self, entity_id: str) -> dict[str, Any] | None:
        ...

    def search_similar_entities(
        self,
        query_vector: list[float],
        domain: str | None = None,
        limit: int = 10,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        ...

    def delete_entity_vector(self, entity_id: str) -> None:
        ...

    # ------------------------------------------------------------------
    # (c) Lifecycle
    # ------------------------------------------------------------------

    def get_collection_info(self) -> dict[str, Any]:
        ...

    def ping(self) -> bool:
        ...

    def close(self) -> None:
        ...
