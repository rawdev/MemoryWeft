"""
K2G ChronoGraph MCP — read-only tool handlers.

Three handlers exposed to MCP clients:

- ``search_tool``          — hybrid / events / entities vector search
- ``entity_lookup_tool``   — substring entity lookup + connected events
- ``context_detail_tool``  — one ContextNode's narrative + hierarchy + events

All handlers are pure functions of a ``Deps`` bundle (graph, vector, embedding)
so they can be unit-tested with in-memory fakes. No LLM calls, no writes,
no content-store access — this is the episodic-memory (L3) read slice only.
"""

from __future__ import annotations

import logging
from typing import Any

from k2g.mcp.factory import Deps
from k2g.mcp.scope import resolve_search_scope, scope_to_str
from k2g.mcp.schemas import (
    ENTITY_CANDIDATE_MAX,
    ContextDetailResponse,
    EntityLookupResponse,
    EntityMatch,
    EventSummary,
    InfluenceReviewCandidate,
    InfluenceReviewMember,
    InfluenceReviewResponse,
    InfluenceUpdate,
    SearchHit,
    SearchResponse,
    TagMatch,
    clamp_depth,
    clamp_top_k,
)

logger = logging.getLogger(__name__)

# Relevance gate for the search deficit signal: a result beyond the shown slice
# only marks `more_available` when its score is within this fraction of the
# weakest shown result. Self-calibrating per query (relative to shown_min), so
# no absolute cosine-similarity magic number. Heuristic default — tune against
# measured score distributions; `tail_score` is surfaced for transparency.
_TAIL_RELEVANCE_RATIO = 0.8


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _manifest_evidence_for_event(
    deps: Deps, event_id: str,
) -> dict[str, Any]:
    """Look up (source_root, file_path, file_hash, ingested_at) from the
    manifest for the given event_id. Missing fields are filled with None."""
    out = {
        "source_root": None, "file_path": None,
        "file_hash": None, "ingested_at": None,
    }
    manifest = getattr(deps, "manifest", None)
    if manifest is None:
        return out
    mconn = getattr(manifest, "_conn", None)
    if mconn is None:
        return out
    try:
        cur = mconn.cursor()
        cur.execute(
            "SELECT mf.file_path, mf.file_hash, mf.built_at, mf.source_root "
            "  FROM build_segment_manifest s "
            "  JOIN build_file_manifest mf "
            "    ON mf.domain = s.domain AND mf.file_path = s.file_path "
            "   AND mf.status = 'active' "
            " WHERE s.event_id = ? "
            " ORDER BY s.id DESC LIMIT 1",
            (event_id,),
        )
        row = cur.fetchone()
        if row is not None:
            out["file_path"] = row["file_path"]
            out["file_hash"] = row["file_hash"]
            out["ingested_at"] = row["built_at"]
            out["source_root"] = row["source_root"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("manifest evidence join failed event_id=%s: %s", event_id, exc)
    return out


def _build_event_summary(deps: Deps, ev: dict[str, Any]) -> EventSummary:
    """events row dict (e.* SELECT) + manifest evidence → EventSummary."""
    eid = str(ev.get("id", ""))
    evid_meta = _manifest_evidence_for_event(deps, eid) if eid else {
        "source_root": None, "file_path": None,
        "file_hash": None, "ingested_at": None,
    }
    influence = ev.get("influence_score")
    # Postgres timestamptz columns return datetime objects on SELECT.
    # Serialize to ISO string to avoid pydantic validation failures.
    # Also compatible with SQLite and other backends that return strings.
    ts_raw = ev.get("timestamp")
    if hasattr(ts_raw, "isoformat"):
        ts_str = ts_raw.isoformat()
    else:
        ts_str = ts_raw
    return EventSummary(
        id=eid,
        vector_id=ev.get("vector_id"),
        domain=ev.get("domain"),
        timestamp=ts_str,
        order_index=ev.get("order_index"),
        summary=ev.get("summary"),
        influence_score=float(influence) if influence is not None else 1.0,
        source_root=evid_meta["source_root"],
        file_path=evid_meta["file_path"],
        file_hash=evid_meta["file_hash"],
        ingested_at=evid_meta["ingested_at"],
    )


# ---------------------------------------------------------------------------
# 1. k2g_search
# ---------------------------------------------------------------------------

def search_tool(
    deps: Deps,
    *,
    query: str,
    top_k: int = 10,
    mode: str = "hybrid",
) -> dict[str, Any]:
    """Vector similarity search over K2G.

    mode:
      - ``hybrid``   — events + entities merged (up to 2*top_k hits).
      - ``events``   — event hits only.
      - ``entities`` — entity hits only.

    The search scope (which domains and groups to query) is determined by the
    server via ``resolve_search_scope(deps)`` (env ``K2G_USER_SEARCH_TARGETS``
    or future RLS). Callers cannot narrow the scope. The ``searched_scope``
    field in the response reflects the actual scope used.
    """
    if not query or not query.strip():
        return SearchResponse(query=query, mode="hybrid", hits=[], total=0).model_dump()

    # Search scope is determined by the server, not the caller. Empty list means no filter.
    scope = resolve_search_scope(deps)
    resolved_targets = scope or None

    # 2. Resolve each target's group to a list of event_ids.
    filter_search_targets: list[tuple[str, list[str] | None]] | None = None
    if resolved_targets is not None:
        filter_search_targets = []
        for t in resolved_targets:
            if t.group:
                event_ids = deps.graph.list_event_ids_by_group(t.group)
                if not event_ids:
                    # This target has 0 rows — skip.
                    continue
                filter_search_targets.append((t.domain, event_ids))
            else:
                filter_search_targets.append((t.domain, None))
        if not filter_search_targets:
            response = SearchResponse(
                query=query,
                mode=mode if mode in ("hybrid", "events", "entities") else "hybrid",
                hits=[],
                total=0,
            ).model_dump()  # type: ignore[arg-type]
            response["searched_scope"] = scope_to_str(scope)
            return response

    normalized_mode = mode if mode in ("hybrid", "events", "entities") else "hybrid"
    k = clamp_top_k(top_k)
    query_vector = deps.embedding.embed(query)

    hits: list[SearchHit] = []

    if normalized_mode in ("hybrid", "events"):
        event_hits = deps.vector.search(
            query_vector=query_vector,
            filter_search_targets=filter_search_targets,
            limit=k + 1,  # +1 probe → detect more_available beyond the shown slice
        )
        for hit in event_hits:
            # PgVectorStore returns a flat shape ({id, domain, summary,
            # distance}). Older SQLite/Qdrant returned nested shape
            # ({vector_id, score, metadata:{...}}). Handle both.
            md = hit.get("metadata", {}) or {}
            distance = hit.get("distance")
            score = hit.get("score")
            if score is None and distance is not None:
                score = 1.0 - float(distance)
            event_id = (
                hit.get("id")
                or hit.get("vector_id")
                or md.get("vector_id")
                or md.get("event_id")
                or ""
            )
            hits.append(
                SearchHit(
                    kind="event",
                    id=str(event_id),
                    score=float(score or 0.0),
                    domain=hit.get("domain") or md.get("domain"),
                    name_or_summary=str(
                        hit.get("summary")
                        or md.get("summary")
                        or md.get("text")
                        or ""
                    ),
                    metadata={
                        "event_id": event_id,
                        "order_index": hit.get("order_index") or md.get("order_index"),
                        "timestamp": hit.get("timestamp") or md.get("timestamp"),
                    },
                )
            )

    # When a group is specified in a target, entity search is skipped because
    # entities have no direct group membership. Group-scoped entity search is
    # a future enhancement. Only event search supports group scoping.
    # For entity search the domain filter is the union of resolved_targets' domains.
    any_group_specified = bool(
        resolved_targets and any(t.group for t in resolved_targets)
    )
    entity_domains: list[str] | None = None
    if resolved_targets:
        entity_domains = [t.domain for t in resolved_targets if t.domain]
        entity_domains = list(dict.fromkeys(entity_domains)) or None  # dedup
    if normalized_mode in ("hybrid", "entities") and not any_group_specified:
        entity_hits = deps.vector.search_similar_entities(
            query_vector=query_vector,
            domain=entity_domains,
            limit=k + 1,  # +1 probe → detect more_available beyond the shown slice
        )
        for hit in entity_hits:
            # PgVectorStore.search_similar_entities returns a flat shape
            # ({id, name, domain, distance, score}).
            md = hit.get("metadata", {}) or {}
            distance = hit.get("distance")
            score = hit.get("score")
            if score is None and distance is not None:
                score = 1.0 - float(distance)
            entity_id = (
                hit.get("id")
                or hit.get("entity_id")
                or md.get("entity_id")
                or ""
            )
            hits.append(
                SearchHit(
                    kind="entity",
                    id=str(entity_id),
                    score=float(score or 0.0),
                    domain=hit.get("domain") or md.get("domain"),
                    name_or_summary=str(hit.get("name") or md.get("name") or ""),
                    metadata={
                        "entity_id": entity_id,
                        "type": hit.get("type") or md.get("type"),
                        "event_count": hit.get("event_count") or md.get("event_count"),
                    },
                )
            )

    hits.sort(key=lambda h: h.score, reverse=True)
    # Deficit signal (relevance-gated): the +1 probe per branch over-fetched, so
    # the ranked list may exceed the display cap. "More" counts only when the
    # first result *beyond* the shown slice (`tail_score`) is still comparable to
    # the weakest result we showed (`shown_min`) — within _TAIL_RELEVANCE_RATIO.
    # A broad query's loosely-similar tail drops off sharply after the slice, so
    # query breadth alone no longer inflates more_available. `tail_score` is
    # surfaced so the caller can judge the drop-off itself, not just trust the flag.
    ranked_count = len(hits)
    cap = k * 2 if normalized_mode == "hybrid" else k
    shown = min(cap, ranked_count)
    shown_min = hits[shown - 1].score if shown > 0 else 0.0
    tail_score = hits[cap].score if ranked_count > cap else None
    hits = hits[:cap]
    more_available = (
        tail_score is not None
        and tail_score >= shown_min * _TAIL_RELEVANCE_RATIO
    )
    continuation = (
        f"{shown} shown; more results of comparable relevance match — call "
        f"mweft_search_window(query, start={shown + 1}) to page beyond this."
        if more_available else None
    )

    # Build the connection map (hint system): attach connections to each hit
    # and include a hint block in the response. Errors are isolated so that
    # a hint failure does not break the search response itself.
    hint_block = None
    try:
        from k2g.mcp.hint import build_connection_map
        hint_block = build_connection_map(deps, hits)
    except Exception as exc:  # noqa: BLE001
        logger.warning("connection map build failed: %s", exc)

    response = SearchResponse(
        query=query,
        mode=normalized_mode,  # type: ignore[arg-type]
        hits=hits,
        total=len(hits),
        hint=hint_block,
        shown=shown,
        more_available=more_available,
        tail_score=round(tail_score, 4) if tail_score is not None else None,
        continuation=continuation,
    ).model_dump()
    from k2g.mcp.contracts import match_for_tool, extract_evidence
    hit_kinds = {h.kind for h in hits}
    primary_kind = "event" if "event" in hit_kinds else (
        "entity" if "entity" in hit_kinds else None
    )
    active = match_for_tool("mweft_search", node_kind=primary_kind)
    if active:
        # Slim down the search response: include only a pointer to each contract
        # instead of the full summary text. Full text is available via the `see` URI.
        # This prevents long contract bodies from burying the hint block.
        response["context"] = {
            "active_contracts": [
                {
                    "topic": c.get("topic"),
                    "status": c.get("status"),
                    "see": c.get("see"),
                }
                for c in active
            ],
        }
    ev = extract_evidence(response)
    if ev:
        response["evidence"] = ev
    response["searched_scope"] = scope_to_str(scope)
    return response


# ---------------------------------------------------------------------------
# 2. k2g_entity_lookup
# ---------------------------------------------------------------------------

# Maximum number of event hits fed to the hint scaffold for entity_lookup.
# build_connection_map makes one graph call per event hit, so this caps cost.
_HINT_MAX_EVENTS = 12


def entity_lookup_tool(
    deps: Deps,
    *,
    entity_name: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Return events connected to entities whose name contains ``entity_name``.

    Read-only: uses ``search_entities_by_name`` (not ``link_or_create_entity``)
    to avoid any MERGE side effects.

    The search scope (domain) is determined by the server via
    ``resolve_search_scope(deps)``. The group part of each target is ignored
    because entities have no direct group membership. The ``searched_scope``
    field in the response reflects the actual scope used.
    """
    if not entity_name or not entity_name.strip():
        return EntityLookupResponse(
            query_name=entity_name,
            matches=[],
            suggestion="empty query",
        ).model_dump()

    # Search scope is determined by the server. Entity search uses domain only
    # (group part is ignored because entities have no direct group membership).
    scope = resolve_search_scope(deps)
    domains_from_scope = list(dict.fromkeys(t.domain for t in scope if t.domain))
    domain: str | list[str] | None
    if not domains_from_scope:
        domain = None
    elif len(domains_from_scope) == 1:
        domain = domains_from_scope[0]
    else:
        domain = domains_from_scope

    page_size = clamp_top_k(limit)

    candidates = deps.graph.search_entities_by_name(
        name=entity_name,
        domain=domain,
        limit=ENTITY_CANDIDATE_MAX,
    )

    matches: list[EntityMatch] = []
    for cand in candidates:
        events, total = deps.graph.get_entity_events(
            entity_id=cand["id"],
            page=1,
            size=page_size,
        )
        event_summaries = [_build_event_summary(deps, ev) for ev in events]
        # Primary sort by influence_score DESC; timestamp/order_index as tiebreaker.
        event_summaries.sort(
            key=lambda s: (
                -s.influence_score,
                -(s.order_index or 0),
            )
        )
        matches.append(
            EntityMatch(
                entity={
                    "kind": "entity",  # self-describing kind field
                    "id": cand.get("id"),
                    "name": cand.get("name"),
                    "domain": cand.get("domain"),
                    "type": cand.get("type"),
                    "user_tag": cand.get("user_tag"),
                },
                events=event_summaries,
                total=int(total),
            )
        )

    # Lexical integration: also match tags (groups) by the same substring.
    tag_matches: list[TagMatch] = []
    try:
        tag_candidates = deps.graph.search_groups_by_name(
            name=entity_name,
            domain=domain,
            limit=ENTITY_CANDIDATE_MAX,
        )
        for cand in tag_candidates:
            tag_id = cand.get("id")
            if not tag_id:
                continue
            t_events, t_total = deps.graph.get_group_events(
                group_id=tag_id, page=1, size=page_size,
            )
            t_event_summaries = [_build_event_summary(deps, ev) for ev in t_events]
            t_event_summaries.sort(
                key=lambda s: (
                    -s.influence_score,
                    -(s.order_index or 0),
                )
            )
            tag_matches.append(
                TagMatch(
                    tag={
                        "kind": "tag",  # self-describing kind field
                        "id": tag_id,
                        "name": cand.get("name"),
                        "domain": cand.get("domain"),
                        "parent_id": cand.get("parent_id"),
                        "user_tag": cand.get("user_tag"),
                    },
                    events=t_event_summaries,
                    total=int(t_total),
                )
            )
    except Exception as exc:  # noqa: BLE001
        # Isolate failures — a tag search error must not break entity_lookup.
        logger.warning("entity_lookup tag search failed: %s", exc)

    suggestion = None
    if not matches and not tag_matches:
        suggestion = (
            f"No entity or tag name contains '{entity_name}'. "
            "Try a shorter substring or a different casing."
        )

    # Build the connection map hint by converting matches to temporary SearchHit
    # objects and reusing build_connection_map (same hint system as search).
    # Errors are isolated so that a hint failure does not break entity_lookup.
    # Events from tag_matches are also included as hint candidates (the tag
    # itself is not a SearchHit.kind enum value, so only its events are added).
    hint_block = None
    try:
        from k2g.mcp.hint import build_connection_map
        hint_hits: list[SearchHit] = []
        seen_event_ids: set[str] = set()
        for m in matches:
            ent = m.entity
            ent_id = ent.get("id")
            if ent_id:
                hint_hits.append(SearchHit(
                    kind="entity", id=str(ent_id), score=0.0,
                    domain=ent.get("domain"),
                    name_or_summary=str(ent.get("name") or ""),
                    metadata={"type": ent.get("type")},
                ))
            for ev in m.events:
                if len(seen_event_ids) >= _HINT_MAX_EVENTS:
                    break
                if not ev.id or ev.id in seen_event_ids:
                    continue
                seen_event_ids.add(ev.id)
                hint_hits.append(SearchHit(
                    kind="event", id=str(ev.id), score=0.0, domain=ev.domain,
                ))
        for tm in tag_matches:
            for ev in tm.events:
                if len(seen_event_ids) >= _HINT_MAX_EVENTS:
                    break
                if not ev.id or ev.id in seen_event_ids:
                    continue
                seen_event_ids.add(ev.id)
                hint_hits.append(SearchHit(
                    kind="event", id=str(ev.id), score=0.0, domain=ev.domain,
                ))
        hint_block = build_connection_map(deps, hint_hits)
    except Exception as exc:  # noqa: BLE001
        logger.warning("entity_lookup connection map build failed: %s", exc)

    response = EntityLookupResponse(
        query_name=entity_name,
        hint=hint_block,
        suggestion=suggestion,
        matches=matches,
        tag_matches=tag_matches,
    ).model_dump()
    from k2g.mcp.contracts import match_for_tool, extract_evidence
    active = match_for_tool("mweft_entity_lookup")
    if active:
        response["context"] = {"active_contracts": active}
    ev = extract_evidence(response)
    if ev:
        response["evidence"] = ev
    response["searched_scope"] = scope_to_str(scope)
    return response


# ---------------------------------------------------------------------------
# 3. k2g_context_detail
# ---------------------------------------------------------------------------

def context_detail_tool(
    deps: Deps,
    *,
    cg_id: str,
    depth: int = 2,
) -> dict[str, Any]:
    """Return a ContextNode's full view: narrative + hierarchy + member events.

    depth=0 → only the CG + member events (no hierarchy walk)
    depth≥1 → include children + ancestor chain (clamped at 3)
    """
    d = clamp_depth(depth)
    cg = deps.graph.get_context_group(cg_id)
    if not cg:
        return {
            "error": "context_group_not_found",
            "detail": f"No ContextGroup with id={cg_id!r}",
        }

    children: list[dict[str, Any]] = []
    ancestors: list[dict[str, Any]] = []
    parent: dict[str, Any] | None = None
    if d >= 1:
        try:
            hier = deps.graph.get_cg_hierarchy(cg_id)
        except Exception:  # noqa: BLE001
            logger.exception("get_cg_hierarchy failed for %s", cg_id)
            hier = {"children": [], "ancestors": []}
        children = list(hier.get("children", []))
        ancestors = list(hier.get("ancestors", []))
        if len(ancestors) >= 2:
            parent = ancestors[1]

    try:
        event_rows = deps.graph.get_cg_events(cg_id)
    except Exception:  # noqa: BLE001
        logger.exception("get_cg_events failed for %s", cg_id)
        event_rows = []

    events = [_build_event_summary(deps, ev) for ev in event_rows]

    response = ContextDetailResponse(
        cg_id=str(cg.get("id", cg_id)),
        name=str(cg.get("name") or ""),
        narrative_summary=cg.get("narrative_summary"),
        stage=cg.get("stage"),
        domain=cg.get("domain"),
        confidence=_to_float(cg.get("confidence")),
        member_count_own=_to_int(cg.get("member_count_own")),
        member_count_total=_to_int(cg.get("member_count_total")),
        parent=parent,
        children=children,
        ancestors=ancestors,
        events=events,
    ).model_dump()
    from k2g.mcp.contracts import match_for_tool, extract_evidence
    active = match_for_tool("mweft_auto_tag_detail")
    if active:
        response["context"] = {"active_contracts": active}
    ev = extract_evidence(response)
    if ev:
        response["evidence"] = ev
    return response


# ---------------------------------------------------------------------------
# 4. k2g_get_event_content — raw content fetch (summary fidelity metric)
# ---------------------------------------------------------------------------

def get_event_content_tool(
    deps: Deps,
    *,
    event_id: str,
    include_raw: bool = True,
) -> dict[str, Any]:
    """Fetch the raw content of an event when the search summary is insufficient.

    Use this when the LLM determines that the summary returned by
    ``k2g_search`` does not contain enough information. This call is counted
    as a *raw_fetch* in telemetry and is the primary input for the summary
    fidelity metric (raw_fetches / search_hits).

    Args:
        event_id: events.id (e.g. "ev_xxx") or vector_id (e.g. "vec_xxx").
        include_raw: When True, fetch the full body from storage_uri.
            When False, return metadata only.

    Returns:
        {"event_id": ..., "found": bool, "summary": ..., "content": "...",
         "char_count": N, "storage_uri": ..., "section_heading": ...,
         "domain": ...}.
    """
    # Look up the event — id takes priority; fall back to vector_id.
    # The events table stores vector_id only (no content_id); content store
    # matching is handled by ContentBackend.get_by_vector_id.
    conn = deps.graph._conn
    cur = conn.cursor()
    is_postgres = "postgres" in type(deps.graph).__name__.lower()
    ph = "%s" if is_postgres else "?"
    try:
        cur.execute(
            f"SELECT id, domain, summary, vector_id, timestamp "
            f"FROM events WHERE id = {ph} OR vector_id = {ph} LIMIT 1",
            (event_id, event_id),
        )
        row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_event_content_tool: events lookup failed: %s", exc)
        row = None
    finally:
        try: cur.close()
        except Exception: pass

    if not row:
        return {
            "event_id": event_id, "found": False,
            "error": "event_not_found",
            "detail": f"No event with id or vector_id = {event_id!r}",
        }

    # Normalise row to dict (handles sqlite Row and psycopg cursor results)
    if hasattr(row, "keys"):
        ev = {k: row[k] for k in row.keys()}
    else:
        ev = {
            "id": row[0], "domain": row[1], "summary": row[2],
            "vector_id": row[3], "timestamp": row[4],
        }

    # ContentRecord lookup — match by vector_id
    content_record = None
    try:
        if ev.get("vector_id"):
            content_record = deps.db.content.get_by_vector_id(str(ev["vector_id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_event_content_tool: content lookup failed: %s", exc)

    storage_uri = None
    inline_meta: dict = {}
    content_type: str | None = None
    if content_record is not None:
        storage_uri = content_record.storage_uri
        inline_meta = content_record.inline_meta or {}
        content_type = content_record.content_type

    # Raw text fetch (skipped when include_raw is False)
    raw_text: str | None = None
    char_count: int | None = None
    if include_raw and storage_uri:
        try:
            raw_text = deps.db.object.get_text(storage_uri)
            char_count = len(raw_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_event_content_tool: object fetch failed (%s): %s",
                storage_uri, exc,
            )

    response: dict[str, Any] = {
        "event_id": str(ev.get("id") or event_id),
        "found": True,
        "domain": ev.get("domain"),
        "summary": ev.get("summary"),
        "timestamp": ev.get("timestamp"),
        "content_type": content_type,
        "storage_uri": storage_uri,
        "section_heading": inline_meta.get("section_heading"),
        "inline_meta": inline_meta,
        "content": raw_text,
        "char_count": char_count,
    }
    from k2g.mcp.contracts import match_for_tool, extract_evidence
    active = match_for_tool("mweft_get_event_content")
    if active:
        response["context"] = {"active_contracts": active}
    ev = extract_evidence(response)
    if ev:
        response["evidence"] = ev
    return response


# ---------------------------------------------------------------------------
# 5. k2g_set_influence
# ---------------------------------------------------------------------------

def set_influence_tool(
    deps: Deps,
    *,
    event_id: str,
    score: float,
    reason: str | None = None,
) -> dict[str, Any]:
    """Update events.influence_score and append one row to events_audit.

    K2G does not call this automatically. Only an explicit user or LLM
    invocation changes the score. Returns ``found=False`` when
    ``event_id`` does not exist.
    """
    influence = getattr(deps, "influence", None)
    if influence is None:
        return InfluenceUpdate(
            event_id=event_id, found=False,
            score_before=None, score_after=None, reason=reason,
        ).model_dump()
    result = influence.set_influence(event_id, float(score), reason)
    return InfluenceUpdate(
        event_id=str(result["event_id"]),
        found=bool(result["found"]),
        score_before=result.get("score_before"),
        score_after=result.get("score_after"),
        reason=result.get("reason"),
    ).model_dump()


# ---------------------------------------------------------------------------
# 5. k2g_suggest_influence_review
# ---------------------------------------------------------------------------

def suggest_influence_review_tool(
    deps: Deps,
    *,
    domain: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return groups of events that may need manual influence review.

    Candidates are cases where both an active and a superseded row exist for
    the same (domain, file_path, segment_key). K2G does not make the
    decision — it only surfaces the evidence.
    """
    influence = getattr(deps, "influence", None)
    if influence is None:
        return InfluenceReviewResponse(candidates=[], total=0).model_dump()
    raw = influence.suggest_influence_review(domain=domain, limit=int(limit))
    candidates = [
        InfluenceReviewCandidate(
            domain=str(c["domain"]),
            file_path=str(c["file_path"]),
            segment_key=str(c["segment_key"]),
            candidates=[
                InfluenceReviewMember(
                    event_id=str(m["event_id"]),
                    status=str(m["status"]),
                    influence_score=m.get("influence_score"),
                    file_hash=m.get("file_hash"),
                    ingested_at=m.get("ingested_at"),
                    source_root=m.get("source_root"),
                )
                for m in c["candidates"]
            ],
        )
        for c in raw
    ]
    return InfluenceReviewResponse(
        candidates=candidates, total=len(candidates),
    ).model_dump()
