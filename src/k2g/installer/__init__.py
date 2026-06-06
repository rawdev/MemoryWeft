"""BP-76 — Multi-client MCP installer.

Registers the ``mweft`` MCP server in the config files of various MCP
clients (Claude Code / Desktop / Cursor / Continue) so users don't have
to hand-edit JSON.  When direct file writes fail (sandbox / permission),
a copy-paste snippet is returned instead.

Public API:

- :func:`available_clients` — list of (slug, label) pairs
- :func:`preview` — dry-run; returns the JSON that *would* be written
- :func:`apply` — writes config files; returns InstallReport
"""

from __future__ import annotations

from k2g.installer.clients import (
    ALL_CLIENTS,
    MCPClientSpec,
    available_clients,
    get_client_spec,
)
from k2g.installer.installer import (
    ClientResult,
    InstallReport,
    apply,
    global_targets,
    install_conflict,
    installer_status,
    is_installed,
    preview,
    reapply_for_entry,
    read_client_mweft_env,
    uninstall,
)
from k2g.installer.locator import (
    config_path,
    detect_platform,
)

__all__ = [
    "ALL_CLIENTS",
    "ClientResult",
    "InstallReport",
    "MCPClientSpec",
    "apply",
    "available_clients",
    "config_path",
    "detect_platform",
    "get_client_spec",
    "global_targets",
    "install_conflict",
    "installer_status",
    "is_installed",
    "preview",
    "reapply_for_entry",
    "read_client_mweft_env",
    "uninstall",
]
