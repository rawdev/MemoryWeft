"""Unified *write* helper for staging event inline_meta across producers.

The read helper (extract_event_keys in event_keys.py) normalises staging event
schema differences across producers. This module is its inverse — the *write*
helper used by all producers when populating the inline_meta dict.

Read/write symmetry contract:

    Write (producer):
        keys = StagingEventBuildKeys(file_path=..., segment_key=..., ...)
        inline_meta = build_inline_meta(keys, schema="doc_batch")
        # → {"path_parts": [...], "segment_key": ..., "segment_index": ...}

    Read (loader / _shared / manifest matching):
        out = extract_event_keys(staged_event)
        # → StagingEventKeys(file_path=..., segment_key=..., segment_index=..., ...)

    Round-trip guarantee:
        build_inline_meta(keys, schema) → inline_meta
        extract_event_keys({inline_meta: ...}).file_path == keys.file_path
        extract_event_keys({inline_meta: ...}).segment_key == keys.segment_key
        extract_event_keys({inline_meta: ...}).segment_index == keys.segment_index

Producer-specific metadata (file_extension / boundary_reason / compound_kind, etc.)
is passed via the `extra: dict` argument — preserving free-form metadata
beyond the manifest-matching keys.

Adding a future multimodal producer (PDF / image / audio) only requires
extending the single schema branch in `_BUILDERS`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SchemaName = Literal["doc_batch", "novel", "doxygen"]


@dataclass(frozen=True)
class StagingEventBuildKeys:
    """Manifest-matching key representation used by producers when writing
    staging events.

    Symmetric counterpart to StagingEventKeys (the read side).
    file_path must be normalised to unix-slash format — caller's responsibility.
    extra holds schema-specific side metadata
    (file_extension / boundary_reason / compound_kind, etc.).

    Fields:
        file_path: Matched against the manifest WHERE clause.
            Unix slash format, e.g. "docs/a.md".
        segment_key: Segment identifier. doc_batch/novel segment_key or
            doxygen fqn. If None, the key is omitted in the schema branch.
        segment_index: Used by doc_batch and novel only; absent for doxygen (None).
        extra: Producer-specific metadata beyond the round-trip keys.
            Treated as an empty dict when None.
    """
    file_path: str
    segment_key: str | None = None
    segment_index: int | None = None
    extra: dict[str, Any] | None = None


def build_inline_meta(
    keys: StagingEventBuildKeys,
    schema: SchemaName,
) -> dict[str, Any]:
    """Build the inline_meta dict for a staging event.

    Schema-specific branches — inverse of the extract_event_keys priority:
      - ``doc_batch``: ``{path_parts, segment_key, segment_index, **extra}``
      - ``novel``:     ``{source_path, segment_key, segment_index, **extra}``
      - ``doxygen``:   ``{file_path, fqn (= segment_key), **extra}``

    Args:
        keys: Manifest-matching keys (file_path required;
            segment_key and segment_index optional).
        schema: Producer schema name. Extend the Literal when adding new
            multimodal producers.

    Returns:
        dict ready to assign to NERContent.inline_meta or
        StagedEvent.inline_meta. The extra metadata is spread into the dict.

    Raises:
        ValueError: If schema is not a known value.
    """
    extra = keys.extra or {}
    builder = _BUILDERS.get(schema)
    if builder is None:
        raise ValueError(
            f"Unknown schema {schema!r}. "
            f"Allowed: {list(_BUILDERS.keys())}",
        )
    return builder(keys, extra)


def _build_doc_batch(keys: StagingEventBuildKeys, extra: dict) -> dict:
    """doc_batch schema — uses ``path_parts`` (list) format."""
    out: dict[str, Any] = {
        "path_parts": keys.file_path.split("/") if keys.file_path else [],
    }
    if keys.segment_key is not None:
        out["segment_key"] = keys.segment_key
    if keys.segment_index is not None:
        out["segment_index"] = keys.segment_index
    out.update(extra)
    return out


def _build_novel(keys: StagingEventBuildKeys, extra: dict) -> dict:
    """novel schema — uses ``source_path`` (str) format."""
    out: dict[str, Any] = {
        "source_path": keys.file_path,
    }
    if keys.segment_key is not None:
        out["segment_key"] = keys.segment_key
    if keys.segment_index is not None:
        out["segment_index"] = keys.segment_index
    out.update(extra)
    return out


def _build_doxygen(keys: StagingEventBuildKeys, extra: dict) -> dict:
    """doxygen schema — ``file_path`` (str) + ``fqn`` (= segment_key)."""
    out: dict[str, Any] = {
        "file_path": keys.file_path,
    }
    if keys.segment_key is not None:
        out["fqn"] = keys.segment_key
    # doxygen has no segment_index — extract_event_keys returns None for it too
    out.update(extra)
    return out


_BUILDERS: dict[str, Any] = {
    "doc_batch": _build_doc_batch,
    "novel": _build_novel,
    "doxygen": _build_doxygen,
}


__all__ = [
    "StagingEventBuildKeys",
    "build_inline_meta",
    "SchemaName",
]
