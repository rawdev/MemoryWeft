"""Manifest schema parser and validator (zero LLM calls, pure data layer).

An AI writes ``[content, summary, entities]`` bundles as a JSON manifest
via incremental append, then the CLI ``ManifestProducer`` reads and loads
them into the graph.  This module parses and validates that JSON — it is a
pure data layer with no graph/embedding/LLM dependencies (unit-testable).

Validation policy: **all-or-nothing**.  If any item has a missing or
over-length content/summary, violates the source enum, or exceeds the total
size limit, the entire manifest is rejected with :class:`ManifestError`
(no partial loading).  Execution is also refused unless ``complete:true``
is set (protection against loading a manifest still being appended to).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Constants (matching single-remember limits + total-size cap) ──────────
MANIFEST_SCHEMA = "k2g.manifest.v1"
MAX_CONTENT_CHARS = 50_000        # per-item content (matches memory_tools)
MAX_SUMMARY_CHARS = 500           # per-item summary  (matches memory_tools)
MAX_ITEMS = 2_000                 # per-call item count cap (advisory, tune by measurement)
MAX_TOTAL_CONTENT_BYTES = 5_000_000   # Σ len(content.encode) ≤ ~5 MB (lock/memory)

# event_sequential_next.source CHECK enum (graph_backend.SequentialSource).
SEQUENTIAL_SOURCES = frozenset({
    "chunk_order", "file_name", "folder_name",
    "thread", "topic_segment", "user_manual", "version up",
})
DEFAULT_SOURCE = "chunk_order"


class ManifestError(ValueError):
    """Manifest validation or parse failure. ``errors`` lists all reasons
    (all-or-nothing policy)."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"manifest validation failed ({len(errors)} error(s)): "
            + "; ".join(errors[:10])
            + (" …" if len(errors) > 10 else "")
        )


@dataclass
class ManifestItem:
    """Serialized single ``NEROutput`` — content/summary/entities
    (plus optional tag/timestamp)."""

    content: str
    summary: str
    entities: list[dict[str, Any]] = field(default_factory=list)
    tag: str | None = None
    timestamp: str | None = None


@dataclass
class ParsedManifest:
    """A validated manifest. ``domain`` is intentionally absent — it is
    always taken from the environment (not from the manifest itself)."""

    working_folder: str | None
    tag: str | None
    source: str
    prev_event_id: str | None
    items: list[ManifestItem]
    # Domain declared in the manifest (ignored for loading; retained for
    # validation warning purposes only).
    declared_domain: str | None = None
    # Optional incremental identifier representing the source document's
    # stable identity.  Used as the stable key for file-level and segment-
    # level hash comparisons during incremental builds.  When absent,
    # incremental dedup is skipped and only deterministic event_id dedup applies.
    file_path: str | None = None


