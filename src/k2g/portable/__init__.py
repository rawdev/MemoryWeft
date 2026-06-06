"""BP-74 — MWeft Domain Archive Export/Import.

Public API for exporting a domain to a portable `.mweft.tar.gz` archive and
importing it back into a (possibly different) MWeft instance.

Use cases — see BP-74 §7:
- Local backup → cloud disk mount folder (Google Drive, OneDrive, etc.)
- Multi-device hand-off (one-way via cloud sync)
- Domain sharing between users
- Bug report bundles
- SQLite ↔ Postgres migration
"""

from __future__ import annotations

from k2g.portable.exporter import DomainExporter
from k2g.portable.importer import (
    ConflictStrategy,
    DomainImporter,
    SchemaVersionMismatch,
)
from k2g.portable.manifest import (
    ARCHIVE_KIND,
    SCHEMA_VERSION,
    ArchiveManifest,
    ArchiveOptions,
    ArchiveSource,
    compute_source_hash,
    utc_now_iso,
)

__all__ = [
    "ARCHIVE_KIND",
    "SCHEMA_VERSION",
    "ArchiveManifest",
    "ArchiveOptions",
    "ArchiveSource",
    "ConflictStrategy",
    "DomainExporter",
    "DomainImporter",
    "SchemaVersionMismatch",
    "compute_source_hash",
    "utc_now_iso",
]
