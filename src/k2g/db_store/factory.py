"""DbStore backend factory — resolves Settings provider strings to concrete implementations.

Only postgres and sqlite backends are supported (Neo4j / Kuzu / Qdrant
were removed during the reconstruct module). To add a new backend,
add a branch in each ``build_*_backend`` function.

The three provider env vars (``GRAPH_DB_PROVIDER`` /
``VECTOR_STORE_PROVIDER`` / ``CONTENT_STORE_MODE``) are unified into
a single ``_resolve_backend_mode`` function. New environments only
need ``K2G_POSTGRES_DSN`` to activate PG mode; if unset, sqlite is
the default. Legacy env vars are supported with deprecation warnings.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from k2g.core.config import Settings
    from k2g.db_store.content_backend import ContentBackend
    from k2g.db_store.graph_backend import GraphStoreProtocol
    from k2g.db_store.object_backend import ObjectBackend
    from k2g.db_store.vector_backend import VectorStoreProtocol

logger = logging.getLogger(__name__)


_BackendMode = Literal["sqlite", "postgres"]

_DSN_ENV_NAMES = ("K2G_POSTGRES_DSN", "POSTGRES_DSN")
_PROVIDER_ENV_NAMES = (
    "GRAPH_DB_PROVIDER",
    "VECTOR_STORE_PROVIDER",
    "CONTENT_STORE_MODE",
)
_DEPRECATED_ENV_NAMES = (
    *_PROVIDER_ENV_NAMES,
    "OBJECT_STORAGE_PROVIDER",
    "K2G_SQLITE_PATH",
)

_warned_deprecated = False


def _resolve_backend_mode(settings: "Settings") -> _BackendMode:
    """Determine backend mode -- explicit provider env takes priority over DSN auto-detection.

    Priority:
      1. Explicit ``GRAPH_DB_PROVIDER`` / ``VECTOR_STORE_PROVIDER`` /
         ``CONTENT_STORE_MODE`` set to ``"sqlite"`` or ``"postgres"``
         honours the user's stated intent via .mcp.json or OS env.
      2. ``K2G_POSTGRES_DSN`` / ``POSTGRES_DSN`` present -> postgres
         (auto-detection: setting a DSN implies PG usage).
      3. Otherwise -> sqlite (default).

    We query ``os.environ`` directly instead of ``settings.graph_db_provider``
    because Settings defaults to "sqlite", making it impossible to
    distinguish an explicit "sqlite" from the default.

    Priority-0 override: if ``settings.backend_mode`` is explicitly set
    ("sqlite"/"postgres"), it takes precedence over env vars. Desktop
    apps run with in-memory Settings only (no os.environ), so env
    auto-detection always falls back to sqlite -- this explicit field
    authoritatively overrides that. None (legacy env/CLI path) falls
    through to the env logic below.
    """
    # 0. Explicit Settings override (desktop in-memory path) -- takes precedence over env
    explicit = getattr(settings, "backend_mode", None)
    if explicit in ("sqlite", "postgres"):
        return explicit  # type: ignore[return-value]
    # 1. Explicit provider env (sqlite/postgres) -- user intent takes precedence
    for env_name in _PROVIDER_ENV_NAMES:
        v = os.environ.get(env_name, "").strip().lower()
        if v in ("sqlite", "postgres"):
            return v  # type: ignore[return-value]
    # 2. DSN auto-detection -- setting a DSN implies PG mode
    for env_name in _DSN_ENV_NAMES:
        if os.environ.get(env_name, "").strip():
            return "postgres"
    # 3. default
    return "sqlite"


def _warn_deprecated_envs_once() -> None:
    global _warned_deprecated
    if _warned_deprecated:
        return
    _warned_deprecated = True
    found = [n for n in _DEPRECATED_ENV_NAMES if os.environ.get(n, "").strip()]
    if not found:
        return
    logger.warning(
        "deprecated backend env vars detected: %s -- can be removed; "
        "K2G_POSTGRES_DSN (PG mode) or DATA_DIR (sqlite) is sufficient. "
        "See ",
        ", ".join(found),
    )


def build_graph_backend(settings: "Settings") -> "GraphStoreProtocol":
    _warn_deprecated_envs_once()
    mode = _resolve_backend_mode(settings)
    if mode == "sqlite":
        from k2g.db_store.sqlite.graph import SqliteGraphStore

        return SqliteGraphStore(
            path=settings.sqlite_all_in_one_path,
            embedding_dim=settings.embedding_dim,
            embedding_model=settings.embedding_model,
        )
    from k2g.db_store.postgres.graph import PostgresGraphStore

    dsn = settings.postgres_graph_dsn or getattr(settings, "postgres_dsn", None)
    if not dsn:
        raise RuntimeError(
            "PG mode requires a DSN -- set K2G_POSTGRES_DSN or postgres_graph_dsn.",
        )
    return PostgresGraphStore(
        dsn=dsn,
        embedding_dim=settings.embedding_dim,
        embedding_model=settings.embedding_model,
    )


def build_vector_backend(settings: "Settings") -> "VectorStoreProtocol":
    _warn_deprecated_envs_once()
    mode = _resolve_backend_mode(settings)
    if mode == "sqlite":
        from k2g.db_store.sqlite.vector import SqliteVectorStore

        return SqliteVectorStore(
            path=settings.sqlite_all_in_one_path,
            dim=settings.embedding_dim,
        )
    from k2g.db_store.postgres.vector import PgVectorStore

    dsn = (
        settings.postgres_vector_dsn
        or settings.postgres_graph_dsn
        or getattr(settings, "postgres_dsn", None)
    )
    if not dsn:
        raise RuntimeError(
            "PG mode requires a vector DSN -- set K2G_POSTGRES_DSN, "
            "postgres_vector_dsn, or postgres_graph_dsn.",
        )
    return PgVectorStore(dsn=dsn, dim=settings.embedding_dim)


def build_content_backend(settings: "Settings") -> "ContentBackend":
    _warn_deprecated_envs_once()
    mode = _resolve_backend_mode(settings)
    if mode == "sqlite":
        from k2g.db_store.sqlite.content import SqliteContentStore

        return SqliteContentStore(db_path=settings.sqlite_path)
    from k2g.db_store.postgres.content import PostgresContentStore

    return PostgresContentStore(dsn=settings.postgres_dsn)


def build_object_backend(settings: "Settings") -> "ObjectBackend":
    # OBJECT_STORAGE_PROVIDER env is unused -- only LocalObjectStorage supported.
    # Re-introduce env-based branching when adding S3 / GCS backends.
    from k2g.db_store.object.local import LocalObjectStorage

    return LocalObjectStorage(base_dir=settings.object_storage_dir)


__all__ = [
    "build_graph_backend",
    "build_vector_backend",
    "build_content_backend",
    "build_object_backend",
]
