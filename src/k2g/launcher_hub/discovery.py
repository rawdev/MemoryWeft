"""Hub discovery — atomic ``<project>/.mweft/hub.json`` read/write.

MCP clients (running in separate processes) consult this file to learn the
hub's port and decide whether to forward writes / embedding calls.  Stale
files (PID no longer alive) are treated as absent.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HUB_FILENAME = "hub.json"
_PROJECT_SUBDIR = ".mweft"
_GLOBAL_SUBDIR = ".mweft"  # under user home


def global_hub_info_path() -> Path:
    """``~/.mweft/hub.json`` — BP-78 global single-hub discovery."""
    return Path.home() / _GLOBAL_SUBDIR / _HUB_FILENAME


@dataclass(frozen=True)
class HubInfo:
    """Runtime location of a running hub process.

    Written by the hub at startup, read by MCP clients to discover where
    to send embedding and write-forwarding requests.
    """

    hub_port: int
    project_port: int
    pid: int
    started_at: str
    token: str | None = None

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.hub_port}"

    @classmethod
    def now(
        cls,
        *,
        hub_port: int,
        project_port: int,
        pid: int | None = None,
        token: str | None = None,
    ) -> HubInfo:
        return cls(
            hub_port=hub_port,
            project_port=project_port,
            pid=pid if pid is not None else os.getpid(),
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            token=token,
        )


def project_hub_info_path(project_dir: str | Path) -> Path:
    """Return the path where the hub info would live for a given project."""
    return Path(project_dir) / _PROJECT_SUBDIR / _HUB_FILENAME


def _write_hub_info_to_path(target: Path, info: HubInfo) -> Path:
    """Atomically write hub info to an arbitrary path (internal helper)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), prefix=".tmp-hub-", suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, target)
        logger.debug("Wrote hub info: %s", target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def _read_hub_info_from_path(
    path: Path,
    *,
    require_alive: bool,
) -> HubInfo | None:
    if not path.is_file():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Corrupt hub info at %s: %s", path, exc)
        return None
    try:
        info = HubInfo(
            hub_port=int(data["hub_port"]),
            project_port=int(data["project_port"]),
            pid=int(data["pid"]),
            started_at=str(data["started_at"]),
            token=data.get("token"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Invalid hub info schema at %s: %s", path, exc)
        return None
    if require_alive and not is_pid_alive(info.pid):
        logger.debug("Hub info at %s is stale (pid %d not alive)", path, info.pid)
        return None
    return info


def write_hub_info(project_dir: str | Path, info: HubInfo) -> Path:
    """Atomically write hub info to ``<project>/.mweft/hub.json``."""
    return _write_hub_info_to_path(project_hub_info_path(project_dir), info)


def read_hub_info(
    project_dir: str | Path,
    *,
    require_alive: bool = True,
) -> HubInfo | None:
    """Read hub info, returning ``None`` if missing / corrupt / stale.

    A file is considered *stale* when its recorded ``pid`` is no longer
    alive — typical case is an mweft-ui process that crashed without
    cleaning up its hub.json.  Pass ``require_alive=False`` to skip the
    liveness check (useful for diagnostic inspection).
    """
    return _read_hub_info_from_path(
        project_hub_info_path(project_dir),
        require_alive=require_alive,
    )


def clear_hub_info(project_dir: str | Path) -> bool:
    """Remove the hub info file if present.  Returns ``True`` if a file was
    deleted, ``False`` if it was already absent."""
    path = project_hub_info_path(project_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# --- BP-78 global (single-hub) variants ---------------------------------

def write_global_hub_info(info: HubInfo) -> Path:
    """Atomically write to ``~/.mweft/hub.json`` (BP-78 single-hub)."""
    return _write_hub_info_to_path(global_hub_info_path(), info)


def read_global_hub_info(*, require_alive: bool = True) -> HubInfo | None:
    """Read ``~/.mweft/hub.json`` with the same stale handling as per-project."""
    return _read_hub_info_from_path(
        global_hub_info_path(),
        require_alive=require_alive,
    )


def clear_global_hub_info() -> bool:
    """Remove ``~/.mweft/hub.json`` if present."""
    try:
        global_hub_info_path().unlink()
        return True
    except FileNotFoundError:
        return False


def is_pid_alive(pid: int) -> bool:
    """Cross-platform pid liveness check (Windows + POSIX)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)
    return _is_pid_alive_posix(pid)


def _is_pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we cannot signal it (different user).
        return True
    except OSError:
        return False
    return True


def _is_pid_alive_windows(pid: int) -> bool:
    # Avoid an outright dependency on psutil; fall back to ctypes OpenProcess.
    import ctypes  # local import — Windows path only
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
    )
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        if not ok:
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
