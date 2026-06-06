"""SourceProvider Protocol + SourceRef dataclass.

Standardizes the K2G ↔ source connection as a
``(provider, source_id, locator)`` 3-tuple. Storage details are
determined by the per-provider backend
(RDB / external DB / git / S3 / IMAP / ...).

Design doc: ``BP-50`` §2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


__all__ = [
    "SourceRef",
    "SourceContent",
    "SourceMeta",
    "SourceProvider",
    "SourceNotFoundError",
    "SourceProviderError",
    "SourcePermissionError",
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SourceProviderError(RuntimeError):
    """General SourceProvider error."""


class SourceNotFoundError(SourceProviderError):
    """source_id does not exist in the backend.

    Lazy detection (``fetch_segment_safe``) catches this exception and
    marks the event's ``source_tombstoned_at``.
    """


class SourcePermissionError(SourceProviderError):
    """Write attempted on a read-only provider."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """Standard representation of a K2G ↔ source mapping.

    Maps 1:1 to the ``source_provider`` / ``source_id`` / ``source_locator``
    columns in the events table.
    """

    provider: str               # "text_blob_postgres" | "external_db" | "vcs" | ...
    source_id: str              # Source identifier (provider-specific semantics)
    locator: Mapping[str, Any]  # Exact location within the source (provider-specific schema)
    version: str | None = None  # External backend row version (optional; for stale detection)


@dataclass(frozen=True)
class SourceContent:
    """Full source fetch result (``fetch_source``)."""

    source_id: str
    content: str                # Full source text
    byte_size: int
    sha256: str | None = None
    title: str | None = None
    source_uri: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceMeta:
    """Source existence + metadata (``head``)."""

    source_id: str
    exists: bool
    version: str | None = None
    byte_size: int | None = None
    sha256: str | None = None


# ---------------------------------------------------------------------------
# SourceProvider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceProvider(Protocol):
    """Standard interface for K2G ↔ source backends.

    Each content type (text_blob_postgres / external_db / vcs / doxygen /
    ...) implements this Protocol. K2G only knows the
    (provider, source_id, locator) 3-tuple; the provider decides storage.
    """

    # Metadata
    name: str
    permissions: Literal["read_only", "read_write"]

    # ---- Read ----
    def fetch_source(self, source_id: str) -> SourceContent:
        """Fetch the entire source.

        Meaningful only for ``store_raw=true`` providers. For
        ``store_raw=false`` providers returns the concatenated segments or
        raises SourceNotFoundError.

        Raises:
            SourceNotFoundError: source_id does not exist in the backend.
        """
        ...

    def fetch_segment(self, source_id: str, locator: Mapping[str, Any]) -> str:
        """Fetch only the segment identified by locator.

        Entry point for cache miss / re-extraction. Used when the event's
        storage_uri is stale.

        Raises:
            SourceNotFoundError: source_id has been removed (triggers lazy detection).
        """
        ...

    def head(self, source_id: str) -> SourceMeta:
        """Check existence + version without fetching content (cheap)."""
        ...

    def list_segments(self, source_id: str) -> list[Mapping[str, Any]]:
        """Return all segment locators for a source (for re-chunking / inspection)."""
        ...

    # ---- Write (read_write providers only) ----
    def store_source(
        self, content: str, *,
        source_id: str | None = None,
        domain: str = "",
        meta: Mapping[str, Any] | None = None,
    ) -> str:
        """Store the full source — opt-in archive.

        Returns: source_id (assigned by the provider).

        Raises:
            SourcePermissionError: provider is read-only.
        """
        ...

    def store_segment(
        self, source_id: str, locator: Mapping[str, Any], content: str, *,
        meta: Mapping[str, Any] | None = None,
    ) -> str:
        """Store a single segment — main truth.

        Returns: segment_id (assigned by the provider).

        Raises:
            SourcePermissionError: provider is read-only.
        """
        ...
