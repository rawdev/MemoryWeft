"""k2g.staging — intermediate layer between Producer and Loader.

The Producer serializes NER + embedding + Object/Content results as ndjson + npy,
and the Loader loads them into Graph + Vector. The two stages can be separated by
a subprocess boundary (or process restart) to prevent memory accumulation during
long builds.

Protocol (contract):
    StageWriter, StageReader   — abstract interfaces for producer/loader
Records:
    StagedEvent / StagedEntity / StagedGroup / StagedCommit / StagingManifest
Union:
    StagedRecord   — union alias for the 4 record types (type-hint)
Implementations:
    StagingWriter (writer.py) — ndjson + npy append-based
    StagingLoader (loader.py) — streaming read + embedding memmap
Path helpers:
    k2g.staging.paths.*
"""
from k2g.staging import paths
from k2g.staging.loader import (
    StagingLoader,
    StagingLoadError,
    list_ready_domains,
)
from k2g.staging.protocol import StagedRecord, StageReader, StageWriter
from k2g.staging.schema import (
    StagedEntity,
    StagedEvent,
    StagedGroup,
    StagingManifest,
)
from k2g.staging.writer import StagingWriter

__all__ = [
    "paths",
    # Protocol + union
    "StageWriter",
    "StageReader",
    "StagedRecord",
    # Records
    "StagedEvent",
    "StagedEntity",
    "StagedGroup",
    "StagingManifest",
    # Implementations
    "StagingWriter",
    "StagingLoader",
    "StagingLoadError",
    "list_ready_domains",
]
