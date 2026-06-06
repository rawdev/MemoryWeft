"""BP-75 Step 5 — subprocess internal write endpoint.

The hub forwards every MCP write call to ``POST /api/internal/write/{op}``,
which dispatches to the equivalent MCP tool function inside the project
subprocess.  Because all MCP clients share this single subprocess, every
write to the project's SQLite DB is serialised through *one* process —
sidestepping SQLite's single-writer-across-processes contention.

Tools are reused unchanged from ``k2g.mcp`` (memory / covenant / share /
plan / set_influence) — the dispatcher just unpacks the JSON payload as
keyword arguments.  ``Deps`` is built lazily from the existing web
``_stores`` so we share the same DB connections.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Header

from k2g.web.deps import get_stores_dep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["internal-write"])


# ---------------------------------------------------------------------------
# Lazy deps / plan-deps construction from already-initialised web stores
# ---------------------------------------------------------------------------

_deps_cache: Any | None = None
_plan_deps_cache: Any | None = None
_plan_deps_error: Exception | None = None


def _build_deps(stores: dict[str, Any]) -> Any:
    """Build an ``mcp.factory.Deps`` from the subprocess web stores."""
    global _deps_cache
    if _deps_cache is not None:
        return _deps_cache

    from k2g.core.config import get_settings
    from k2g.mcp.factory import Deps

    _deps_cache = Deps(
        db=stores.get("_db"),
        graph=stores["graph"],
        vector=stores["vector"],
        embedding=stores.get("embedding"),
        query=None,
        settings=get_settings(),
        manifest=None,
        influence=None,
    )
    return _deps_cache


def _build_plan_deps(stores: dict[str, Any]) -> Any:
    """Build ``PlanDeps`` lazily.  May fail if training/LLM deps are absent —
    cached so we don't re-fail on every call."""
    global _plan_deps_cache, _plan_deps_error
    if _plan_deps_cache is not None:
        return _plan_deps_cache
    if _plan_deps_error is not None:
        raise _plan_deps_error

    try:
        from k2g.mcp.factory import build_plan_deps

        d = _build_deps(stores)
        _plan_deps_cache = build_plan_deps(deps=d)
    except Exception as exc:  # noqa: BLE001 — surface as 503 to caller
        _plan_deps_error = exc
        raise
    return _plan_deps_cache


def _reset_caches_for_testing() -> None:
    """Clear lazy caches — used by tests that swap stores between cases."""
    global _deps_cache, _plan_deps_cache, _plan_deps_error
    _deps_cache = None
    _plan_deps_cache = None
    _plan_deps_error = None


# ---------------------------------------------------------------------------
# Op dispatch — 13 MCP write tools (BP-77 §8)
# ---------------------------------------------------------------------------

def _dispatch_remember(stores, **kw):
    from k2g.mcp.memory_tools import remember_tool
    return remember_tool(_build_deps(stores), **kw)


def _dispatch_remember_edit(stores, **kw):
    from k2g.mcp.memory_tools import remember_edit_tool
    return remember_edit_tool(_build_deps(stores), **kw)


def _dispatch_covenant_add(stores, **kw):
    from k2g.mcp.covenant_tools import covenant_add_tool
    return covenant_add_tool(_build_deps(stores), **kw)


def _dispatch_covenant_remove(stores, **kw):
    from k2g.mcp.covenant_tools import covenant_remove_tool
    return covenant_remove_tool(_build_deps(stores), **kw)


def _dispatch_share_group_create(stores, **kw):
    from k2g.mcp.share_tools import share_group_create_tool
    return share_group_create_tool(_build_deps(stores), **kw)


def _dispatch_share_group_delete(stores, **kw):
    from k2g.mcp.share_tools import share_group_delete_tool
    return share_group_delete_tool(_build_deps(stores), **kw)


def _dispatch_share_member_add(stores, **kw):
    from k2g.mcp.share_tools import share_member_add_tool
    return share_member_add_tool(_build_deps(stores), **kw)


