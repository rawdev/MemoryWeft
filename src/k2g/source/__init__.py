"""Source Lineage Abstraction.

Standardizes the link between K2G and originals as a ``(provider, source_id,
locator)`` 3-tuple. Storage format per content type is free-form (RDB /
external DB / git / S3 / IMAP / ...).
"""
from __future__ import annotations

from k2g.source.provider import (
    SourceContent,
    SourceMeta,
    SourceNotFoundError,
    SourcePermissionError,
    SourceProvider,
    SourceProviderError,
    SourceRef,
)


__all__ = [
    "SourceRef",
    "SourceContent",
    "SourceMeta",
    "SourceProvider",
    "SourceProviderError",
    "SourceNotFoundError",
    "SourcePermissionError",
]
