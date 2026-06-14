"""
MWeft (MemoryWeft) MCP Server.

FastMCP wrapper exposing read-only tools over stdio for MCP-capable clients
(Claude Desktop, Claude Code, MCP Inspector):

Read tools (event unit for search/lookup, auto-tag unit for detail):
- ``mweft_search``           — hybrid / events / entities vector search
- ``mweft_entity_lookup``    — substring entity → connected events
- ``mweft_auto_tag_detail``  — single auto-tag (community) hierarchy + events

Recording control:
- ``mweft_set_recording`` / ``mweft_get_recording`` — toggle Stop-hook cache write
  (legacy; prefers AI-proposed markdown curation via CLAUDE.md guidance).

Write path: no MCP write tool. AI proposes, user consents, then saves to
``data/memory/drafts/*.md`` and ingests via ``scripts/ingest_text_batch.py``.
The legacy Claude Code Stop hook (``k2g-cache-turn``) is deprecated and no
longer installed.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

# Eager-load heavy native deps on the main thread — FastMCP runs tool calls in
# anyio worker threads, and the first BLAS/MKL initialisation of numpy/torch
# can hang due to thread-affinity issues in worker threads
# (2026-05-12 K2G_TEST incident).
# Importing on the main thread makes subsequent worker-thread imports instant
# (module-cache hit).
#
# Set K2G_MCP_PREWARM_NATIVE=false to skip all eager imports
# (faster startup, but worker hangs are possible). Default: prewarm = true.
#
# Broad try/except + diagnostic logger on every import — tracks startup
# crashes in the K2G_TEST environment (2026-05-12 incident).
import os as _os_for_prewarm

_prewarm_log = logging.getLogger("k2g.mcp.server.prewarm")
_PREWARM_NATIVE = _os_for_prewarm.environ.get(
    "K2G_MCP_PREWARM_NATIVE", "true",
).strip().lower() in ("1", "true", "yes")

if _PREWARM_NATIVE:
    _prewarm_log.info("server.py top: eager prewarm start")
    try:
        import numpy as _numpy_eager  # noqa: F401
        _prewarm_log.info("server.py top: numpy ok")
    except Exception as _exc:  # noqa: BLE001
        _prewarm_log.warning("server.py top: numpy failed — %r", _exc)

    try:
        from k2g.embedding import client as _embedding_client_eager  # noqa: F401
        _prewarm_log.info("server.py top: k2g.embedding.client ok")
    except Exception as _exc:  # noqa: BLE001
        _prewarm_log.warning("server.py top: k2g.embedding.client failed — %r", _exc)

    try:
        from sentence_transformers import SentenceTransformer as _st_eager  # noqa: F401
        _prewarm_log.info("server.py top: sentence_transformers ok")
    except Exception as _exc:  # noqa: BLE001
        _prewarm_log.warning("server.py top: sentence_transformers failed — %r", _exc)

    try:
        import torch as _torch_eager  # noqa: F401
        _prewarm_log.info("server.py top: torch ok")
    except Exception as _exc:  # noqa: BLE001
        _prewarm_log.warning("server.py top: torch failed — %r", _exc)

    _prewarm_log.info("server.py top: eager prewarm done")
else:
    _prewarm_log.info("server.py top: eager prewarm skipped (K2G_MCP_PREWARM_NATIVE=false)")

from k2g.mcp.neighbors_tools import neighbors_tool
from k2g.mcp.community_tools import (
    community_detail_tool,
    community_explore_tool,
    community_list_tool,
    community_of_tool,
    community_residual_tool,
    community_summarize_tool,
)
from k2g.mcp.relations_tools import relations_tool
from k2g.mcp.sql_tools import (
    describe_schema_tool,
    explain_query_tool,
    sql_query_tool,
)
from k2g.mcp.temporal_tools import temporal_flow_tool
from k2g.mcp.memory_tools import remember_tool, remember_edit_tool
from k2g.db_store.embedding_guard import EmbeddingFingerprintMismatch
from k2g.mcp.factory import Deps, build_dependencies, build_plan_deps
from k2g.mcp.proxy.write_proxy import forwardable_write
from k2g.mcp.tools import (
    context_detail_tool,
    entity_lookup_tool,
    get_event_content_tool,
    search_tool,
    set_influence_tool,
    suggest_influence_review_tool,
)

logger = logging.getLogger(__name__)

mcp_app = FastMCP("mweft")

_deps: Deps | None = None
_plan_deps: PlanDeps | None = None


def _get_deps() -> Deps:
    """Lazily build production dependencies on first tool invocation."""
    global _deps
    if _deps is None:
        logger.info("Building MWeft MCP dependencies (graph + vector + embedding)")
        try:
            _deps = build_dependencies()
        except EmbeddingFingerprintMismatch as exc:
            # Deliver the exact, actionable reason to the MCP client instead of an
            # opaque startup failure — ToolError messages are surfaced verbatim.
            logger.error("MCP dependency build blocked: %s", exc)
            raise ToolError(str(exc)) from exc
    return _deps


def _get_plan_deps() -> PlanDeps:
    """Lazily build PlanDeps on first plan-tool invocation."""
    global _plan_deps
    if _plan_deps is None:
        logger.info("Building MWeft MCP PlanDeps (graph + embedding + LLM)")
        _plan_deps = build_plan_deps(_get_deps())
    return _plan_deps


def set_deps(deps: Deps) -> None:
    """Inject pre-built dependencies (used by tests and the CLI entry point)."""
    global _deps
    _deps = deps


def set_plan_deps(deps: PlanDeps) -> None:
    """Inject pre-built PlanDeps (used by tests)."""
    global _plan_deps
    _plan_deps = deps


# MCP telemetry decorator (per-call duration / error / count JSONL).
# Call results and exceptions pass through unchanged; telemetry failures
# are logged at WARNING level only.
import functools
import threading

from k2g.observability.mcp_telemetry import telemetry_wrap as _raw_telemetry_wrap

# Ensures thread-safe access to a single Postgres connection.
# FastMCP runs sync tool functions in a thread pool, so parallel tool
# dispatches from a client can access deps.graph._conn cursors simultaneously,
# causing races or hangs. psycopg2 connections are not thread-safe, so a
# single lock serializes all DB tool calls.
#
# RLock prevents deadlocks when one tool internally re-enters another
# (Python-side re-entry, not subprocess).
_TOOL_LOCK = threading.RLock()


def _telemetry_wrap(name: str):
    """Combine telemetry_wrap with a DB connection lock.

    Applied to all tool functions — a single ``@_telemetry_wrap("mweft_xxx")``
    decorator guarantees both duration JSONL logging and serialized DB access.
    """
    raw = _raw_telemetry_wrap(name)

    def decorator(fn):
        wrapped = raw(fn)

        @functools.wraps(wrapped)
        def with_lock(*args, **kwargs):
            with _TOOL_LOCK:
                return wrapped(*args, **kwargs)

        return with_lock

    return decorator


@mcp_app.tool()
@_telemetry_wrap("mweft_search")
def mweft_search(
    query: str,
    top_k: int = 10,
    mode: str = "hybrid",
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """MWeft vector search over events and/or entities.

    The search scope (domain and group) is determined by the server — callers
    cannot narrow it. The actual scope used is reported in ``searched_scope``.

    Args:
        query: Natural-language query string.
        top_k: Max hits (1–50, clamped).
        mode: ``"hybrid"`` (events + entities), ``"events"``, or ``"entities"``.
        conversation_id: Client-provided session id (optional). Used to trace
            the search → mweft_get_event_content flow for summary fidelity
            metrics. When None, only the owner_id is recorded in telemetry.

    Returns:
        ``{query, mode, hits, total, hint, searched_scope}``. Each event hit
        may include ``connections.sequential {prev,next}`` (adjacent document
        chunks). The ``hint`` field is a connection map over all results:
        ``threads`` (sequential chains) / ``entities`` (entities spanning
        two or more hits) / ``context_groups`` / ``categories`` /
        ``available_channels``. Use the hint to guide follow-up calls —
        no additional graph tool opt-in required.
    """
    return search_tool(
        _get_deps(),
        query=query,
        top_k=top_k,
        mode=mode,
    )


@mcp_app.tool()
@_telemetry_wrap("mweft_entity_lookup")
def mweft_entity_lookup(
    entity_name: str,
    limit: int = 20,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Look up entities by name substring and return their connected events.

    The search scope (domain) is determined by the server. See ``searched_scope``
    in the response for the actual scope used.

    Args:
        entity_name: Substring to match against entity names (case-insensitive).
        limit: Max events per entity (1–50, clamped).
        conversation_id: Client-provided session id (optional).
    """
    return entity_lookup_tool(
        _get_deps(),
        entity_name=entity_name,
        limit=limit,
    )


