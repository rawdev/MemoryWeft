"""BP-76 — OS × MCP-client config path locator.

Resolves the on-disk location of each MCP client's config file.  The
matrix below comes from each client's published docs (2026-05).

Returns ``None`` when a client has no known config location on the
current OS — the UI then falls back to copy-paste guidance.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

Platform = Literal["windows", "macos", "linux", "unknown"]


def detect_platform() -> Platform:
    """Return a coarse OS family identifier."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def _home() -> Path:
    """Cross-platform home directory."""
    return Path(os.path.expanduser("~"))


def _appdata() -> Path | None:
    """Windows ``%APPDATA%`` — typically ``C:\\Users\\<u>\\AppData\\Roaming``."""
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata)
    return None


def config_path(
    client_slug: str,
    *,
    project_dir: str | Path | None = None,
    platform: Platform | None = None,
) -> Path | None:
    """Return the config-file path for a given MCP client + scope.

    Args:
        client_slug: ``"claude_code"`` / ``"claude_desktop"`` /
            ``"cursor_global"`` / ``"cursor_project"`` /
            ``"codex_global"`` / ``"codex_project"`` / ``"continue"``.
            ``"chatgpt"`` returns ``None`` (no documented MCP config
            location as of 2026-05).
        project_dir: Required for ``claude_code``, ``cursor_project``
            and ``codex_project`` (per-project ``.mcp.json`` /
            ``.cursor/mcp.json`` / ``.codex/config.toml``).
        platform: OS family; default = autodetect.

    Returns:
        Absolute path, or ``None`` if the slug + OS has no known location.
    """
    plat = platform or detect_platform()

    if client_slug == "claude_code":
        if project_dir is None:
            return None
        return Path(project_dir) / ".mcp.json"

    if client_slug == "claude_desktop":
        if plat == "windows":
            ad = _appdata()
            if ad is None:
                return None
            return ad / "Claude" / "claude_desktop_config.json"
        if plat == "macos":
            return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if plat == "linux":
            return _home() / ".config" / "Claude" / "claude_desktop_config.json"
        return None

    if client_slug == "cursor_global":
        return _home() / ".cursor" / "mcp.json"

    if client_slug == "cursor_project":
        if project_dir is None:
            return None
        return Path(project_dir) / ".cursor" / "mcp.json"

    if client_slug == "gemini_global":
        return _home() / ".gemini" / "settings.json"

    if client_slug == "gemini_project":
        if project_dir is None:
            return None
        return Path(project_dir) / ".gemini" / "settings.json"

    if client_slug == "codex_global":
        return _home() / ".codex" / "config.toml"

    if client_slug == "codex_project":
        if project_dir is None:
            return None
        return Path(project_dir) / ".codex" / "config.toml"

    if client_slug == "continue":
        return _home() / ".continue" / "config.json"

    if client_slug == "chatgpt":
        # MCP support is still evolving (2026-05) — no documented config path.
        return None

    return None
