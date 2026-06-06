"""ContentBackend Protocol — content metadata store abstraction.

PostgresContentStore / SqliteContentStore structurally conform to this
Protocol. content_store table: content_id / domain / vector_id /
content_type / storage_uri / inline_meta / created_at.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from k2g.core.models import ContentRecord


@runtime_checkable
class ContentBackend(Protocol):
    """Content metadata storage interface."""

    def save(
        self,
        *,
        domain: str,
        vector_id: str,
        content_type: str,
        storage_uri: str,
        inline_meta: dict,
    ) -> str:
        """Assign content_id and persist metadata. Returns content_id."""
        ...

    def get(self, content_id: str) -> ContentRecord | None: ...

    def get_by_vector_id(self, vector_id: str) -> ContentRecord | None: ...

    def mark_orphan(self, content_id: str) -> None:
        """Compensating transaction — logical deactivation without physical delete."""
        ...

    def close(self) -> None: ...


__all__ = ["ContentBackend"]
