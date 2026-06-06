"""Backend-mode detection + hub-use policy for the MCP-side proxy layer.

Three knobs decide whether a write tool forwards through the hub:

1. **Backend mode** — SQLite vs Postgres.  PG's MVCC makes write
   delegation unnecessary, so forwarding is only enabled in SQLite mode.
2. **K2G_USE_HUB** env — explicit override:
   - ``auto`` (default): forward if ``hub.json`` is discoverable, else inline.
   - ``true``: force forward; fail if no hub is available.
   - ``false``: never forward; always inline (legacy behaviour).
3. **Hub discovery** — see :mod:`k2g.launcher_hub.discovery`.

The MCP process picks the hub URL from the first source that resolves:

1. env ``K2G_HUB_URL`` (explicit override, e.g., for tests / cross-host).
2. ``<project>/.mweft/hub.json``'s ``hub_port`` field.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

from k2g.launcher_hub.discovery import (
    HubInfo,
    read_global_hub_info,
    read_hub_info,
)

logger = logging.getLogger(__name__)

HubUseMode = Literal["auto", "true", "false"]
HUB_USE_VALUES: tuple[HubUseMode, ...] = ("auto", "true", "false")

_USE_HUB_ENV = "K2G_USE_HUB"
_HUB_URL_ENV = "K2G_HUB_URL"
_SUBPROCESS_MARKER_ENV = "K2G_SUBPROCESS_MODE"


def is_subprocess_mode() -> bool:
    """Return ``True`` when the current process is the project subprocess
    spawned by the launcher hub.

    The supervisor sets ``K2G_SUBPROCESS_MODE=true`` when spawning the
    subprocess so its own MCP write tools (if any) and embedding calls
    skip the hub-forward path — otherwise we'd loop forever (hub →
    subprocess → hub → ...).
    """
    return os.environ.get(_SUBPROCESS_MARKER_ENV, "").strip().lower() in (
        "1", "true", "yes",
    )


def is_sqlite_mode(settings: Any) -> bool:
    """Return ``True`` iff the project is configured for SQLite (not PG).

    Mirrors ``_resolve_backend_mode`` semantics: a non-empty ``postgres_dsn``
    or ``postgres_graph_dsn`` flips to PG mode.
    """
    pg_dsn = getattr(settings, "postgres_dsn", None) or ""
    pg_graph_dsn = getattr(settings, "postgres_graph_dsn", None) or ""
    graph_provider = getattr(settings, "graph_db_provider", "sqlite")

    if graph_provider == "postgres":
        return False
    if str(pg_dsn).strip() or str(pg_graph_dsn).strip():
        return False
    return True


def get_hub_use_mode() -> HubUseMode:
    """Read the ``K2G_USE_HUB`` env knob, falling back to ``auto``."""
    raw = os.environ.get(_USE_HUB_ENV, "auto").strip().lower()
    if raw in HUB_USE_VALUES:
        return raw  # type: ignore[return-value]
    logger.warning(
        "Invalid %s=%r — expected one of %s. Falling back to 'auto'.",
        _USE_HUB_ENV, raw, HUB_USE_VALUES,
    )
    return "auto"


def get_hub_url(project_dir: str | Path | None = None) -> str | None:
    """Resolve the hub base URL.

    Lookup order (first hit wins):

    1. ``K2G_HUB_URL`` env — explicit override (tests, cross-host, advanced).
    2. ``~/.mweft/hub.json`` — BP-78 global single-hub discovery.
    3. ``<project>/.mweft/hub.json`` — BP-75 per-project discovery (legacy
       fallback when ``project_dir`` is provided).

    Returns ``None`` when no hub is discoverable.  Callers should combine
    this with :func:`get_hub_use_mode` to decide whether the absence is a
    fail-loud condition (``true``) or a fall-through to inline mode
    (``auto`` / ``false``).
    """
    env_url = os.environ.get(_HUB_URL_ENV, "").strip()
    if env_url:
        return env_url.rstrip("/")

    info = read_global_hub_info()
    if info is not None:
        return info.base_url()

    if project_dir is None:
        return None
    info = read_hub_info(project_dir)
    if info is None:
        return None
    return info.base_url()


def discover_hub_info(project_dir: str | Path | None = None) -> HubInfo | None:
    """Return the raw hub info — global first, then per-project fallback.

    Useful when the caller needs the token; the URL alone can be obtained
    via :func:`get_hub_url`.
    """
    info = read_global_hub_info()
    if info is not None:
        return info
    if project_dir is None:
        return None
    return read_hub_info(project_dir)
