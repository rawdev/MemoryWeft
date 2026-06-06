"""BP-75 — MWeft launcher hub.

A small FastAPI server that runs alongside the project subprocess and
provides:

- A single BGE-M3 embedding instance shared across all MCP clients
  (Claude Code / Claude Desktop / ChatGPT / Cursor / ...), avoiding the
  N-times memory blow-up of one process per client.
- A write-forwarding endpoint that funnels every SQLite write through
  the project subprocess, side-stepping SQLite's single-writer
  contention when multiple MCP clients run simultaneously.

Public API:

- :func:`discovery.read_hub_info` / :func:`discovery.write_hub_info` —
  serialise the hub's runtime location to ``<project>/.mweft/hub.json``
  so MCP clients can discover it.
"""

from __future__ import annotations

from k2g.launcher_hub.discovery import (
    HubInfo,
    clear_global_hub_info,
    clear_hub_info,
    global_hub_info_path,
    is_pid_alive,
    project_hub_info_path,
    read_global_hub_info,
    read_hub_info,
    write_global_hub_info,
    write_hub_info,
)
from k2g.launcher_hub.registry import ProjectEntry, ProjectRegistry

__all__ = [
    "HubInfo",
    "ProjectEntry",
    "ProjectRegistry",
    "clear_global_hub_info",
    "clear_hub_info",
    "global_hub_info_path",
    "is_pid_alive",
    "project_hub_info_path",
    "read_global_hub_info",
    "read_hub_info",
    "write_global_hub_info",
    "write_hub_info",
]
