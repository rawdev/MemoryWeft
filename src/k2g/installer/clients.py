"""BP-76 — MCPClientSpec adapters.

Each MCP client has a slightly different config schema:

- **claude_code / claude_desktop / cursor / gemini**: JSON,
  ``{"mcpServers": {<name>: {...}}}``
- **continue**: JSON, ``{"mcpServers": [{"name": ..., ...}]}`` (array, not map)
- **codex**: TOML (``~/.codex/config.toml``), ``[mcp_servers.<name>]`` table.
  Different container key (``mcp_servers``) *and* a different on-disk
  serialization — handled by the installer's TOML path.

The :class:`MCPClientSpec` records the slug, human label, project-scope
flag, the config container key + serialization, and an *upsert function*
that mutates an existing config dict (possibly empty) to add/remove our
entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

MCP_SERVER_KEY = "mweft"


def _upsert_map_form(
    config: dict[str, Any],
    server_block: dict[str, Any],
    server_key: str = MCP_SERVER_KEY,
    container_key: str = "mcpServers",
) -> dict[str, Any]:
    """Map-form schema: ``container_key`` is a dict keyed by server name.

    ``container_key`` is ``mcpServers`` for the JSON clients and
    ``mcp_servers`` for Codex (TOML).
    """
    servers = config.setdefault(container_key, {})
    if not isinstance(servers, dict):
        # Non-destructive guarantee: a wrong-typed container would have to be
        # rebuilt, destroying user content — refuse (installer pre-validates
        # via _container_writable, so this is a guard for other callers).
        raise ValueError(
            f"config[{container_key!r}] is not a table — refusing to overwrite user content"
        )
    servers[server_key] = server_block
    return config


def _remove_map_form(
    config: dict[str, Any],
    server_key: str = MCP_SERVER_KEY,
    container_key: str = "mcpServers",
) -> dict[str, Any]:
    servers = config.get(container_key)
    if isinstance(servers, dict):
        servers.pop(server_key, None)
    return config


def _upsert_array_form(
    config: dict[str, Any],
    server_block: dict[str, Any],
    server_key: str = MCP_SERVER_KEY,
) -> dict[str, Any]:
    """Array form: ``mcpServers`` is a list of objects with a ``name`` key."""
    servers = config.setdefault("mcpServers", [])
    if not isinstance(servers, list):
        # See _upsert_map_form: refuse rather than rebuild over user content.
        raise ValueError(
            "config['mcpServers'] is not a list — refusing to overwrite user content"
        )
    # Drop existing entry then append.
    config["mcpServers"] = [
        s for s in servers if not (isinstance(s, dict) and s.get("name") == server_key)
    ]
    config["mcpServers"].append({"name": server_key, **server_block})
    return config


def _remove_array_form(
    config: dict[str, Any],
    server_key: str = MCP_SERVER_KEY,
) -> dict[str, Any]:
    servers = config.get("mcpServers")
    if isinstance(servers, list):
        config["mcpServers"] = [
            s for s in servers if not (isinstance(s, dict) and s.get("name") == server_key)
        ]
    return config


@dataclass(frozen=True)
class MCPClientSpec:
    slug: str
    label: str
    schema: str  # "map" | "array"
    requires_project_dir: bool = False
    note: str = ""
    # On-disk serialization: "json" (default) or "toml" (Codex). The
    # installer reads/writes accordingly; "toml" clients merge a single
    # ``[<container_key>.<name>]`` block to preserve user comments.
    serialization: str = "json"
    # Top-level key holding the server map. "mcpServers" for JSON clients,
    # "mcp_servers" for Codex.
    container_key: str = "mcpServers"

    def upsert(
        self,
        config: dict[str, Any],
        server_block: dict[str, Any],
    ) -> dict[str, Any]:
        if self.schema == "array":
            return _upsert_array_form(config, server_block)
        return _upsert_map_form(config, server_block, container_key=self.container_key)

    def remove(self, config: dict[str, Any]) -> dict[str, Any]:
        if self.schema == "array":
            return _remove_array_form(config)
        return _remove_map_form(config, container_key=self.container_key)


ALL_CLIENTS: tuple[MCPClientSpec, ...] = (
    MCPClientSpec(
        slug="claude_code",
        label="Claude Code (per-project .mcp.json)",
        schema="map",
        requires_project_dir=True,
    ),
    MCPClientSpec(
        slug="claude_desktop",
        label="Claude Desktop",
        schema="map",
    ),
    MCPClientSpec(
        slug="cursor_project",
        label="Cursor (per-project .cursor/mcp.json)",
        schema="map",
        requires_project_dir=True,
    ),
    MCPClientSpec(
        slug="cursor_global",
        label="Cursor (global ~/.cursor/mcp.json)",
        schema="map",
    ),
    MCPClientSpec(
        slug="gemini_project",
        label="Gemini CLI (per-project .gemini/settings.json)",
        schema="map",
        requires_project_dir=True,
    ),
    MCPClientSpec(
        slug="gemini_global",
        label="Gemini CLI (global ~/.gemini/settings.json)",
        schema="map",
    ),
    MCPClientSpec(
        slug="codex_global",
        label="OpenAI Codex CLI (global ~/.codex/config.toml)",
        schema="map",
        serialization="toml",
        container_key="mcp_servers",
    ),
    MCPClientSpec(
        slug="codex_project",
        label="OpenAI Codex CLI (per-project .codex/config.toml)",
        schema="map",
        requires_project_dir=True,
        serialization="toml",
        container_key="mcp_servers",
    ),
    MCPClientSpec(
        slug="continue",
        label="Continue (~/.continue/config.json)",
        schema="array",
    ),
    MCPClientSpec(
        slug="chatgpt",
        label="ChatGPT Desktop",
        schema="map",
        note=(
            "ChatGPT's MCP support is still evolving (2026-05). No "
            "documented config file location yet; use the copy-paste "
            "snippet inside ChatGPT's settings when available."
        ),
    ),
)

_BY_SLUG = {c.slug: c for c in ALL_CLIENTS}


def get_client_spec(slug: str) -> MCPClientSpec | None:
    return _BY_SLUG.get(slug)


def available_clients() -> list[dict[str, Any]]:
    """UI-facing list — basic metadata for each known client."""
    return [
        {
            "slug": c.slug,
            "label": c.label,
            "schema": c.schema,
            "requires_project_dir": c.requires_project_dir,
            "note": c.note,
        }
        for c in ALL_CLIENTS
    ]