def _dispatch_share_member_remove(stores, **kw):
    from k2g.mcp.share_tools import share_member_remove_tool
    return share_member_remove_tool(_build_deps(stores), **kw)


def _dispatch_set_influence(stores, **kw):
    from k2g.mcp.tools import set_influence_tool
    return set_influence_tool(_build_deps(stores), **kw)


def _dispatch_etg_create(stores, **kw):
    from k2g.mcp.plan_tools import etg_create_tool
    return etg_create_tool(_build_plan_deps(stores), **kw)


def _dispatch_plan_create(stores, **kw):
    from k2g.mcp.plan_tools import plan_create_tool
    return plan_create_tool(_build_plan_deps(stores), **kw)


def _dispatch_direction_create(stores, **kw):
    from k2g.mcp.plan_tools import direction_create_tool
    return direction_create_tool(_build_plan_deps(stores), **kw)


def _dispatch_plan_confirm(stores, **kw):
    from k2g.mcp.plan_tools import plan_confirm_tool
    return plan_confirm_tool(_build_plan_deps(stores), **kw)


def _dispatch_plan_abandon(stores, **kw):
    from k2g.mcp.plan_tools import plan_abandon_tool
    return plan_abandon_tool(_build_plan_deps(stores), **kw)


DispatchFn = Callable[..., Any]

DISPATCH: dict[str, DispatchFn] = {
    "remember": _dispatch_remember,
    "remember_edit": _dispatch_remember_edit,
    "covenant_add": _dispatch_covenant_add,
    "covenant_remove": _dispatch_covenant_remove,
    "share_group_create": _dispatch_share_group_create,
    "share_group_delete": _dispatch_share_group_delete,
    "share_member_add": _dispatch_share_member_add,
    "share_member_remove": _dispatch_share_member_remove,
    "set_influence": _dispatch_set_influence,
    "etg_create": _dispatch_etg_create,
    "plan_create": _dispatch_plan_create,
    "direction_create": _dispatch_direction_create,
    "plan_confirm": _dispatch_plan_confirm,
    "plan_abandon": _dispatch_plan_abandon,
}

PLAN_OPS = {
    "etg_create", "plan_create", "direction_create",
    "plan_confirm", "plan_abandon",
}


# ---------------------------------------------------------------------------
# Token auth (opt-in via K2G_HUB_REQUIRE_TOKEN)
# ---------------------------------------------------------------------------

def _expected_token() -> str | None:
    import os
    return os.environ.get("K2G_HUB_TOKEN", "").strip() or None


def _require_token_enabled() -> bool:
    import os
    return os.environ.get("K2G_HUB_REQUIRE_TOKEN", "").strip().lower() in (
        "1", "true", "yes",
    )


def _verify_token(x_mweft_token: str | None) -> None:
    if not _require_token_enabled():
        return
    expected = _expected_token()
    if expected is None or x_mweft_token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-Mweft-Token")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/api/internal/write/{op}")
def write_op(
    op: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    stores: dict[str, Any] = Depends(get_stores_dep),
    x_mweft_token: str | None = Header(default=None, alias="X-Mweft-Token"),
) -> Any:
    _verify_token(x_mweft_token)

    handler = DISPATCH.get(op)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown write op: {op!r}. Valid ops: {sorted(DISPATCH)}",
        )

    try:
        return handler(stores, **payload)
    except HTTPException:
        raise
    except TypeError as exc:
        # unexpected kwargs etc — surface as 400 so the caller can fix
        raise HTTPException(
            status_code=400,
            detail=f"invalid payload for op={op!r}: {exc}",
        ) from exc
    except NotImplementedError as exc:
        # plan_deps build failure (LLM/training layer absent)
        raise HTTPException(
            status_code=503,
            detail=f"op {op!r} unavailable: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("internal write op %r failed", op)
        raise HTTPException(
            status_code=500,
            detail=f"op {op!r} failed: {exc}",
        ) from exc
