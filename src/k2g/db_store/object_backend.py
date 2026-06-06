"""ObjectBackend Protocol — raw/binary storage abstraction.

LocalObjectStorage structurally conforms to this Protocol. To add S3/GCS
backends in the future, just implement a new class that satisfies the
same Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ObjectBackend(Protocol):
    """Raw/binary storage interface."""

    def put_text(
        self,
        text: str,
        *,
        content_type: str = "text/plain",
        domain: str = "",
        hint: str = "",
    ) -> str:
        """Store text and return storage_uri (e.g. file://...)."""
        ...

    def get(self, storage_uri: str) -> bytes:
        """Load binary content from storage_uri."""
        ...

    def get_text(self, storage_uri: str, encoding: str = "utf-8") -> str: ...

    def close(self) -> None: ...


__all__ = ["ObjectBackend"]
