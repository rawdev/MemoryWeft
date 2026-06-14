"""Memory MCP tools (k2g_remember + k2g_remember_edit).

Allows an AI agent to explicitly save the current conversation context to
K2G MemoryWeft, and to correct hallucinated entities after saving.

Design principles:
- *The event itself is real* (conversation content) — preserved as-is
- *Only entities can be hallucinated* — only the ``participated_in``
  relationship is removed (3 SQL statements, millisecond-scale)
- The caller provides NER results (K2G does not make model calls).
  Only embeddings are generated on the K2G side.
- Requires an explicit keyword trigger and user consent before calling.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from k2g.mcp.factory import Deps

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DOMAIN = "ai_memory"
MAX_CONTENT_CHARS = 50_000
MAX_SUMMARY_CHARS = 500


def remember_tool(
    deps: Deps,
    *,
    content: str,
    summary: str,
    entities: list[dict[str, Any]],
    working_folder: str | None = None,
    tag: str | None = None,
    timestamp: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Save the current conversation context to K2G MemoryWeft immediately.

    The save domain is determined solely by server-side environment variables
    — the caller (LLM) cannot control it (prevents an LLM hallucination from
    scattering data to wrong domains). Priority: ``K2G_USER_MEMORY_SAVE_DOMAIN``
    env → fallback ``ai_memory``.

    Save flow:
    1. Apply env defaults:
       - working_folder: uses settings.user_memory_save_group
         (K2G_USER_MEMORY_SAVE_GROUP env) when not provided
       - domain: server-enforced from env / default (not in LLM signature)
       - timestamp: defaults to NOW() (ISO 8601)
    2. Validation
    3. content_store.save (raw content archival)
    4. Embedding generation (from summary)
    5. graph.create_event + vector.upsert
    6. Entity UPSERT + participated_in links
    7. Tag tree (sub-tag path under working_folder root) + event_member_of

    The response includes ``saved_entities`` so the LLM can show them to
    the user; hallucinated entities can be corrected via
    ``k2g_remember_edit``.

    Each save targets a single (domain, tag) tuple — events.domain is NOT
    NULL and an event belongs to exactly one tag. This is intentionally
    separate from the search default K2G_USER_SEARCH_TARGETS (CSV tuple
    list): search is LLM-arg-first, save is env-enforced.

    Internal note: tag maps to the ``groups`` DB table; sub-tag paths use
    '/' as separator.
    """
    # Internal variable name kept as 'category' for consistency with the
    # groups table code.
    category = tag
    from datetime import datetime, timezone

    # Save-context resolution uses a shared helper (consistent with CLI
    # ManifestProducer).
    from k2g.memory.save_context import (
        resolve_save_domain,
        resolve_working_folder,
    )

    # Apply env defaults
    settings = getattr(deps, "settings", None)
    applied_defaults: dict[str, Any] = {}

    # working_folder default — K2G_USER_MEMORY_SAVE_GROUP (save-only, single)
    working_folder, _wf_applied = resolve_working_folder(settings, working_folder)
    applied_defaults.update(_wf_applied)

    # domain — server-enforced; not in the LLM call signature.
    # env K2G_USER_MEMORY_SAVE_DOMAIN → fallback DEFAULT_MEMORY_DOMAIN.
    domain, _dom_applied = resolve_save_domain(settings)
    applied_defaults.update(_dom_applied)

    # timestamp default — ISO 8601 NOW()
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()

    # Validation
    if not content or not content.strip():
        return {"error": "content is required"}
    if not summary or not summary.strip():
        return {"error": "summary is required"}
    if len(content) > MAX_CONTENT_CHARS:
        return {"error": f"content too long (max {MAX_CONTENT_CHARS} chars)"}
    if len(summary) > MAX_SUMMARY_CHARS:
        return {"error": f"summary too long (max {MAX_SUMMARY_CHARS} chars)"}
    if working_folder is None or not working_folder.strip():
        return {
            "error": (
                "working_folder not set and K2G_USER_MEMORY_SAVE_GROUP is absent. "
                "Provide it as an argument or set the env variable."
            ),
        }

    # 1. Generate embedding (from summary)
    try:
        embedding_vector = deps.embedding.embed(summary)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Embedding generation failed: {exc}"}

    # 2. vector_id (UUID)
    vector_id = f"mem_{uuid.uuid4().hex}"

    # 3. Save to content_store (raw content archival via inline_meta)
    content_inline_meta: dict[str, Any] = {
        "source": "mweft_remember",
        "working_folder": working_folder,
        "content": content,
    }
    if conversation_id:
        content_inline_meta["conversation_id"] = conversation_id

    try:
        content_id = deps.db.content.save(
            domain=domain,
            vector_id=vector_id,
            content_type="text/plain",
            storage_uri=f"inline://{vector_id}",
            inline_meta=content_inline_meta,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Content store save failed: {exc}"}

    # 4. Event INSERT (graph store)
    graph = deps.graph
    # order_index — max + 1 within the same domain (simple approach)
    try:
        cur = graph._conn.cursor()
        backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
        ph = "%s" if backend == "postgres" else "?"
        cur.execute(
            f"SELECT COALESCE(MAX(order_index), -1) + 1 FROM events WHERE domain = {ph}",
            (domain,),
        )
        row = cur.fetchone()
        next_order = int(
            row[0] if not hasattr(row, "keys")
            else (row[0] if isinstance(row, tuple) else list(row)[0])
        )
    except Exception:  # noqa: BLE001
        next_order = 0

    event_id = graph.create_event(
        vector_id=vector_id,
        domain=domain,
        timestamp=timestamp,
        order_index=next_order,
        summary=summary,
        ner_method="caller_provided",
    )

    # 5. Vector store upsert (persist embedding)
    try:
        deps.vector.upsert(
            vector_id=vector_id,
            vector=embedding_vector,
            metadata={},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "vector.upsert failed (event preserved, embedding not stored): %s", exc,
        )

    # 6. Entity UPSERT + participated_in links
    saved_entities: list[dict[str, Any]] = []
    entity_ids_for_connection: list[str] = []
    for ent in entities or []:
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        ent_type = ent.get("type") or ""
        # Pre-check existence to determine is_new
        backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
        ph = "%s" if backend == "postgres" else "?"
        cur = graph._conn.cursor()
        cur.execute(
            f"SELECT id FROM entities WHERE name = {ph} AND domain = {ph}",
            (name, domain),
        )
        existing = cur.fetchone()
        is_new = existing is None

        entity_id = graph.link_or_create_entity(name=name, domain=domain, type=ent_type)
        graph.link_participated_in(entity_id=entity_id, event_id=event_id)
        entity_ids_for_connection.append(entity_id)

        saved_entities.append({
            "name": name,
            "entity_id": entity_id,
            "is_new": is_new,
            "type": ent_type,
        })

    # 6.5 entity_connection — pairwise co-occurrence (N×N/2)
    # Mirrors the build-pipeline pattern in producer/_shared.py so that
    # domains populated via mw save also have entity_connection edges visible
    # in graph visualisations.
    # Outlier events with many entities are skipped to avoid N(N-1)/2 blowup:
    # the cap matches settings.community_max_entities_per_event (default 50),
    # keeping entity_connection density consistent between the producer and
    # remember ingest paths, and preventing large events from distorting the
    # community graph.
    from k2g.core.config import get_settings
    _max_ent = get_settings().community_max_entities_per_event
    if 2 <= len(entity_ids_for_connection) < _max_ent:
        for i in range(len(entity_ids_for_connection)):
            for j in range(i + 1, len(entity_ids_for_connection)):
                graph.upsert_entity_connection(
                    entity_ids_for_connection[i],
                    entity_ids_for_connection[j],
                    event_id,
                )

    # 7. Group + category tree + forced save_tags + event_member_of
    # Resolution logic uses shared helper (consistent with CLI ManifestProducer
    # group structure).
    from k2g.memory.save_context import (
        attach_event_memberships,
        resolve_tag_groups,
    )

    _tagres = resolve_tag_groups(
        graph,
        domain=domain,
        working_folder=working_folder,
        category=category,
        settings=settings,
    )
    attach_event_memberships(graph, event_id, _tagres)

    # Local variables for response assembly (backward-compatible names)
    group_id = _tagres.group_id
    category_enabled = _tagres.category_enabled
    category_resolved = _tagres.category_resolved
    category_tree = _tagres.category_tree
    forced_cfg = _tagres.forced_cfg
    forced_tag_ids = _tagres.forced_tag_ids

    # 7.5 Sync entity vector recompute (internal option, invisible to LLM)
    # When settings.entity_vector_sync_recompute is true (default), the
    # mean+L2norm centroid for affected entities is recomputed immediately.
    # ~0.5 ms overhead for 3-5 entities; drift = 0. Failures do not roll back
    # the saved event.
    if (
        settings is not None
        and getattr(settings, "entity_vector_sync_recompute", True)
        and entity_ids_for_connection
    ):
        try:
            from k2g.trainer.projection import ProjectionEngine
            engine = ProjectionEngine(
                graph_store=graph,
                vector_store=deps.vector,
                content_store=None,
                object_storage=None,
                embedding_client=None,  # mean+L2norm recompute needs no embedding client
            )
            for eid in entity_ids_for_connection:
                try:
                    engine.compute_entity_centroid(eid, use_cache=False)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Sync entity vector recompute failed: entity_id=%s, error=%s",
                        eid, exc,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sync entity vector hook init failed: %s", exc)

    # 7.6 Sync incremental jaccard (same gate as entity vector recompute)
    # Computes event_jaccard_connected edges for the new event immediately
    # so that the jaccard hint channel is populated for events saved via
    # mweft_remember. Failures do not affect the saved event.
    if (
        settings is not None
        and getattr(settings, "entity_vector_sync_recompute", True)
    ):
        try:
            from k2g.trainer.jaccard import JaccardPhase
            JaccardPhase().incremental(deps.db, domain=domain, event_id=event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sync incremental jaccard failed: %s", exc)

    # 8. Build response
    # tag_id = id of the finally resolved tag (sub if present, else root).
    # tag_created = whether that tag was newly created in this call.
    final_tag_created = _tagres.final_tag_created
    response: dict[str, Any] = {
        "event_id": event_id,
        "tag_id": group_id,
        "tag_created": final_tag_created,
        "saved_entities": saved_entities,
        "summary": summary,
        "vector_id": vector_id,
        "content_id": content_id,
        "edit_command": (
            f"mweft_remember_edit(event_id='{event_id}', "
            f"remove_entities=[<name_or_id>, ...])"
        ),
    }
    if applied_defaults:
        response["applied_defaults"] = applied_defaults
    # Sub-tag path — lets the LLM treat the tag set as closed on the next save.
    # Internal DB column source='mweft_category' is preserved; only the
    # surface key is 'tag'.
    if category_enabled:
        response["tag_tree"] = category_tree
        if category_resolved is not None:
            response["tag"] = category_resolved

    # K2G_USER_MEMORY_SAVE_TAGS (manager project settings) — forced tags.
    # These are actually attached via event_member_of above (not just
    # suggested); applied_save_tags surfaces which tags were force-applied.
    if forced_tag_ids:
        response["applied_save_tags"] = forced_cfg
        response["applied_save_tag_ids"] = list(forced_tag_ids)

    # Contract enrichment + evidence
    from k2g.mcp.contracts import match_for_tool, extract_evidence
    active = match_for_tool("mweft_remember", node_kind="event")
    if active:
        response["context"] = {"active_contracts": active}
    ev = extract_evidence(response)
    if ev:
        response["evidence"] = ev

    # Fire-and-forget community recomputation trigger after ingest.
    # Debounced + in-flight guard + dedicated DbStore background thread —
    # does not block the save response. All failures are absorbed.
    try:
        from k2g.trainer.community_trigger import trigger_recompute_async
        trigger_recompute_async(domain)
    except Exception:  # noqa: BLE001
        pass

    return response


def remember_edit_tool(
    deps: Deps,
    *,
    event_id: str,
    remove_entities: list[str] | None = None,
    add_entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Correct entity relationships on a saved event. The event itself is preserved.

    Based on the principle that *the event is real* (conversation content)
    while *only entities can be hallucinated*. Only the ``participated_in``
    edge is removed (3 SQL statements); orphaned entities are marked
    deprecated. The event row and its summary / group memberships are left
    intact.
    """
    if not event_id or not event_id.strip():
        return {"error": "event_id is required"}

    graph = deps.graph
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    ph = "%s" if backend == "postgres" else "?"
    conn = graph._conn

    # Backend-portable "not deprecated" predicate. Postgres rejects `= 0` on a
    # BOOLEAN column with a hard type error (operator does not exist:
    # boolean = integer) — an `OR ... = FALSE` fallback does not save it, the
    # whole statement fails to plan. SQLite may store deprecated as 0/1 or
    # 'f'/'false' strings, so it needs the IN-list (see BP-92 lexical unify).
    e_live = (
        "(e.deprecated = FALSE OR e.deprecated IS NULL)"
        if backend == "postgres"
        else "(e.deprecated IN (0, 'f', 'false') OR e.deprecated IS NULL)"
    )

    # Look up the event and its domain
    cur = conn.cursor()
    cur.execute(f"SELECT domain FROM events WHERE id = {ph}", (event_id,))
    ev_row = cur.fetchone()
    if ev_row is None:
        return {"error": f"event_id not found: {event_id}"}
    event_domain = ev_row[0] if not hasattr(ev_row, "keys") else (
        ev_row["domain"] if isinstance(ev_row, dict) or hasattr(ev_row, "__getitem__")
        else ev_row[0]
    )

    removed_count = 0
    added_count = 0
    orphaned_entity_ids: list[str] = []

    # ---------------- 1. Process remove_entities -------------------------
    if remove_entities:
        # Resolve names or ids to entity_ids
        placeholders = ", ".join([ph] * len(remove_entities))
        cur.execute(
            f"SELECT id FROM entities "
            f"WHERE (id IN ({placeholders}) OR name IN ({placeholders})) "
            f"AND domain = {ph}",
            (*remove_entities, *remove_entities, event_domain),
        )
        resolved_ids = [r[0] for r in cur.fetchall()]

        if resolved_ids:
            # 1a. Delete participated_in rows
            ph_resolved = ", ".join([ph] * len(resolved_ids))
            cur.execute(
                f"DELETE FROM participated_in "
                f"WHERE event_id = {ph} AND entity_id IN ({ph_resolved})",
                (event_id, *resolved_ids),
            )
            removed_count = cur.rowcount if cur.rowcount > 0 else 0

            # 1b. Decrement entity_connection.event_count for pairs in this event
            cur.execute(
                f"UPDATE entity_connection SET event_count = event_count - 1 "
                f"WHERE (a_id IN ({ph_resolved}) OR b_id IN ({ph_resolved})) "
                f"AND event_count > 0",
                (*resolved_ids, *resolved_ids),
            )

            # 1c. Mark orphaned entities as deprecated
            cur.execute(
                f"SELECT id FROM entities WHERE id IN ({ph_resolved}) "
                f"AND NOT EXISTS (SELECT 1 FROM participated_in "
                f"WHERE participated_in.entity_id = entities.id)",
                tuple(resolved_ids),
            )
            orphans = [r[0] for r in cur.fetchall()]
            if orphans:
                ph_orphans = ", ".join([ph] * len(orphans))
                # SQLite: deprecated INTEGER, Postgres: BOOLEAN
                deprecated_val = 1 if backend == "sqlite" else True
                cur.execute(
                    f"UPDATE entities SET deprecated = {ph} WHERE id IN ({ph_orphans})",
                    (deprecated_val, *orphans),
                )
                orphaned_entity_ids = list(orphans)

            conn.commit()

    # ---------------- 2. Process add_entities ----------------------------
    if add_entities:
        # For each new entity, add pairwise connections with entities already
        # on the event. Re-queried each iteration to include entities added in
        # earlier loop steps.
        for ent in add_entities:
            name = (ent.get("name") or "").strip()
            if not name:
                continue
            ent_type = ent.get("type") or ""
            entity_id = graph.link_or_create_entity(
                name=name, domain=event_domain, type=ent_type,
            )

            # Fetch other (non-deprecated, non-self) entities on this event
            cur.execute(
                f"SELECT e.id FROM entities e "
                f"JOIN participated_in p ON p.entity_id = e.id "
                f"WHERE p.event_id = {ph} AND e.id != {ph} "
                f"AND {e_live}",
                (event_id, entity_id),
            )
            other_ids = [r[0] for r in cur.fetchall()]

            graph.link_participated_in(entity_id=entity_id, event_id=event_id)
            for other_id in other_ids:
                graph.upsert_entity_connection(entity_id, other_id, event_id)
            added_count += 1

    # ---------------- 3. Return currently linked entities ----------------
    cur.execute(
        f"SELECT e.id, e.name, e.type FROM entities e "
        f"JOIN participated_in p ON p.entity_id = e.id "
        f"WHERE p.event_id = {ph} "
        f"AND {e_live}",
        (event_id,),
    )
    now_linked = [
        {"entity_id": r[0], "name": r[1], "type": r[2]}
        for r in cur.fetchall()
    ]

    return {
        "event_id": event_id,
        "removed_count": removed_count,
        "added_count": added_count,
        "now_linked_entities": now_linked,
        "orphaned_entities": orphaned_entity_ids,
    }


__all__ = [
    "remember_tool",
    "remember_edit_tool",
    "DEFAULT_MEMORY_DOMAIN",
    "MAX_CONTENT_CHARS",
    "MAX_SUMMARY_CHARS",
]
