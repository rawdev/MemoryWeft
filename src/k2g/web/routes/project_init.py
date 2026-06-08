"""Project setup — captures project config into ``mweft_manager.json``.

Option A: the central registry (``~/.mweft/mweft_manager.json``) is the single
source of truth. This route writes a project's config (group / domain /
search_targets / save_tags / backend / DSN / DB folder / embedding) straight
into its registry entry — there is no per-project ``project.yaml`` anymore.

Separated from the MCP installer (BP-76): the installer registers the mweft
server in MCP client configs; this route captures the project config. Both read
the same registry entry.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from k2g.ui.project_config import resolve_embedding_dim
from k2g.ui.project_registry import (
    add_project,
    default_data_dir,
    entry_env_dict,
    find_by_dir,
    get_last_active,
    get_project,
    list_projects,
    registry_path,
    remove_project,
    set_last_active,
    touch_project,
    update_project,
)
from k2g.web.deps import sanitize

logger = logging.getLogger(__name__)

router = APIRouter(tags=["project"])

# Project-derived env vars (from ProjectConfig.to_env_dict) — stripped before
# spawning a server for *another* project so its own project.yaml repopulates
# them (the spawned cli's _apply_env uses setdefault → inherited values would
# otherwise win and point the new server at THIS project's DB).
_PROJECT_ENV_KEYS = (
    "K2G_PROJECT_DIR", "K2G_PROJECT_SLUG", "DATA_DIR", "K2G_POSTGRES_DSN",
    "K2G_USER_MEMORY_SAVE_GROUP", "K2G_USER_MEMORY_SAVE_DOMAIN",
    "K2G_USER_MEMORY_SAVE_TAGS", "K2G_USER_SEARCH_TARGETS", "K2G_DOTENV_FILE",
    "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIM",
)

# Light env keys safe to hot-reload in place (no live DB/embedding rebind):
# they only steer save/search behaviour, read through the cached Settings.
# Heavy keys (DATA_DIR / K2G_POSTGRES_DSN / EMBEDDING_*) bind to _stores built
# at startup() and require a full server relaunch — NOT in this list.
_LIGHT_ENV_KEYS = (
    "K2G_USER_MEMORY_SAVE_GROUP", "K2G_USER_MEMORY_SAVE_DOMAIN",
    "K2G_USER_MEMORY_SAVE_TAGS", "K2G_USER_SEARCH_TARGETS",
)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class BackendSqlite(BaseModel):
    kind: Literal["sqlite"]
    data_dir: str = Field(
        ..., description="Absolute directory where SQLite DBs live (graph.db etc).",
    )


class BackendPostgres(BaseModel):
    kind: Literal["postgres"]
    dsn: str = Field(
        ..., description="postgresql://user:pass@host:port/db",
    )


class EmbeddingConfigIn(BaseModel):
    provider: str = "local"
    model: str = "BAAI/bge-m3"
    dim: int | None = None      # None → resolve from model


class ProjectInitRequest(BaseModel):
    slug: str | None = Field(
        default=None, description="Existing entry to update (name-only registration).",
    )
    project_dir: str = Field(
        default="", description="Project anchor (postgres case / legacy lookup).",
    )
    group: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    search_targets: list[dict[str, str]] = Field(default_factory=list)
    save_tags: list[str] = Field(default_factory=list)
    backend: BackendSqlite | BackendPostgres
    embedding: EmbeddingConfigIn = Field(default_factory=EmbeddingConfigIn)
    force: bool = Field(default=False, description="Unused (kept for compat).")


@router.get("/project/config")
def get_project_config(project_dir: str = "", slug: str = "") -> dict[str, Any]:
    """Pre-populate the setup form from the project's registry entry.

    Resolve by ``slug`` (name-only registration) or by ``project_dir``.
    """
    entry = get_project(slug) if slug else (find_by_dir(project_dir) if project_dir else None)
    configured = entry is not None and bool(entry.db_dir or entry.domain or entry.group)
    if not configured:
        return sanitize({
            "initialized": False,
            "project_dir": (entry.project_dir if entry else str(Path(project_dir)) if project_dir else ""),
            "data_dir": default_data_dir(entry.slug if entry else (slug or "default")),
            "name": entry.name if entry else None,
            "path": str(registry_path()),
            "ai_clients": list(entry.ai_clients or []) if entry else [],
            # The deployment's effective embedding provider (portable bundle sets
            # onnx). The setup form defaults to THIS, not a hard-coded "local" —
            # saving "local" on an onnx-only bundle makes the spawned MCP crash
            # ("sentence-transformers required").
            "deployment_provider": os.environ.get("EMBEDDING_PROVIDER") or "local",
        })
    is_pg = (entry.backend == "postgres") or bool(entry.postgres_dsn)
    emb = entry.embedding or {}
    # Report the EFFECTIVE model: when the entry leaves it unset the runtime
    # falls back to the deployment env (the portable bundle sets EMBEDDING_*),
    # so the setup form/onboarding must round-trip that, not a hard-coded value.
    model = str(emb.get("model") or os.environ.get("EMBEDDING_MODEL") or "BAAI/bge-m3")
    return sanitize({
        "initialized": True,
        "deployment_provider": os.environ.get("EMBEDDING_PROVIDER") or "local",
        "project_dir": entry.project_dir,
        # Effective local anchor (object storage + logs). For Postgres the entry
        # may carry none, so suggest a per-project default the onboarding pre-fills.
        "data_dir": entry.db_dir or entry.project_dir or default_data_dir(entry.slug),
        "path": str(registry_path()),
        "group": entry.group or "default",
        "domain": entry.domain or "default",
        "search_targets": entry.search_targets or [{"domain": entry.domain or "default"}],
        "save_tags": list(entry.save_tags or []),
        "backend": (
            {"kind": "postgres", "dsn": entry.postgres_dsn}
            if is_pg else
            {"kind": "sqlite", "data_dir": entry.db_dir or entry.project_dir}
        ),
        "embedding": {
            # EFFECTIVE provider: an unset entry falls back to the env provider
            # (onnx in the portable bundle). Reporting 'local' here would make a
            # save persist 'local' and defeat that fallback — re-breaking onnx.
            "provider": str(emb.get("provider") or os.environ.get("EMBEDDING_PROVIDER") or "local"),
            "model": model,
            "dim": emb.get("dim") or resolve_embedding_dim(model),
        },
        # The project's user-managed AI-client list. The setup panel reads this
        # to render the installed AIs (it no longer disk-scans + overwrites). The
        # list is mutated only by the panel's add/remove actions (PUT
        # /projects/{slug}/ai-clients), never as a side effect of apply/uninstall.
        "ai_clients": list(entry.ai_clients or []),
    })


def _same_path(a: str | None, b: str | None) -> bool:
    """Case/sep-insensitive path equality (Windows-safe). None-tolerant."""
    if not a or not b:
        return a == b
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _db_locked() -> None:
    """Reject a DB-location change on an already-initialised project."""
    raise HTTPException(
        status_code=409,
        detail=(
            "DB location is locked for an initialised project. To use a "
            "different database, register a new project."
        ),
    )


@router.post("/project/init")
def init_project(
    body: ProjectInitRequest = Body(...),
) -> dict[str, Any]:
    """Persist the project's config into its ``mweft_manager.json`` entry.

    Option A: the registry entry is the single source of truth (no per-project
    project.yaml). ``db_dir`` = the DB data folder (= DATA_DIR for sqlite);
    ``project_dir`` = the IDE working folder where per-project MCP configs go.
    They are distinct; ``project_dir`` defaults to ``db_dir`` when not given.
    """
    proj_dir = str(Path(body.project_dir).expanduser().resolve()) if body.project_dir else ""
    if body.backend.kind == "sqlite":
        # The DB folder = data anchor: resolve to absolute + create it.
        db_dir = str(Path(body.backend.data_dir).expanduser().resolve())
        backend_kind = "sqlite"
        postgres_dsn: str | None = None
        try:
            Path(db_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"data_dir not creatable: {exc}") from exc
    else:
        dsn = body.backend.dsn
        if not dsn.startswith(("postgres://", "postgresql://")):
            raise HTTPException(
                status_code=400, detail="postgres dsn must start with 'postgresql://'")
        # Postgres has no data folder — anchor object storage/logs on
        # project_dir. Preserve the existing local anchor when none is supplied,
        # so a re-save (e.g. the first-run onboarding) doesn't empty it and make
        # object storage resolve to the drive root.
        _ex = get_project(body.slug) if body.slug else None
        db_dir = proj_dir or ((_ex.db_dir or _ex.project_dir) if _ex else "")
        backend_kind = "postgres"
        postgres_dsn = dsn

    # Resolve which registry entry to write: an explicit slug (name-only
    # registration → setup picks its DB folder) wins; else dedup by db_dir; else
    # create a fresh entry.
    existing = get_project(body.slug) if body.slug else None
    if existing is None and db_dir:
        existing = find_by_dir(db_dir)
    if existing is not None:
        slug = existing.slug
    else:
        created = add_project(
            name=(Path(db_dir).name or db_dir) if db_dir else body.domain,
            project_dir=proj_dir or db_dir, db_dir=db_dir,
        )
        slug = created.slug

    # DB-location lock: an already-initialised project (``backend`` set) may not
    # have its backend / data_dir / DSN re-pointed. Silently switching a live
    # project to a different (often empty, un-initialised) database breaks it —
    # to use a different DB, register a new project. Non-location fields
    # (domain / group / tags / embedding) stay editable while the DB is unchanged.
    if existing is not None and existing.backend:
        if existing.backend != backend_kind:
            _db_locked()
        elif backend_kind == "sqlite":
            if not _same_path(existing.db_dir, db_dir):
                _db_locked()
        elif (existing.postgres_dsn or "") != (postgres_dsn or ""):
            _db_locked()

    # Data-loss guard: the form has no search_targets editor. Preserve existing
    # targets when none sent; else default to [{domain}].
    search_targets = body.search_targets
    if not search_targets and existing and existing.search_targets:
        search_targets = existing.search_targets
    if not search_targets:
        search_targets = [{"domain": body.domain}]

    try:
        update_project(
            slug=slug,
            project_dir=(proj_dir or (existing.project_dir if existing else "") or db_dir),
            db_dir=db_dir,
            group=body.group, domain=body.domain,
            search_targets=search_targets,
            save_tags=list(body.save_tags or []),
            backend=backend_kind, postgres_dsn=postgres_dsn,
            embedding={
                "provider": body.embedding.provider,
                "model": body.embedding.model,
                "dim": body.embedding.dim,
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"registry write failed: {exc}") from exc

    saved = get_project(slug)
    if saved is None:
        raise HTTPException(status_code=500, detail="entry vanished after write")
    return sanitize({
        "ok": True,
        "slug": slug,
        "path": str(registry_path()),
        "env_preview": entry_env_dict(saved),
    })


# --- User-level project registry (~/.mweft/projects.json) ---------------


class RegisterProjectIn(BaseModel):
    name: str = Field(..., min_length=1)
    project_dir: str = Field(default="", description="Optional — DB folder is set in setup.")
    slug: str | None = Field(default=None)


def _current_project_dir() -> str | None:
    """Best-effort resolution of the project_dir this UI is bound to."""
    env_dir = os.environ.get("K2G_PROJECT_DIR", "").strip()
    return env_dir or None


def _current_entry():
    """The registry entry this UI is bound to.

    Prefer ``K2G_PROJECT_SLUG`` (set by the spawned server) so the lookup is
    unambiguous when several entries share one folder; fall back to folder-based
    resolution for servers started without a slug.
    """
    slug = os.environ.get("K2G_PROJECT_SLUG", "").strip()
    if slug:
        e = get_project(slug)
        if e is not None:
            return e
    current = _current_project_dir()
    return find_by_dir(current) if current else None


@router.get("/projects")
def list_known_projects() -> dict[str, Any]:
    """Return the user-level project registry + which entry is currently active."""
    entries = list_projects()
    current = _current_project_dir()
    current_entry = _current_entry()
    current_slug = current_entry.slug if current_entry else None
    return sanitize({
        "registry_path": str(registry_path()),
        "current_project_dir": current,
        "current_slug": current_slug,
        "last_active_slug": get_last_active(),
        "projects": [e.to_dict() for e in entries],
    })


@router.post("/projects")
def add_known_project(body: RegisterProjectIn = Body(...)) -> dict[str, Any]:
    """Register a project in mweft_manager.json.

    Option A: registration captures only a display name; the DB folder (anchor)
    is chosen later in the setup panel (``/project/init``). A ``project_dir`` may
    still be supplied to register an existing folder directly.
    """
    if not body.project_dir.strip():
        from k2g.ui.project_registry import add_named_project
        entry = add_named_project(name=body.name, slug=body.slug)
        return sanitize({"ok": True, "entry": entry.to_dict()})
    # Explicit folder given → resolve to absolute (a relative input like
    # "MWEFTK2G" would otherwise be re-joined to project_dir → doubling).
    pd = Path(body.project_dir).expanduser().resolve()
    if pd.exists() and not pd.is_dir():
        raise HTTPException(
            status_code=400, detail=f"not a directory: {pd}")
    try:
        pd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail=f"cannot create folder {pd}: {exc}") from exc
    entry = add_project(
        name=body.name, project_dir=str(pd), slug=body.slug,
        db_dir=str(pd),
    )
    return sanitize({"ok": True, "entry": entry.to_dict()})


@router.delete("/projects/{slug}")
def delete_known_project(slug: str) -> dict[str, Any]:
    """Remove a project from the user-level registry. Does NOT delete files."""
    ok = remove_project(slug)
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown slug: {slug}")
    return {"ok": True, "slug": slug}


@router.get("/projects/current")
def get_current_project() -> dict[str, Any]:
    """Return info about the project this UI subprocess is bound to."""
    current = _current_project_dir()
    if not current:
        return sanitize({
            "initialized": False,
            "project_dir": None,
            "note": "K2G_PROJECT_DIR is not set",
        })
    pd = Path(current)
    entry = _current_entry()
    out: dict[str, Any] = {
        "project_dir": str(pd),
        "initialized": entry is not None,
    }
    if entry is not None:
        out["name"] = entry.name
        out["group"] = entry.group or "default"
        out["domain"] = entry.domain or "default"
        out["backend"] = "postgres" if (entry.backend == "postgres" or entry.postgres_dsn) else "sqlite"
        try:
            touch_project(entry.project_dir)
        except Exception:  # noqa: BLE001
            pass
    return sanitize(out)


class ActivateProjectIn(BaseModel):
    slug: str = Field(..., min_length=1)


def _health_ok(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


@router.post("/projects/activate")
def activate_project(body: ActivateProjectIn = Body(...)) -> dict[str, Any]:
    """Switch the active project by **relaunching** a UI server for it.

    The active subprocess can't safely re-point its own stores at runtime, so
    activation spawns a fresh ``mweft-ui-project --project-dir <db_dir>`` (the
    DB folder = anchor), waits for ``/health``, and returns its URL for the
    browser to redirect to. Once the new server is healthy, THIS server bows out
    (graceful self-shutdown) so switching projects doesn't pile up one process
    per click — a single active server at a time. Sets ``last_active_slug`` so a
    bare relaunch reopens this project.
    """
    entry = get_project(body.slug)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown slug: {body.slug}")

    # Desktop app: switch IN-PROCESS (swap the active Settings) instead of
    # spawning a new per-project server. The desktop launcher registers
    # ``deps.desktop_rebind``; when it's absent (web browser) we fall through to
    # the spawn + redirect path below. Returning ``spawned: False`` tells the
    # frontend to soft-refresh in place rather than navigate to a new server.
    from k2g.web import deps
    if deps.desktop_rebind is not None:
        try:
            url = deps.desktop_rebind(body.slug)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return sanitize({"ok": True, "url": url, "slug": body.slug, "spawned": False})

    db_dir = entry.db_dir or entry.project_dir
    if not db_dir:
        raise HTTPException(
            status_code=409,
            detail=f"project '{body.slug}' has no DB folder yet — open its setup and save first.",
        )
    pd = Path(db_dir)
    set_last_active(body.slug)

    # Activation is Manager-local: it switches which project the Manager UI loads
    # and does NOT touch external (global) AI client configs (Claude Desktop, …).
    # Global clients have a single fixed config slot, so re-applying ai_clients
    # here would silently *repoint* a global client at whatever project was last
    # activated — hijacking a binding the user set deliberately. Global configs now
    # change ONLY via an explicit "Apply All MCP" / install action.

    port = _pick_free_port()
    # Pass --slug so the spawned server loads THIS entry's config even when
    # another entry shares the same folder (folder lookup alone is ambiguous and
    # would load the wrong DB — e.g. a sqlite sibling instead of this postgres one).
    cmd = [sys.executable, "-m", "k2g.ui_project.cli",
           "--slug", body.slug, "--project-dir", str(pd), "--port", str(port)]
    # Strip this server's project-derived env so the target's project.yaml
    # repopulates them (cli._apply_env uses setdefault).
    env = {k: v for k, v in os.environ.items() if k not in _PROJECT_ENV_KEYS}
    popen_kwargs: dict[str, Any] = {"env": env}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **popen_kwargs)  # noqa: S603 — detached UI server
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"spawn failed: {exc}") from exc

    url = f"http://127.0.0.1:{port}"
    base = {"ok": True, "url": url, "slug": body.slug, "spawned": True}
    for _ in range(40):  # ~20s
        if _health_ok(url):
            # New server is up → this (old) server steps down after the response
            # reaches the browser and it redirects. Only on confirmed health, so
            # a failed spawn never leaves the user with no server.
            _self_shutdown_after_activate()
            return sanitize(base)
        time.sleep(0.5)
    # Bound but not yet healthy — keep this server alive as a fallback; return
    # the URL anyway and let the browser retry.
    return sanitize({**base, "note": "server starting — page may take a moment"})


def _self_shutdown_after_activate() -> None:
    """Best-effort graceful shutdown of the server handling this activation.

    No-op outside the per-project UI server (TestClient / standalone web app
    have no registered uvicorn Server)."""
    try:
        from k2g.ui_project.cli import request_self_shutdown
        request_self_shutdown(delay=3.0)
    except Exception as exc:  # noqa: BLE001 — never block activation on this
        logger.warning("activate: self-shutdown schedule failed: %s", exc)


class ReloadConfigIn(BaseModel):
    project_dir: str = Field(..., min_length=1)


@router.post("/project/reload")
def reload_project_config(body: ReloadConfigIn = Body(...)) -> dict[str, Any]:
    """Hot-reload *light* settings (domain / save_tags / search_targets / group)
    without a restart.

    The running server loaded its env once at boot into a cached Settings
    singleton; heavy values (DATA_DIR / DSN / embedding) bind to live stores and
    can't be re-pointed in place. This re-reads the registry entry and
    force-overwrites only the light env keys, then clears the Settings cache so
    the next request sees the new domain/tags. Refuses unless this server is
    bound to the project (so we never mutate another project's runtime).
    """
    pd = Path(body.project_dir)
    current = os.environ.get("K2G_PROJECT_DIR", "").strip()
    if not current or Path(current).resolve() != pd.resolve():
        raise HTTPException(
            status_code=409,
            detail="not the active project for this server — restart instead.",
        )
    entry = find_by_dir(pd)
    if entry is None:
        raise HTTPException(status_code=409, detail=f"{pd}: no mweft_manager.json entry")
    env = entry_env_dict(entry)

    reloaded: dict[str, str] = {}
    for k in _LIGHT_ENV_KEYS:
        if k in env:
            os.environ[k] = env[k]   # force overwrite — boot used setdefault
            reloaded[k] = env[k]
    # Drop the cached Settings so consumers re-read the updated env.
    try:
        from k2g.web.deps import get_settings
        get_settings.cache_clear()
    except Exception as exc:  # noqa: BLE001 — cache clear is best-effort
        logger.warning("settings cache_clear failed: %s", exc)
    return sanitize({"ok": True, "reloaded": reloaded})


class AiClientsIn(BaseModel):
    ai_clients: list[dict[str, Any]] = Field(default_factory=list)


@router.put("/projects/{slug}/ai-clients")
def set_project_ai_clients(slug: str, body: AiClientsIn = Body(...)) -> dict[str, Any]:
    """Persist the project's installed-AI list [{slug, scope, config_path}]."""
    ok = update_project(slug=slug, ai_clients=body.ai_clients)
    if not ok:
        raise HTTPException(status_code=404, detail=f"unknown slug: {slug}")
    return {"ok": True, "slug": slug, "count": len(body.ai_clients)}
