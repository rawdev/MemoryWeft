"""mweft_search_window — chained, paged search with a deficit signal.

Why a separate tool (``mweft_search`` is left untouched):

``mweft_search`` returns a rich, hint-enriched top-k slice that *looks*
complete — so the LLM treats the bounded payload as a closed world and stops.
This tool does the opposite: it returns a thin, explicit *window* of the
ranked result set (e.g. index 1-10, then 11-20) and **loudly advertises what
lies beyond it** — ``more_available`` / ``next_start`` / ``candidates_ranked``.

The point is behavioural, not just paging: by surfacing the deficit (how much
was *not* shown) and a ready next call, the LLM is nudged to *chain* searches
instead of answering from the first bounded result.

Pure function of ``Deps`` (graph/vector/embedding) — unit-testable with fakes.
Read-only. No hint/connection-map here on purpose: this is the lean
continuation view, not the rich neighbourhood view.
"""

from __future__ import annotations

import logging
from typing import Any

from k2g.mcp.scope import resolve_search_scope, scope_to_str

logger = logging.getLogger(__name__)

# Hard ceiling on how deep a single window may reach into the ranked set.
# Beyond this the result is "capped" — the caller should narrow the query
# rather than page forever.
_RANK_CAP = 200
# Per-window size ceiling (keeps the payload lean).
_MAX_COUNT = 50


def _build_filter_targets(
    deps: Deps,
) -> list[tuple[str, list[str] | None]] | None:
    """Resolve the server-side search scope into ``filter_search_targets``.

    Mirrors ``search_tool``'s scope handling (fail-closed):

    - ``None``  → no scope set → search all permitted domains (no filter).
    - ``[]``    → scope *was* set but every target resolved to nothing
      (e.g. a group with zero events) → caller must search **nothing**, not
      fall back to all domains. Collapsing this to ``None`` would be a
      scope-bypass / cross-domain leak.
    - non-empty → the active filter.
    """
    scope = resolve_search_scope(deps)
    if not scope:
        return None
    targets: list[tuple[str, list[str] | None]] = []
    for t in scope:
        if t.group:
            event_ids = deps.graph.list_event_ids_by_group(t.group)
            if not event_ids:
                continue
            targets.append((t.domain, event_ids))
        else:
            targets.append((t.domain, None))
    return targets  # may be [] — "scope set but empty", NOT None


