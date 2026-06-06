"""Updater Protocol + shared dataclasses for incremental builds.

Producer's `produce(unit, writer, updater=...)` depends only on this
Protocol.  The default implementation is
`k2g.updater.incremental.IncrementalBuilder` -- future heuristics
(e.g. timestamp-based, git-diff-based) can be swapped in without
producer-side changes as long as they satisfy the Protocol.

Ported from legacy `pipeline/incremental.py:FileCheckResult / SegmentAction`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

SegmentActionKind = Literal["ingest", "skip"]
FileActionKind = Literal["skip_file", "check_segments"]


@dataclass
class SegmentAction:
    """Check result for a single segment."""

    segment_key: str
    segment_index: int
    segment_hash: str
    action: SegmentActionKind
    old_vector_id: str | None = None


@dataclass
class FileCheckResult:
    """File-level check result."""

    action: FileActionKind
    file_hash: str
    segment_actions: list[SegmentAction] = field(default_factory=list)
    superseded_keys: list[str] = field(default_factory=list)


@runtime_checkable
class Updater(Protocol):
    """Contract for the incremental checker injected into Producer."""

    build_id: str

    # hash / key utility
    @staticmethod
    def compute_hash(content: str) -> str: ...
    @staticmethod
    def derive_segment_key(segment: dict, index: int) -> str: ...

    # check
    def check_and_prepare(
        self,
        *,
        domain: str,
        file_path: str,
        file_content: str,
        segments: list[dict],
    ) -> FileCheckResult: ...

    # record
    def record_result(
        self,
        *,
        domain: str,
        file_path: str,
        segment_key: str,
        segment_index: int,
        segment_hash: str,
        vector_id: str,
        event_id: str,
        old_vector_id: str | None,
    ) -> None: ...

    def finalize_file(
        self,
        *,
        domain: str,
        file_path: str,
        file_hash: str,
        segment_count: int,
        source_root: str | None = None,
    ) -> None: ...


__all__ = [
    "Updater",
    "SegmentAction",
    "FileCheckResult",
    "SegmentActionKind",
    "FileActionKind",
]
