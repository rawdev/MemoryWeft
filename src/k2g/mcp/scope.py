"""MWeft read-scope policy -- the server decides search targets.

The search *scope* (which domain/group to look at) for read tools
(search / entity_lookup / temporal_flow) is determined by the MWeft
server, not the caller (LLM). If the LLM narrows the scope, data is
silently omitted -- scope is a data-layer policy.

``resolve_search_scope`` is the single decision point:

- Today: env ``K2G_USER_SEARCH_TARGETS``
  (``settings.user_search_targets``).
- Future (central RLS): ``deps`` becomes an authenticated principal
  and this function returns the principal's permission set. Tool
  signatures and core logic remain unchanged.

DB-layer RLS / query_filter remains as a row-level backstop
(defense in depth).
"""

from __future__ import annotations

from typing import Any

from k2g.core.config import SearchTarget


def resolve_search_scope(deps: Any) -> list[SearchTarget]:
    """Return (domain, optional group) targets this caller may search.

    Empty list means search all permitted domains (no filter),
    matching the existing semantics of ``K2G_USER_SEARCH_TARGETS``.
    """
    settings = getattr(deps, "settings", None)
    if settings is None:
        return []
    return list(getattr(settings, "user_search_targets", []) or [])


def scope_to_str(targets: list[SearchTarget]) -> list[str]:
    """Human-readable form for the ``searched_scope`` response field.

    The caller can *see* the scope but cannot *change* it. Empty
    list maps to ``["(all permitted domains)"]``.
    """
    if not targets:
        return ["(all permitted domains)"]
    return [t.to_str() for t in targets]
