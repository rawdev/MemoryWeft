"""BP-75 / BP-77 — MCP-side proxy layer.

In SQLite mode, MCP write tools forward to the launcher hub instead of
writing the DB directly.  Read tools still hit SQLite directly (WAL
permits concurrent readers), but their embedding calls go through the
hub's :class:`ProxyEmbeddingClient` so the BGE-M3 weights live in a
single process.
"""

from __future__ import annotations

from k2g.mcp.proxy.mode import (
    HUB_USE_VALUES,
    HubUseMode,
    get_hub_url,
    get_hub_use_mode,
    is_sqlite_mode,
)

__all__ = [
    "HUB_USE_VALUES",
    "HubUseMode",
    "get_hub_url",
    "get_hub_use_mode",
    "is_sqlite_mode",
]
