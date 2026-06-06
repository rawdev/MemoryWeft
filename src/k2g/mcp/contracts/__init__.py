"""K2G MCP Contract Layer -- loader + matcher.

Meta-knowledge layer that force-attaches *system behavior contracts*
alongside data responses. When a diagnostic LLM fires free-form SQL
queries without learning the schema/narrative, it may misclassify
expected states (e.g., staging table having 0 rows after endgame is
normal; W.6 pilot plaintext signature is not a spec violation) via
*negative inference*. Contracts serve as a backstop.

Directory layout (rglob auto-scan):
- ``operator/`` -- MCP infrastructure-operations-only contracts
- ``system/``   -- data-lifecycle contracts
- ``data_model/`` -- domain-agnostic graph-abstraction contracts

One md file per contract. Frontmatter (YAML) + long-form body.
The ``summary`` in the frontmatter is a short prompt-like message
embedded in the LLM response payload. The body is exposed via
``mweft://system/contracts/<topic>.md`` resource fetch.

Frontmatter schema:

.. code-block:: yaml

    ---
    topic: <id_snake_case>
    status: stable | pilot | by_design | deprecated
    applies_to_tables: [table1, ...]
    applies_to_columns: [table.column, ...]   # optional
    applies_when_result:                       # optional
      - pattern: count_zero | all_null | drift_signature_plain
        columns: [...]
    summary: |
      Short natural-language summary, <=500 chars.
    ---

Principles:
- Contracts cover *structural conventions / classification enums /
  blocking rules / diagnostic interpretation rules* only. Concrete
  values (file paths, domain examples, operational measurement
  specs) belong in data/responses. The contract layer is invariant
  across domains.
- Payload bloat prevention: enrichment responses include only
  ``topic`` + ``status`` + ``summary`` + ``see`` URI. The md body
  is returned only on fetch.
- Cap: at most ``MAX_CONTRACTS_PER_RESPONSE`` per response.
- Read-only: contract data is loaded once at boot + memory-cached.
  No runtime mutations.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Max contracts per response (payload bloat prevention)
MAX_CONTRACTS_PER_RESPONSE = 3

# Max length for frontmatter summary (lint -- truncated on load)
SUMMARY_MAX_CHARS = 500

# Result pattern identifiers
PATTERN_COUNT_ZERO = "count_zero"
PATTERN_ALL_NULL = "all_null"
PATTERN_DRIFT_SIGNATURE_PLAIN = "drift_signature_plain"

# Resource URI prefix
RESOURCE_URI_PREFIX = "mweft://system/contracts"


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split an md file into (frontmatter dict, body str).

    Depends on PyYAML -- included in pyproject.toml base deps
    (``pyyaml>=6.0``).
    """
    import yaml

    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("missing or malformed frontmatter")
    fm_raw, body = m.group(1), m.group(2)
    fm = yaml.safe_load(fm_raw) or {}
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a YAML mapping")
    return fm, body


# ---------------------------------------------------------------------------
# Contract registry (loaded once at boot, memory-cached)
# ---------------------------------------------------------------------------

_CONTRACTS_DIR = Path(__file__).parent
_loaded: bool = False
_topics: dict[str, dict[str, Any]] = {}              # topic_id -> frontmatter
_bodies: dict[str, str] = {}                         # topic_id -> md body
_by_table: dict[str, list[str]] = {}                 # table -> [topic_id]
_by_column: dict[str, list[str]] = {}                # "table.column" -> [topic_id]
_by_pattern: dict[str, list[tuple[str, list[str]]]] = {}  # pattern -> [(topic, columns)]


