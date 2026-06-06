"""BP-78 — Hub-side ProjectRegistry.

The hub holds a single in-memory map ``project_dir → ProjectEntry`` that
records each enrolled subprocess.  MCP write tools forward to the hub
with an ``X-Mweft-Project-Dir`` header; the hub looks up the entry and
proxies the request to that subprocess's ``/api/internal/write/{op}``.

Stale entries (PID no longer alive) are pruned on every read so a
crashed subprocess does not silently absorb future writes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from k2g.launcher_hub.discovery import is_pid_alive

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_project_dir(project_dir: str | Path) -> str:
    """Canonical form used as registry key.

    Resolved absolute path, forward-slash-normalised so that different
    string forms ("C:/x", "C:\\x", "C:/x/") all map to the same key.
    """
    p = Path(project_dir).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError:
        pass
    return str(p).replace("\\", "/").rstrip("/")


@dataclass(frozen=True)
class ProjectEntry:
    """Runtime metadata for a single subprocess registered with the hub."""

    project_dir: str        # canonicalised
    project_url: str        # e.g. "http://127.0.0.1:8765"
    pid: int
    registered_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProjectRegistry:
    """Thread-safe registry of running subprocesses.

    The registry is *advisory*: a subprocess can come and go without
    coordinating with the hub, and PID-based liveness check handles
    crashes.  All mutating methods are O(1) under a single ``RLock``.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ProjectEntry] = {}
        self._lock = threading.RLock()

    # --- mutators -------------------------------------------------------

    def register(
        self,
        *,
        project_dir: str | Path,
        project_url: str,
        pid: int,
    ) -> ProjectEntry:
        """Add or replace an entry; returns the canonicalised entry."""
        key = _normalize_project_dir(project_dir)
        entry = ProjectEntry(
            project_dir=key,
            project_url=project_url.rstrip("/"),
            pid=int(pid),
            registered_at=_now_iso(),
        )
        with self._lock:
            self._entries[key] = entry
        logger.info(
            "Registry: project %s registered → %s (pid=%d)",
            key, entry.project_url, pid,
        )
        return entry

    def unregister(self, project_dir: str | Path) -> bool:
        """Remove an entry; returns True if a row was deleted."""
        key = _normalize_project_dir(project_dir)
        with self._lock:
            removed = self._entries.pop(key, None) is not None
        if removed:
            logger.info("Registry: project %s unregistered", key)
        return removed

    def prune_stale(self) -> list[str]:
        """Drop entries whose PIDs are no longer alive; return removed keys."""
        removed: list[str] = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                if not is_pid_alive(entry.pid):
                    self._entries.pop(key, None)
                    removed.append(key)
        for key in removed:
            logger.info("Registry: pruned stale entry %s (dead pid)", key)
        return removed

    # --- accessors ------------------------------------------------------

    def get(self, project_dir: str | Path) -> ProjectEntry | None:
        """Look up an entry by project_dir (any string form accepted)."""
        key = _normalize_project_dir(project_dir)
        self.prune_stale()
        with self._lock:
            return self._entries.get(key)

    def list_entries(self) -> list[ProjectEntry]:
        """All currently-live entries (stale ones pruned first)."""
        self.prune_stale()
        with self._lock:
            return list(self._entries.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __contains__(self, project_dir: object) -> bool:
        if not isinstance(project_dir, (str, Path)):
            return False
        return self.get(project_dir) is not None


__all__ = [
    "ProjectEntry",
    "ProjectRegistry",
]
