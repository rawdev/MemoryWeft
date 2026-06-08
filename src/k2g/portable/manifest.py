"""BP-74 — ArchiveManifest schema and helpers.

The manifest is the first file in every `.mweft.tar.gz` archive. It pins the
schema version, source identity, options used at export time, and row counts
for verification at import time.

`compute_source_hash` is deterministic and order-independent — it lets the
importer detect that two archives contain the same domain snapshot even if
exported at different times.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0.0"
ARCHIVE_KIND = "mweft.domain_archive"


class ArchiveSource(BaseModel):
    """Identity of the MWeft instance that produced the archive."""

    project: str
    group: str
    domain: str
    embedding: dict = Field(default_factory=dict)
    host: str = ""
    user: str = ""
    k2g_version: str = ""


class ArchiveOptions(BaseModel):
    """Export-time toggles. Mirrors CLI flags."""

    include_segments: bool = True
    include_vectors: bool = True
    include_plan: bool = True
    include_audit: bool = False
    include_content: bool = True  # BP-86 — content_store.db raw text bodies


class ArchiveManifest(BaseModel):
    """Top-level manifest.json schema."""

    schema_version: str = SCHEMA_VERSION
    kind: str = ARCHIVE_KIND
    source: ArchiveSource
    exported_at: str
    options: ArchiveOptions = Field(default_factory=ArchiveOptions)
    row_counts: dict[str, int] = Field(default_factory=dict)
    source_hash: str = ""

    @classmethod
    def from_json(cls, raw: str | bytes) -> ArchiveManifest:
        return cls.model_validate_json(raw)

    def to_json(self, *, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)

    def is_compatible(self) -> bool:
        """Major-version compatibility check against this module's SCHEMA_VERSION."""
        try:
            this_major = SCHEMA_VERSION.split(".", 1)[0]
            archive_major = self.schema_version.split(".", 1)[0]
        except (AttributeError, IndexError):
            return False
        return this_major == archive_major


def compute_source_hash(
    event_ids: list[str] | tuple[str, ...],
    entity_ids: list[str] | tuple[str, ...],
) -> str:
    """Deterministic SHA256 over sorted event ids + entity ids.

    Order-independent: callers may pass IDs in any order.
    """
    h = hashlib.sha256()
    for eid in sorted(event_ids):
        h.update(eid.encode("utf-8"))
        h.update(b"\n")
    h.update(b"---\n")
    for eid in sorted(entity_ids):
        h.update(eid.encode("utf-8"))
        h.update(b"\n")
    return f"sha256:{h.hexdigest()}"


def utc_now_iso() -> str:
    """ISO 8601 UTC with `Z` suffix, second precision — manifest.exported_at."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