def _event_hits(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise raw vector-store event rows into ranked hit dicts."""
    out: list[dict[str, Any]] = []
    for hit in raw:
        md = hit.get("metadata", {}) or {}
        distance = hit.get("distance")
        score = hit.get("score")
        if score is None and distance is not None:
            score = 1.0 - float(distance)
        eid = (hit.get("id") or hit.get("vector_id")
               or md.get("vector_id") or md.get("event_id") or "")
        out.append({
            "kind": "event",
            "id": str(eid),
            "score": float(score or 0.0),
            "domain": hit.get("domain") or md.get("domain"),
            "name_or_summary": str(hit.get("summary") or md.get("summary")
                                   or md.get("text") or ""),
        })
    return out


def _entity_hits(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise raw vector-store entity rows into ranked hit dicts."""
    out: list[dict[str, Any]] = []
    for hit in raw:
        md = hit.get("metadata", {}) or {}
        distance = hit.get("distance")
        score = hit.get("score")
        if score is None and distance is not None:
            score = 1.0 - float(distance)
        eid = (hit.get("id") or hit.get("entity_id") or md.get("entity_id") or "")
        out.append({
            "kind": "entity",
            "id": str(eid),
            "score": float(score or 0.0),
            "domain": hit.get("domain") or md.get("domain"),
            "name_or_summary": str(hit.get("name") or md.get("name") or ""),
        })
    return out


def search_window_tool(
    deps: Deps,
    *,
    query: str,
    start: int = 1,
    count: int = 10,
    mode: str = "events",
) -> dict[str, Any]:
    """Return a ranked *window* ``[start, start+count)`` plus a deficit signal.

    Args:
        query: natural-language query (same embedding path as ``mweft_search``).
        start: 1-based rank index of the first result to return.
        count: window size (clamped to ``_MAX_COUNT``).
        mode: ``events`` | ``entities`` | ``hybrid``.

    Chain usage: call once with ``start=1``; if ``more_available`` is true,
    call again with ``start=next_start`` to pull the next window.
    """
    normalized_mode = mode if mode in ("hybrid", "events", "entities") else "events"
    start = max(1, int(start))
    count = max(1, min(int(count), _MAX_COUNT))

    if not query or not query.strip():
        return {
            "query": query, "mode": normalized_mode,
            "window": {"start": start, "end": start - 1, "returned": 0},
            "hits": [], "more_available": False, "next_start": None,
            "candidates_ranked": 0, "capped": False,
            "searched_scope": scope_to_str(resolve_search_scope(deps)),
        }

    need = start - 1 + count           # last index the window needs (0-based: need-1)
    fetch = min(need + 1, _RANK_CAP)   # +1 peek → detect "more beyond window"

    filter_targets = _build_filter_targets(deps)
    # Scope set but resolved to nothing → search nothing (fail-closed), do NOT
    # fall back to all domains. Matches search_tool's empty-target guard.
    if filter_targets is not None and not filter_targets:
        return {
            "query": query, "mode": normalized_mode,
            "window": {"start": start, "end": start - 1, "returned": 0},
            "hits": [], "more_available": False, "next_start": None,
            "candidates_ranked": 0, "capped": False,
            "searched_scope": scope_to_str(resolve_search_scope(deps)),
        }

    qv = deps.embedding.embed(query)

    ranked: list[dict[str, Any]] = []
    if normalized_mode in ("hybrid", "events"):
        ranked += _event_hits(deps.vector.search(
            query_vector=qv, filter_search_targets=filter_targets, limit=fetch,
        ))
    if normalized_mode in ("hybrid", "entities"):
        domains = None
        if filter_targets is not None:
            domains = list(dict.fromkeys(
                d for d, _ in filter_targets if d
            )) or None
        ranked += _entity_hits(deps.vector.search_similar_entities(
            query_vector=qv, domain=domains, limit=fetch,
        ))

    ranked.sort(key=lambda h: h["score"], reverse=True)

    window = ranked[start - 1: start - 1 + count]
    for i, h in enumerate(window):
        h["index"] = start + i        # 1-based global rank for chaining

    # ``need + 1`` (window end + peek) past the cap means we could not even
    # fetch enough to confirm the window — the query is too broad to page.
    capped = (need + 1) > _RANK_CAP
    if capped:
        more_available = len(ranked) >= _RANK_CAP  # hit the ceiling → more exist
        next_start = None                          # cannot page past the cap
    else:
        more_available = len(ranked) > need        # the peek row exists
        next_start = (start + count) if more_available else None

    response: dict[str, Any] = {
        "query": query,
        "mode": normalized_mode,
        "window": {
            "start": start,
            "end": start - 1 + len(window),
            "returned": len(window),
        },
        "hits": window,
        # --- deficit signal: what was NOT shown -------------------------
        "more_available": more_available,
        "next_start": next_start,
        "candidates_ranked": len(ranked),  # "≥" if capped
        "capped": capped,
        "searched_scope": scope_to_str(resolve_search_scope(deps)),
    }
    if more_available and not capped:
        response["instruction"] = (
            f"Showing rank {start}-{start - 1 + len(window)}; more results "
            f"exist. To continue, call mweft_search_window(query, "
            f"start={next_start})."
        )
    elif capped:
        response["instruction"] = (
            f"Rank window reached the {_RANK_CAP}-result cap and more still "
            f"match — the query is too broad to page through. Narrow it."
        )
    return response


# Late import for the type hint only (avoid a circular import at module load).
from k2g.mcp.factory import Deps  # noqa: E402

__all__ = ["search_window_tool"]