@mcp_app.tool()
@_telemetry_wrap("mweft_auto_tag_detail")
def mweft_auto_tag_detail(
    auto_tag_id: str,
    depth: int = 2,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Return an auto-tag's narrative summary, hierarchy, and member events.

    Auto-tags are produced by the system's community detection (Leiden /
    HDBSCAN). The internal identifier is a ContextGroup id (``cg_...``).

    Args:
        auto_tag_id: auto-tag id (e.g. ``"cg_..."``).
        depth: Hierarchy traversal depth (0–3, clamped).
        conversation_id: Client-provided session id (optional).
    """
    return context_detail_tool(
        _get_deps(),
        cg_id=auto_tag_id,
        depth=depth,
    )


@mcp_app.tool()
@_telemetry_wrap("mweft_get_event_content")
def mweft_get_event_content(
    event_id: str,
    include_raw: bool = True,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Fetch raw content of an event when its summary is insufficient.

    Use this when the summary returned by ``mweft_search`` /
    ``mweft_entity_lookup`` / ``mweft_auto_tag_detail`` lacks enough detail to
    answer the user. Telemetry records this call as a *raw fetch* — combined
    with search call counts it yields the **summary fidelity ratio**
    (raw_fetches / search_hits), which drives file-event vs. segment-event
    aggregation policy.

    Args:
        event_id: ``events.id`` (e.g. ``"ev_xxx"``) or ``vector_id``
            (e.g. ``"vec_xxx"``).
        include_raw: True fetches the full body from ``storage_uri``; False
            returns metadata only (useful for large files or test queries).
        conversation_id: Client-provided session id (optional). Used to trace
            the search → get_event_content flow within the same conversation.

    Returns:
        ``{"event_id": ..., "found": bool, "summary": ..., "content": "...",
        "char_count": N, "storage_uri": ..., "section_heading": ...,
        "domain": ..., "inline_meta": {...}}``.
    """
    return get_event_content_tool(
        _get_deps(),
        event_id=event_id,
        include_raw=include_raw,
    )










