"""BP-78 Step 8 — server-side directory browser endpoint.

Browser-only file dialogs cannot reveal the user's absolute file-system
path (security sandbox).  Instead, the subprocess + hub expose a small
read-only filesystem navigator that the UI can drive client-side.

Endpoints:
- ``GET /api/fs/list?path=<abs>&filter=all|dir|file`` — enumerate
- ``GET /api/fs/roots`` — list allowed top-level roots

Security:
- Single-user desktop tool (mweft-ui-project) — defaults to **full
  filesystem** (Windows: enumerate drives, Unix: ``/``). This matches
  the user's mental model of "pick any folder for my DB".
- ``K2G_FS_ROOTS`` env (``os.pathsep``-separated absolute paths) can
  restrict navigation when shared / sandboxed deployments need it.

Symlinks resolve before the allowed-roots check, so the user cannot
break out via ``..`` or aliased mount points.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)


def _enumerate_windows_drives() -> list[Path]:
    """Return assigned drive letters as ``C:\\``, ``D:\\``, … via the
    ``GetLogicalDrives`` bitmask (instant, no I/O).

    Probing each letter with ``.exists()`` blocks for *seconds* on empty
    optical / floppy / disconnected network drives, and was called on every
    request → multi-second UI lag. ``GetLogicalDrives`` is a pure bitmask
    (bit 0 = A, bit 1 = B, …) — no media access. Falls back to the slow
    ``.exists()`` probe only if ctypes is unavailable.
    """
    import string
    try:
        import ctypes
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        mask = 0
    if mask:
        drives = [
            Path(f"{letter}:\\")
            for i, letter in enumerate(string.ascii_uppercase)
            if mask & (1 << i)
        ]
        if drives:
            return drives
    # Fallback (non-Windows ctypes / failure) — slow per-letter probe.
    probed: list[Path] = []
    for letter in string.ascii_uppercase:
        p = Path(f"{letter}:\\")
        try:
            if p.exists():
                probed.append(p)
        except OSError:
            continue
    return probed or [Path.home().resolve()]


def _default_roots() -> list[Path]:
    """Whole filesystem by default — desktop tool, single user."""
    import sys
    if sys.platform == "win32":
        return _enumerate_windows_drives()
    return [Path("/")]


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("K2G_FS_ROOTS", "").strip()
    if raw:
        roots: list[Path] = []
        for piece in raw.split(os.pathsep):
            piece = piece.strip()
            if not piece:
                continue
            try:
                roots.append(Path(piece).expanduser().resolve(strict=False))
            except OSError:
                logger.warning("fs_browser: cannot resolve root %r", piece)
        if roots:
            return roots
    return _default_roots()


def _is_within_allowed_roots(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _entry_for(child: Path, kind: str) -> dict[str, Any]:
    size: int | None = None
    if kind == "file":
        try:
            size = child.stat().st_size
        except OSError:
            size = None
    return {
        "name": child.name,
        "path": str(child),
        "kind": kind,
        "size": size,
    }


router = APIRouter(tags=["fs-browser"])


@router.get("/fs/roots")
def fs_roots() -> dict[str, Any]:
    return {"roots": [str(r) for r in _allowed_roots()]}


@router.get("/fs/list")
def fs_list(
    path: str = Query("", description="Absolute path. '' = first allowed root."),
    filter: str = Query("all", description="all | dir | file"),
) -> dict[str, Any]:
    if filter not in ("all", "dir", "file"):
        raise HTTPException(
            status_code=400,
            detail=f"invalid filter {filter!r} (expected all|dir|file)",
        )

    roots = _allowed_roots()
    if not path:
        target = roots[0]
    else:
        target = Path(path).expanduser()

    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc

    if not _is_within_allowed_roots(resolved, roots):
        raise HTTPException(
            status_code=403,
            detail=(
                f"path outside allowed roots: {resolved}. "
                f"Roots: {[str(r) for r in roots]}"
            ),
        )

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"not found: {resolved}")

    # If a file was passed, list its parent so the navigator stays useful.
    if resolved.is_file():
        resolved = resolved.parent

    if not resolved.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"not a directory: {resolved}",
        )

    entries: list[dict[str, Any]] = []
    try:
        for child in sorted(
            resolved.iterdir(),
            key=lambda p: (0 if p.is_dir() else 1, p.name.lower()),
        ):
            kind = "dir" if child.is_dir() else "file"
            # Directories are ALWAYS listed so the navigator can drill in;
            # `filter` only restricts which *files* are surfaced:
            #   dir  → hide files (the user is picking a folder)
            #   file → show files to pick, plus dirs for navigation
            #   all  → show everything
            if filter == "dir" and kind == "file":
                continue
            entries.append(_entry_for(child, kind))
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail=f"permission denied: {exc}",
        ) from exc

    # Parent is reachable only when we'd stay inside an allowed root.
    parent: str | None = None
    if resolved.parent != resolved:
        candidate = resolved.parent
        if _is_within_allowed_roots(candidate, roots):
            parent = str(candidate)

    return {
        "path": str(resolved),
        "parent": parent,
        "roots": [str(r) for r in roots],
        "filter": filter,
        "entries": entries,
    }
