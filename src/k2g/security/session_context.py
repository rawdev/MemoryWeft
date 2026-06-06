"""Session context (user_id / home_domain) injection API.

Postgres: ``SET LOCAL k2g.user_id = '...'`` -- the RLS policy reads
it via ``current_setting``.
SQLite: thread-local store (``_THREAD_CTX``) -- no RLS support, so
query_filter references it to auto-inject into SELECT WHERE clauses.

When not called, the default is to return all rows (preserves existing
behaviour) -- safe for incremental adoption.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionContext:
    """User context for the current query.

    Attrs:
        user_id: OAuth subject or authenticated user id.  None means
            unauthenticated (returns all rows by default).
        home_domain: The user's home domain.  Used to classify
            cross-domain queries as *foreign* domain access.
        roles: User role list (admin, etc.).  Used by RLS policy in
            future.
    """

    user_id: str | None = None
    home_domain: str | None = None
    roles: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Thread-local store (referenced by the sqlite app-level query filter)
# ---------------------------------------------------------------------------


_THREAD_CTX = threading.local()


def get_session_context() -> SessionContext | None:
    """Current thread's session context.  None if not set."""
    return getattr(_THREAD_CTX, "ctx", None)


def clear_session_context() -> None:
    """Remove thread-local context (for tests / session teardown)."""
    if hasattr(_THREAD_CTX, "ctx"):
        delattr(_THREAD_CTX, "ctx")


# ---------------------------------------------------------------------------
# Injection API -- branches by backend
# ---------------------------------------------------------------------------


def set_session_context(
    graph_store: Any,
    *,
    user_id: str | None,
    home_domain: str | None,
    roles: tuple[str, ...] = (),
) -> SessionContext:
    """Inject user context into the current connection / thread.

    Postgres: ``SET LOCAL`` (valid within a transaction only -- call
    before the query).
    SQLite: stored in thread-local (referenced by query_filter).

    Args:
        graph_store: SqliteGraphStore or PostgresGraphStore.
        user_id: OAuth subject.  None means unauthenticated (RLS
            will either block all or allow all depending on policy).
        home_domain: The user's default domain.
        roles: Reserved for future extension.

    Returns:
        The ``SessionContext`` that was set (for testing/debugging).
    """
    ctx = SessionContext(
        user_id=user_id, home_domain=home_domain, roles=tuple(roles),
    )
    _THREAD_CTX.ctx = ctx

    cls_name = type(graph_store).__name__
    if "Postgres" in cls_name:
        _set_postgres_context(graph_store, ctx)
    elif "Sqlite" in cls_name:
        # sqlite only needs thread-local storage
        pass
    else:
        logger.warning("session_context_unknown_backend", extra={
            "backend_class": cls_name,
        })
    logger.debug("session_user_set", extra={
        "user_id": user_id, "domain": home_domain,
        "backend": ("postgres" if "Postgres" in cls_name
                    else "sqlite" if "Sqlite" in cls_name else "unknown"),
    })
    return ctx


def _set_postgres_context(graph_store: Any, ctx: SessionContext) -> None:
    """Postgres SET LOCAL -- read by the RLS policy via ``current_setting``.

    SET LOCAL is valid only within a transaction -- the caller must
    wrap it in a BEGIN block.
    """
    conn = getattr(graph_store, "_conn", None)
    if conn is None:
        return
    cur = conn.cursor()
    try:
        # Represent NULL by setting to empty string (clears the value)
        if ctx.user_id is None:
            cur.execute("SET LOCAL k2g.user_id = ''")
        else:
            cur.execute("SET LOCAL k2g.user_id = %s", (ctx.user_id,))
        if ctx.home_domain is None:
            cur.execute("SET LOCAL k2g.home_domain = ''")
        else:
            cur.execute(
                "SET LOCAL k2g.home_domain = %s", (ctx.home_domain,),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres SET LOCAL failed: %s", exc)
    finally:
        cur.close()


__all__ = [
    "SessionContext",
    "get_session_context",
    "clear_session_context",
    "set_session_context",
]