# ---------------------------------------------------------------------------
# Covenant Metadata tools (multi-source registration + audit).
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Share Group / Member tools (real-time permission add/remove).
# ---------------------------------------------------------------------------
















# ---------------------------------------------------------------------------
# Free SQL tool for LLMs (Row-Level Security blocks unauthorized rows).
# ---------------------------------------------------------------------------


@mcp_app.tool()
@_telemetry_wrap("mweft_sql_query")
def mweft_sql_query(
    sql: str,
    max_rows: int = 10000,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Run a read-only SQL query against MWeft.

    Learn the schema first with ``mweft_describe_schema``. Access control is
    enforced automatically by RLS (Postgres) or query_filter (SQLite) —
    unauthorized rows never appear in results.

    Safety layers applied:
    - Only SELECT / WITH allowed (DML and DDL are blocked)
    - Dangerous functions (pg_read_file, etc.) are blocked
    - LIMIT is added automatically (max_rows)
    - statement_timeout is set (Postgres, timeout_ms)

    Args:
        sql: SELECT or WITH SQL statement.
        max_rows: Automatic LIMIT cap (preserved if already set). Default 10000.
        timeout_ms: Postgres statement_timeout in milliseconds. Default 5000.
    """
    return sql_query_tool(
        _get_deps(), sql=sql, max_rows=int(max_rows),
        timeout_ms=int(timeout_ms),
    )


@mcp_app.tool()
def mweft_describe_schema() -> dict[str, Any]:
    """Return all MWeft tables, columns, and types.

    Use this to learn the schema before writing a free SQL query. RLS policies
    protect the data; the schema itself is public information. Very cheap to
    call.
    """
    return describe_schema_tool(_get_deps())


@mcp_app.tool()
def mweft_explain_query(sql: str) -> dict[str, Any]:
    """Return EXPLAIN output to help debug query efficiency.

    Postgres: EXPLAIN. SQLite: EXPLAIN QUERY PLAN.
    """
    return explain_query_tool(_get_deps(), sql=sql)


# ---------------------------------------------------------------------------
# Community tools (read-only — Leiden community interpretation).
# ---------------------------------------------------------------------------


@mcp_app.tool()
def mweft_auto_tag_list(
    kind: str = "entity",
    domain: str | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    """List auto-tags from the latest Leiden community detection run.

    An auto-tag is the narrative label for a system-detected community. ``kind``
    must be ``"entity"`` or ``"event"``. Returns each auto-tag's size and top
    member names so the LLM can interpret and label them. If the result is
    empty, run ``scripts/train_community.py`` first.
    """
    return community_list_tool(_get_deps(), kind=kind, domain=domain, top_n=top_n)


@mcp_app.tool()
def mweft_auto_tag_members(
    auto_tag_id: int,
    kind: str = "entity",
    run_id: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Return the full ranked member list for a single auto-tag.

    Pass ``domain`` (the active project's domain, same as ``mweft_auto_tag_list``)
    so the members resolve the SAME per-domain Leiden run. Omitting it falls back
    to the cross-domain (NULL) run, where an auto_tag_id can map to a different
    domain's community (e.g. K2G members under a 'sample' auto-tag in a shared DB).
    """
    return community_detail_tool(
        _get_deps(), community_id=auto_tag_id, kind=kind, run_id=run_id,
        domain=domain,
    )


@mcp_app.tool()
def mweft_auto_tag_of(node_id: str, kind: str = "entity") -> dict[str, Any]:
    """Return the auto-tag that an entity or event belongs to, plus sample peers."""
    return community_of_tool(_get_deps(), node_id=node_id, kind=kind)


@mcp_app.tool()
def mweft_auto_tag_summarize(
    kind: str = "entity",
    domain: str | None = None,
    top_n: int = 10,
    members_preview: int = 5,
) -> dict[str, Any]:
    """Return a full summary of the Leiden community (auto-tag) structure.

    Returns in one call: the Leiden settings stored in ``analysis_param``
    (resolution/seed), the community distribution from the latest completed run
    (count / size statistics / top members of leading communities). Use this
    when an LLM needs to narrate or label the overall community structure.
    ``kind`` must be ``"entity"`` or ``"event"``. If the result is empty, run
    ``scripts/train_community.py`` first.

    Returns: ``configured_params`` (stored settings), ``run_params`` /
    ``metrics`` (actual run), ``size_stats``, ``communities`` (top_n by size).
    """
    return community_summarize_tool(
        _get_deps(),
        kind=kind,
        domain=domain,
        top_n=top_n,
        members_preview=members_preview,
    )


@mcp_app.tool()
def mweft_community_explore(
    scope: str,
    kind: str = "event",
    resolution: float = 1.5,
    theta_e: float | None = None,
    domain: str | None = None,
    top_n: int = 10,
    members_preview: int = 5,
) -> dict[str, Any]:
    """Parameterized exploration view: run Leiden on-demand within a scope.

    Executes the ``suggested_view`` returned in ``explore_hints`` from
    ``mweft_auto_tag_summarize``. Within the given scope boundary, runs Leiden
    at the specified resolution to reveal finer-grained communities (not
    persisted; cached in-process). Call only when the user has opted in
    (e.g. agreed to "Analyze further?").

    scope: ``tag:<group_id>`` (sub-structure within a tag boundary) |
           ``community:<cid>`` (re-partition a large community at higher
           resolution) | bare group_id | tag name.
    resolution: Higher values produce smaller communities (default 1.5 for
           drill-in).
    theta_e (entity similarity threshold, 0–1): Two events are connected only
           when their shared-entity Jaccard similarity meets or exceeds this
           value. Lower values include weaker similarities, increasing community
           count and reducing modularity. Recommended: 0.4 for code/docs, 0.05
           for sparse conversational memory. When None, the stored
           ``analysis_param`` value is used — same baseline as the full summary.
           Override explicitly for tag-scoped exploration.
    Returns: ``num_communities`` / ``size_stats`` / ``communities`` (top_n,
    top_members) / ``metrics.modularity`` / ``theta_e`` / ``explore_hints``
    (structural signals).
    """
    deps = _get_deps()
    if theta_e is None:
        from k2g.trainer.community_freshness import read_leiden_params
        conn = getattr(deps.db.graph, "_conn", None)
        theta_e = (
            float(read_leiden_params(conn, domain).get("theta_e", 0.4))
            if conn is not None else 0.4
        )
    return community_explore_tool(
        deps,
        scope=scope,
        kind=kind,
        resolution=resolution,
        theta_e=theta_e,
        domain=domain,
        top_n=top_n,
        members_preview=members_preview,
    )


@mcp_app.tool()
def mweft_community_residual(
    kind: str = "event",
    domain: str | None = None,
    top_n: int = 10,
    members_preview: int = 5,
) -> dict[str, Any]:
    """Residual discovery: detected communities minus explicit labels.

    Compares standard-run communities against human-declared forced/user tags
    to surface hidden knowledge that labels do not explain. Quantified via
    NMI / H(community|tag) / residual (1−NMI). Four modes:
    ``emergent`` (latent concepts without labels — new tag candidates) /
    ``cross_label_bridge`` (undeclared cross-dependencies) /
    ``label_anomaly`` (misclassifications or hidden connections) /
    ``scoped`` (equivalent to explore).

    Prerequisite: at least 2 distinct forced/user tags are required. Without
    them the response returns ``applicable=false``. Set project or topic tags
    via the Manager UI or the CLI ``--tag`` option to activate this tool.
    """
    return community_residual_tool(
        _get_deps(),
        kind=kind,
        domain=domain,
        top_n=top_n,
        members_preview=members_preview,
    )


# ---------------------------------------------------------------------------
# Plan / Direction / ETG tools (write-capable, confirm-gated).
# ---------------------------------------------------------------------------






















# ---------------------------------------------------------------------------
# LLM content exploration tools: audit_trail / neighbors / temporal / relations
# ---------------------------------------------------------------------------






@mcp_app.tool()
@_telemetry_wrap("mweft_neighbors")
def mweft_neighbors(
    node_id: str,
    node_kind: str = "entity",
    rel: str = "all",
    hop: int = 1,
) -> dict[str, Any]:
    """Traverse 1–N hops of neighbors from a graph node.

    Args:
        node_id: Starting node id.
        node_kind: ``entity | event | cg | etg | plan | direction``.
        rel: ``all | participated_in | entity_connection | event_member_of |
            event_belongs_to_context | event_sequential_next | plan_from |
            plan_next | realized_as``.
        hop: 1–3 (hard cap at 3).

    Returns:
        ``{node, rel, hop, neighbors: [...], total_visited, truncated}``.
    """
    return neighbors_tool(
        _get_deps(),
        node_id=node_id, node_kind=node_kind, rel=rel, hop=int(hop),
    )


@mcp_app.tool()
@_telemetry_wrap("mweft_temporal_flow")
def mweft_temporal_flow(
    entity_id: str | None = None,
    cg_id: str | None = None,
    days: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return events in chronological order for an entity, CG, or domain.

    When neither entity_id nor cg_id is provided, returns domain-wide flow.
    The domain is determined by the server from the search scope (env / RLS);
    see ``searched_scope`` in the response.

    Args:
        entity_id: Return events for this entity (via PARTICIPATED_IN).
        cg_id: Return events for this ContextGroup (via
            event_belongs_to_context).
        days: Restrict to the last N days.
        limit: 1–200 (default 20).

    Returns:
        ``{mode, anchor, events: [...], total, truncated, searched_scope}``.
    """
    return temporal_flow_tool(
        _get_deps(),
        entity_id=entity_id, cg_id=cg_id,
        days=days, limit=int(limit),
    )


@mcp_app.tool()
@_telemetry_wrap("mweft_relations")
def mweft_relations(
    node_a_id: str,
    node_a_kind: str,
    node_b_id: str,
    node_b_kind: str,
    max_hop: int = 3,
) -> dict[str, Any]:
    """Find paths between two nodes using bidirectional BFS (hop <= 3).

    Args:
        node_a_id, node_a_kind: Starting node.
        node_b_id, node_b_kind: Destination node.
        max_hop: 1–3.

    Returns:
        ``{from, to, paths: [[{id, kind, rel_to_next}, ...]], hops_searched,
        total_visited, truncated, partial}``.
    """
    return relations_tool(
        _get_deps(),
        node_a_id=node_a_id, node_a_kind=node_a_kind,
        node_b_id=node_b_id, node_b_kind=node_b_kind,
        max_hop=int(max_hop),
    )


# ---------------------------------------------------------------------------
# Memory MCP (mweft_remember / mweft_remember_edit)
# ---------------------------------------------------------------------------


@mcp_app.tool()
@_telemetry_wrap("mweft_remember")
@forwardable_write("remember", deps_fetcher=_get_deps)
def mweft_remember(
    *,
    summary: str,
    entities: list[dict[str, Any]],
    working_folder: str | None = None,
    tag: str | None = None,
    timestamp: str | None = None,
    conversation_id: str | None = None,
    content: str,
) -> dict[str, Any]:
    """Save conversation context to MWeft (MemoryWeft).

    IMPORTANT calling convention: ``content`` can be very long, and in
    tool-call serialization the argument immediately following it can be
    silently dropped (observed: summary dropped when placed after content,
    causing validation failures). For this reason ``content`` is the last
    keyword-only argument in the signature — when arguments are listed in
    schema/signature order, content comes last and nothing follows it.
    Always list ``summary``, ``entities``, and ``tag`` before ``content``,
    with ``content`` at the very end.

    Args:
        summary: LLM-extracted summary (required, <= 500 chars).
        entities: Named entity recognition results [{name, type}, ...]
            (required).
        working_folder: Tag root mapping (internal groups table). Falls back
            to ``K2G_USER_MEMORY_SAVE_GROUP`` env when not provided.
        tag: Sub-tag path under the working_folder root for this conversation.
            Slash-separated (e.g. ``"A"`` or ``"A/B"``); each segment creates
            a sub-tag automatically. Match against the ``tag_tree`` in the
            response and reuse an existing path when possible; a new tag is
            created only when nothing fits.
        timestamp: ISO 8601 timestamp; defaults to NOW.
        conversation_id: Conversation identifier (optional).
        content: Original text (required, <= 50000 chars). Must be listed
            last (see calling convention above).

    The save domain is enforced server-side and cannot be controlled by the
    caller (prevents LLM hallucinations from scattering data across wrong
    domains). Priority: ``K2G_USER_MEMORY_SAVE_DOMAIN`` env → fallback
    ``ai_memory``.

    Returns:
        ``{event_id, tag_id, tag, tag_tree, saved_entities, edit_command, ...}``.
        ``tag`` is the resolved sub-tag path (absent when only the root applies);
        ``tag_tree`` lists all sub-tag paths under the root. Check
        ``applied_defaults.domain`` to confirm the actual save domain. Show
        ``saved_entities`` to the user; use ``mweft_remember_edit`` to correct
        any hallucinated entities.
    """
    return remember_tool(
        _get_deps(),
        content=content,
        summary=summary,
        entities=entities,
        working_folder=working_folder,
        tag=tag,
        timestamp=timestamp,
        conversation_id=conversation_id,
    )


@mcp_app.tool()
@_telemetry_wrap("mweft_remember_edit")
@forwardable_write("remember_edit", deps_fetcher=_get_deps)
def mweft_remember_edit(
    event_id: str,
    remove_entities: list[str] | None = None,
    add_entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Correct entity relationships on a saved event.

    The event itself (conversation content) is preserved; only entity
    associations can be hallucinated. This tool only severs or adds
    ``participated_in`` edges (3 SQL statements, milliseconds). Orphaned
    entities are marked as deprecated.

    Args:
        event_id: The event to correct.
        remove_entities: Entity names or entity_ids to unlink.
        add_entities: Missing entities to add [{name, type}, ...] (optional).

    Returns:
        ``{event_id, removed_count, added_count, now_linked_entities,
        orphaned_entities}``.
    """
    return remember_edit_tool(
        _get_deps(),
        event_id=event_id,
        remove_entities=remove_entities,
        add_entities=add_entities,
    )


# ---------------------------------------------------------------------------
# System Contract Resources
# ---------------------------------------------------------------------------
# When a client fetches the URI `mweft://system/contracts/<topic>.md`, this
# resource returns the full contract body (frontmatter + long-form text).
# This is the target referenced by ``context.active_contracts[].see`` in
# sql_query responses.

from k2g.mcp import contracts as _contracts_module


@mcp_app.resource("mweft://system/contracts/index")
def get_contracts_index() -> str:
    """Return the table of contents listing all registered contract topics."""
    topics = _contracts_module.list_topics()
    lines = ["# MWeft System Contracts — Index\n"]
    for t in topics:
        lines.append(f"- [{t}](mweft://system/contracts/{t}.md)")
    return "\n".join(lines)


@mcp_app.resource("mweft://system/contracts/{topic}.md")
def get_contract(topic: str) -> str:
    """Return the frontmatter and long-form body for an individual contract."""
    # Accept both "topic" and "topic.md" for compatibility.
    clean = topic[:-3] if topic.endswith(".md") else topic
    try:
        return _contracts_module.read_full(clean)
    except KeyError:
        return f"# Unknown contract: {clean}\n\nAvailable: {', '.join(_contracts_module.list_topics())}"