def _load_all() -> None:
    """Load all *.md files in the contracts directory + build reverse
    indexes.

    No-op if already loaded. Ignored on subsequent calls after boot
    (use reset_for_testing in tests).
    """
    global _loaded
    if _loaded:
        return

    _topics.clear()
    _bodies.clear()
    _by_table.clear()
    _by_column.clear()
    _by_pattern.clear()

    # rglob -- auto-scan operator/ + system/ + data_model/ subdirectories.
    for md_path in sorted(_CONTRACTS_DIR.rglob("*.md")):
        try:
            text = md_path.read_text(encoding="utf-8")
            fm, body = _parse_frontmatter(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("contract parse failed: %s -- %s", md_path.name, exc)
            continue

        topic = fm.get("topic")
        if not topic or not isinstance(topic, str):
            logger.warning("contract missing topic: %s", md_path.name)
            continue

        # summary truncate
        summary = fm.get("summary") or ""
        if isinstance(summary, str) and len(summary) > SUMMARY_MAX_CHARS:
            summary = summary[:SUMMARY_MAX_CHARS].rstrip() + "…"
            fm["summary"] = summary

        _topics[topic] = fm
        _bodies[topic] = body

        # Reverse index -- table
        for table in fm.get("applies_to_tables") or []:
            _by_table.setdefault(str(table), []).append(topic)

        # Reverse index -- column
        for col in fm.get("applies_to_columns") or []:
            _by_column.setdefault(str(col), []).append(topic)

        # Reverse index -- pattern
        for spec in fm.get("applies_when_result") or []:
            if not isinstance(spec, dict):
                continue
            pattern = spec.get("pattern")
            cols = spec.get("columns") or []
            if not pattern:
                continue
            _by_pattern.setdefault(str(pattern), []).append((topic, [str(c) for c in cols]))

    _loaded = True
    logger.info("contracts loaded: %d topics", len(_topics))


def reset_for_testing() -> None:
    """For tests -- triggers reload on next call."""
    global _loaded
    _loaded = False


def list_topics() -> list[str]:
    """List of registered contract topic ids."""
    _load_all()
    return sorted(_topics.keys())


def read_full(topic: str) -> str:
    """For resource fetch -- return full md (frontmatter + body)."""
    _load_all()
    if topic not in _topics:
        raise KeyError(f"unknown contract topic: {topic}")
    # Re-serialize frontmatter + body
    import yaml
    fm_yaml = yaml.safe_dump(
        _topics[topic], sort_keys=False, allow_unicode=True,
    ).strip()
    return f"---\n{fm_yaml}\n---\n\n{_bodies[topic]}"


# ---------------------------------------------------------------------------
# SQL touched-tables extraction
# ---------------------------------------------------------------------------

_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


def extract_touched_tables(sql: str) -> set[str]:
    """Extract table names from FROM / JOIN clauses in SQL text.

    Also extracts the last segment of schema-qualified names
    (e.g., ``public.events``) since contracts list
    ``applies_to_tables`` without schema.
    """
    matches = _TABLE_RE.findall(sql or "")
    return {m.lower() for m in matches if m}


# ---------------------------------------------------------------------------
# Result pattern checks
# ---------------------------------------------------------------------------

_SHA256_RE = re.compile(r"\A[0-9a-f]{32,64}\Z")


def _matches_count_zero(rows: list[dict[str, Any]], cols: list[str]) -> bool:
    """True if at least one of *cols* is 0 in any row.

    Empty cols checks *all numeric columns* (catch-all).
    """
    if not rows:
        return False
    for row in rows:
        target_keys = cols or [k for k, v in row.items() if isinstance(v, (int, float))]
        for k in target_keys:
            v = row.get(k)
            if isinstance(v, (int, float)) and v == 0:
                return True
    return False


def _matches_all_null(rows: list[dict[str, Any]], cols: list[str]) -> bool:
    """True if all cols in every row are None."""
    if not rows or not cols:
        return False
    for row in rows:
        for k in cols:
            if row.get(k) is not None:
                return False
    return True


def _matches_drift_signature_plain(rows: list[dict[str, Any]], cols: list[str]) -> bool:
    """True if signature column is plaintext (not sha256/hex format)."""
    if not rows or not cols:
        return False
    for row in rows:
        for k in cols:
            v = row.get(k)
            if isinstance(v, str) and v.strip() and not _SHA256_RE.match(v.strip().lower()):
                return True
    return False


_PATTERN_MATCHERS = {
    PATTERN_COUNT_ZERO: _matches_count_zero,
    PATTERN_ALL_NULL: _matches_all_null,
    PATTERN_DRIFT_SIGNATURE_PLAIN: _matches_drift_signature_plain,
}


# ---------------------------------------------------------------------------
# Public API -- match_active_contracts
# ---------------------------------------------------------------------------


def match_active_contracts(
    sql: str,
    columns: list[str] | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return short payloads of active contracts matching SQL + result
    columns/rows.

    Three matching dimensions (OR-combined):
    1. Touched tables in SQL (``applies_to_tables``)
    2. SELECT columns (``applies_to_columns``) -- matches both
       ``table.column`` and bare ``column``
    3. Result row patterns (``applies_when_result``)

    Return dict shape (no md body -- payload bloat prevention):

    .. code-block:: python

        {
            "topic": "manifest_endgame",
            "status": "stable",
            "summary": "...",
            "see": "mweft://system/contracts/manifest_endgame.md",
        }

    Cap = ``MAX_CONTRACTS_PER_RESPONSE``. Priority:
    pattern match > column match > table match (most specific
    first).
    """
    _load_all()
    rows = rows or []
    columns = columns or []

    # Priority queue -- (priority, topic). Lower priority = higher rank.
    matched: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(topic: str, priority: int) -> None:
        if topic not in _topics or topic in seen:
            return
        seen.add(topic)
        matched.append((priority, topic))

    # 1. Pattern (highest priority)
    for pattern_name, entries in _by_pattern.items():
        matcher = _PATTERN_MATCHERS.get(pattern_name)
        if matcher is None:
            continue
        for topic, target_cols in entries:
            if matcher(rows, target_cols):
                add(topic, priority=1)

    # 2. Column
    for col in columns:
        for key in (col, f"*.{col}"):
            for topic in _by_column.get(key, []):
                add(topic, priority=2)

    # 3. Table
    touched = extract_touched_tables(sql)
    for table in touched:
        for topic in _by_table.get(table, []):
            add(topic, priority=3)
        # Additional -- match columns registered as "table.column" in column index
        for col_key, topics in _by_column.items():
            if col_key.startswith(f"{table}.") or col_key == f"*.{table}":
                for topic in topics:
                    add(topic, priority=2)

    # Sort by priority + cap
    matched.sort()
    out: list[dict[str, Any]] = []
    for _prio, topic in matched[:MAX_CONTRACTS_PER_RESPONSE]:
        fm = _topics[topic]
        out.append({
            "topic": topic,
            "status": fm.get("status", "stable"),
            "summary": fm.get("summary", "").strip(),
            "see": f"{RESOURCE_URI_PREFIX}/{topic}.md",
        })
    return out


# ---------------------------------------------------------------------------
# match_for_tool: enrichment entry point for non-SQL tools
# ---------------------------------------------------------------------------

# Tool -> default node_kind mapping
_TOOL_NODE_KIND: dict[str, str] = {
    "mweft_entity_lookup": "entity",
    "mweft_auto_tag_detail": "cg",
    "mweft_get_event_content": "event",
    # BP-70 — Memory MCP
    "mweft_remember": "event",
    "mweft_remember_edit": "event",
}

# node_kind -> applicable data_model contract topics
_NODE_KIND_CONTRACTS: dict[str, list[str]] = {
    "entity": ["entity_extraction_provenance", "edge_semantics"],
    "event": ["event_summary_provenance", "edge_semantics", "source_lineage"],
    "cg": ["auto_tag_semantics"],
    "etg": ["etg_plan_semantics"],
    "group": ["tag_semantics"],
    "domain": ["domain_isolation"],
}

# rel -> applicable contract topics
_REL_CONTRACTS: dict[str, list[str]] = {
    "participated_in": ["edge_semantics"],
    "entity_connection": ["edge_semantics"],
    "event_sequential_next": ["edge_semantics"],
    "event_belongs_to_context": ["auto_tag_semantics", "edge_semantics"],
    "event_member_of": ["tag_semantics", "edge_semantics"],
    "group_hierarchy": ["tag_semantics"],
    "all": ["edge_semantics"],
}

# audit_kind -> applicable contract topics (per mweft_audit_trail kind)
_AUDIT_KIND_CONTRACTS: dict[str, list[str]] = {
    "source_purge": ["source_lineage"],
    "covenant": ["source_lineage"],
    "build_audit": [],
    "sql_audit": ["sql_safety"],
    "build_failure": [],
    "train_state": ["train_state_w6", "incremental_skip"],
    "events_audit": ["event_summary_provenance"],
}


def match_for_tool(
    tool_name: str,
    *,
    node_kind: str | None = None,
    rel: str | None = None,
    audit_kind: str | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Auto-match contracts per tool -- enrichment entry point for
    non-SQL tools.

    ``match_active_contracts(sql=...)`` is for sql_query only. This
    function handles the other 7 tools (search / entity_lookup /
    context_detail / get_event_content / audit_trail / neighbors /
    temporal_flow / relations).

    Matching dimensions:
    1. ``tool_name`` -> default node_kind from ``_TOOL_NODE_KIND``
    2. Explicit ``node_kind`` argument
    3. Explicit ``rel`` argument
    4. Explicit ``audit_kind`` argument
    5. Pattern matching on ``rows`` (count_zero, etc.)

    Cap = ``MAX_CONTRACTS_PER_RESPONSE``. Priority:
    pattern > kind/rel > tool.
    """
    _load_all()
    rows = rows or []

    matched: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(topic: str, priority: int) -> None:
        if topic not in _topics or topic in seen:
            return
        seen.add(topic)
        matched.append((priority, topic))

    # 1. Pattern (highest priority)
    for pattern_name, entries in _by_pattern.items():
        matcher = _PATTERN_MATCHERS.get(pattern_name)
        if matcher is None:
            continue
        for topic, target_cols in entries:
            if matcher(rows, target_cols):
                add(topic, priority=1)

    # 2. Explicit rel / audit_kind (priority=2) -- rel is a more
    #    specific match than node_kind, so it wins on cap contention.
    #    (e.g., rel='event_member_of' is narrower than
    #    node_kind='event'.)
    if rel:
        for topic in _REL_CONTRACTS.get(rel, []):
            add(topic, priority=2)
    if audit_kind:
        for topic in _AUDIT_KIND_CONTRACTS.get(audit_kind, []):
            add(topic, priority=2)

    # 3. Explicit node_kind (priority=3)
    if node_kind:
        for topic in _NODE_KIND_CONTRACTS.get(node_kind, []):
            add(topic, priority=3)

    # 4. Tool default node_kind (priority=4)
    default_kind = _TOOL_NODE_KIND.get(tool_name)
    if default_kind and default_kind != node_kind:
        for topic in _NODE_KIND_CONTRACTS.get(default_kind, []):
            add(topic, priority=4)

    # Sort by priority + cap
    matched.sort()
    out: list[dict[str, Any]] = []
    for _prio, topic in matched[:MAX_CONTRACTS_PER_RESPONSE]:
        fm = _topics[topic]
        out.append({
            "topic": topic,
            "status": fm.get("status", "stable"),
            "summary": fm.get("summary", "").strip(),
            "see": f"{RESOURCE_URI_PREFIX}/{topic}.md",
        })
    return out


# ---------------------------------------------------------------------------
# evidence_envelope: auto-extract source_events / source_segments from tool responses
# ---------------------------------------------------------------------------

MAX_EVIDENCE_EVENTS = 5   # Max per source_events / source_segments
_EVIDENCE_SKIP_KEYS = frozenset({"context", "evidence"})  # Cycle prevention


def extract_evidence(result: dict[str, Any]) -> dict[str, list[str]] | None:
    """Recursively walk a response dict for event_id / vector_id to
    build evidence.

    K2G event ids have an ``evt_`` prefix; vector_id is an arbitrary
    string keyed by ``vector_id``. ``context`` / ``evidence`` keys
    are skipped to prevent cycles.

    Returns:
        ``{"source_events": [...], "source_segments": [...]}`` or
        None if absent.
    """
    events: set[str] = set()
    segments: set[str] = set()

    def _walk(obj: Any, parent_key: str = "") -> None:
        if parent_key in _EVIDENCE_SKIP_KEYS:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in _EVIDENCE_SKIP_KEYS:
                    continue
                if isinstance(v, str):
                    if k in ("id", "event_id") and v.startswith("evt_"):
                        events.add(v)
                    elif k == "vector_id" and v:
                        segments.add(v)
                else:
                    _walk(v, parent_key=k)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(result)

    if not events and not segments:
        return None

    ev: dict[str, list[str]] = {}
    if events:
        ev["source_events"] = sorted(events)[:MAX_EVIDENCE_EVENTS]
    if segments:
        ev["source_segments"] = sorted(segments)[:MAX_EVIDENCE_EVENTS]
    return ev


__all__ = [
    "match_active_contracts",
    "match_for_tool",
    "extract_evidence",
    "list_topics",
    "read_full",
    "reset_for_testing",
    "extract_touched_tables",
    "MAX_CONTRACTS_PER_RESPONSE",
    "MAX_EVIDENCE_EVENTS",
    "RESOURCE_URI_PREFIX",
    "PATTERN_COUNT_ZERO",
    "PATTERN_ALL_NULL",
    "PATTERN_DRIFT_SIGNATURE_PLAIN",
]
