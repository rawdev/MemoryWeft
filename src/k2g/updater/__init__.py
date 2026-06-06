"""k2g.updater — Incremental build.

The Producer uses this to decide whether segments need to be rebuilt.

Public API:
    Updater (Protocol)
    FileCheckResult / SegmentAction  (check result dataclasses)
    IncrementalBuilder               (default implementation)
    BuildManifestStore               (SQLite manifest DB)
"""

from k2g.updater.base import (
    FileActionKind,
    FileCheckResult,
    SegmentAction,
    SegmentActionKind,
    Updater,
)
from k2g.updater.incremental import IncrementalBuilder
from k2g.updater.manifest import BuildManifestStore

__all__ = [
    "Updater",
    "FileCheckResult",
    "SegmentAction",
    "FileActionKind",
    "SegmentActionKind",
    "IncrementalBuilder",
    "BuildManifestStore",
]
