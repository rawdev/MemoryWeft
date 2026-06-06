"""Single helper that normalises staging event inline_meta schema differences
across producers.

Single source of truth for the schema-branching logic used at three call
sites: loader matching, _shared manifest_rows, and producer file_key_fn.

Per-producer schema differences:
- doc_batch: ``path_parts`` (list) + ``segment_key`` + ``segment_index``
- novel:     ``source_path`` (str) + ``segment_index`` + ``segment_key`` (optional)
- doxygen:   ``file_path`` (str) + ``fqn`` (used as segment_key). No segment_index.
- vcs:       load() stub — file_key_fn is never called.

This module handles *read normalisation* only. *Write normalisation*
(each producer populating its inline_meta) is handled by
``k2g.staging.event_builder.build_inline_meta``, the inverse function,
with a round-trip guarantee:

    keys = StagingEventBuildKeys(file_path="docs/a.md", segment_key="S0", segment_index=0)
    meta = build_inline_meta(keys, schema="doc_batch")
    out = extract_event_keys({"inline_meta": meta})
    assert out.file_path == keys.file_path
    assert out.segment_key == keys.segment_key
    assert out.segment_index == keys.segment_index

loader._extract_staging_keys was promoted to this module as a proper public
helper, with file_key_for_grouping added. The deprecated wrapper will be
removed once the migration is complete.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StagingEventKeys:
    """Producer-agnostic staging event identifier.

    Fields:
        file_path: Matched against the manifest WHERE clause
            (build_segment/file_manifest.file_path).
            Normalised: backslash → slash, leading/trailing slashes stripped.
        segment_key: Matched against the manifest WHERE clause.
            doc_batch segment_key or doxygen fqn (both uniquely identify
            a segment).
        segment_index: Populated for doc_batch only; None for doxygen.
        file_key_for_grouping: Batch grouping key used by stream_load_by_file.
            Mostly identical to file_path. Falls back to fqn.split("::")[0]
            (doxygen legacy) or local_ref (not reached for normal events).
    """
    file_path: str
    segment_key: str | None
    segment_index: int | None
    file_key_for_grouping: str


def extract_event_keys(ev) -> StagingEventKeys:
    """Extract a normalised 4-field identifier from a staging event.

    Args:
        ev: StagedEvent instance. Only inline_meta (dict) and local_ref are
            accessed, so any dict-like object works (useful for tests).

    Returns:
        StagingEventKeys (frozen dataclass).
    """
    meta = getattr(ev, "inline_meta", None) or {}

    # 1. Resolve file_path (priority): file_path > source_path > path_parts join
    fp_raw = meta.get("file_path") or meta.get("source_path") or ""
    if not fp_raw:
        parts = meta.get("path_parts") or []
        fp_raw = "/".join(str(p) for p in parts) if parts else ""
    fp = (fp_raw or "").replace("\\", "/").strip("/")

    # 2. segment_key: segment_key (doc_batch/novel) > fqn (doxygen)
    seg_key = meta.get("segment_key") or meta.get("fqn")
    seg_idx = meta.get("segment_index")

    # 3. file_key_for_grouping: file_path first, fqn fallback (doxygen legacy),
    #    local_ref as last resort (not reached for normal events)
    fkey = fp
    if not fkey:
        fqn = meta.get("fqn") or ""
        if fqn:
            # doxygen legacy: fqn = "Project::path/to/file.py::ClassName"
            # The file part is the second segment (path/to/file.py), but
            # since this is a file_path-absent fallback we use the first
            # segment to preserve parity with the original doxygen
            # file_key_fn split("::")[0] behaviour.
            fkey = fqn.split("::")[0]
        else:
            fkey = getattr(ev, "local_ref", "") or ""

    return StagingEventKeys(
        file_path=fp,
        segment_key=seg_key,
        segment_index=seg_idx,
        file_key_for_grouping=fkey,
    )


__all__ = ["StagingEventKeys", "extract_event_keys"]