def _validate_entities(raw: Any, idx: int, errors: list[str]) -> list[dict[str, Any]]:
    """Validate item.entities — expected shape [{name, type}].
    A missing name is recorded as an error, not silently skipped."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(
            f"items[{idx}].entities must be a list (got {type(raw).__name__})"
        )
        return []
    out: list[dict[str, Any]] = []
    for j, ent in enumerate(raw):
        if not isinstance(ent, dict):
            errors.append(f"items[{idx}].entities[{j}] must be an object")
            continue
        name = str(ent.get("name") or "").strip()
        if not name:
            errors.append(f"items[{idx}].entities[{j}].name missing or empty")
            continue
        out.append({"name": name, "type": str(ent.get("type") or "").strip()})
    return out


def parse_manifest(raw: dict[str, Any]) -> ParsedManifest:
    """Parse a manifest dict into :class:`ParsedManifest` with all-or-nothing
    validation.

    Raises :class:`ManifestError` if any violation is found.  The
    ``complete`` gate is enforced here — a manifest with ``complete:false``
    (still being appended to) is refused.
    """
    errors: list[str] = []

    if not isinstance(raw, dict):
        raise ManifestError(["manifest top level must be a JSON object"])

    # Schema version check
    schema = raw.get("schema")
    if schema != MANIFEST_SCHEMA:
        errors.append(f"schema mismatch: expected {MANIFEST_SCHEMA!r}, got {schema!r}")

    # complete gate — refuse to execute a manifest still being appended to
    if raw.get("complete") is not True:
        errors.append(
            "complete:true not set — refusing to execute a manifest that is "
            "still being appended to. Set complete=true when writing is done."
        )

    # source enum validation
    source = raw.get("source") or DEFAULT_SOURCE
    if source not in SEQUENTIAL_SOURCES:
        errors.append(
            f"source {source!r} is outside the allowed set "
            f"{sorted(SEQUENTIAL_SOURCES)}"
        )

    working_folder = raw.get("working_folder")
    if working_folder is not None and not str(working_folder).strip():
        errors.append("working_folder is an empty string — omit it or provide a value")

    tag = raw.get("tag")
    prev_event_id = raw.get("prev_event_id")
    declared_domain = raw.get("domain")
    file_path = raw.get("file_path")

    # items
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        errors.append("items must be a non-empty list")
        raise ManifestError(errors)

    if len(raw_items) > MAX_ITEMS:
        errors.append(
            f"items count {len(raw_items)} exceeds limit {MAX_ITEMS} — "
            "split the manifest (chain via prev_event_id)"
        )

    items: list[ManifestItem] = []
    total_bytes = 0
    for idx, it in enumerate(raw_items):
        if not isinstance(it, dict):
            errors.append(f"items[{idx}] must be an object")
            continue
        content = it.get("content")
        summary = it.get("summary")
        if not content or not str(content).strip():
            errors.append(f"items[{idx}].content missing or empty (required)")
            content = ""
        if not summary or not str(summary).strip():
            errors.append(f"items[{idx}].summary missing or empty (required)")
            summary = ""
        content = str(content)
        summary = str(summary)
        if len(content) > MAX_CONTENT_CHARS:
            errors.append(
                f"items[{idx}].content length {len(content)} > {MAX_CONTENT_CHARS}"
            )
        if len(summary) > MAX_SUMMARY_CHARS:
            errors.append(
                f"items[{idx}].summary length {len(summary)} > {MAX_SUMMARY_CHARS}"
            )
        total_bytes += len(content.encode("utf-8"))
        entities = _validate_entities(it.get("entities"), idx, errors)
        item_tag = it.get("tag")
        if item_tag is not None and not str(item_tag).strip():
            item_tag = None
        items.append(ManifestItem(
            content=content,
            summary=summary,
            entities=entities,
            tag=item_tag,
            timestamp=it.get("timestamp"),
        ))

    if total_bytes > MAX_TOTAL_CONTENT_BYTES:
        errors.append(
            f"total content size {total_bytes} bytes exceeds limit "
            f"{MAX_TOTAL_CONTENT_BYTES} (~5 MB) — split the manifest"
        )

    if errors:
        raise ManifestError(errors)

    return ParsedManifest(
        working_folder=str(working_folder).strip() if working_folder else None,
        tag=str(tag).strip() if tag and str(tag).strip() else None,
        source=source,
        prev_event_id=str(prev_event_id).strip() if prev_event_id else None,
        items=items,
        declared_domain=str(declared_domain) if declared_domain else None,
        file_path=(str(file_path).replace("\\", "/").strip("/")
                   if file_path and str(file_path).strip() else None),
    )


def load_manifest(path: str | Path) -> ParsedManifest:
    """Load and validate a manifest from a file path.
    JSON parse failures are also raised as ManifestError."""
    p = Path(path)
    if not p.is_file():
        raise ManifestError([f"manifest file not found: {p}"])
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError([f"manifest JSON parse failed ({p}): {exc}"]) from exc
    return parse_manifest(raw)


__all__ = [
    "MANIFEST_SCHEMA",
    "MAX_CONTENT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_ITEMS",
    "MAX_TOTAL_CONTENT_BYTES",
    "SEQUENTIAL_SOURCES",
    "DEFAULT_SOURCE",
    "ManifestError",
    "ManifestItem",
    "ParsedManifest",
    "parse_manifest",
    "load_manifest",
]
