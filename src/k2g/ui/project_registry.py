"""User-level project registry — known MWeft projects on this machine.

Stores a JSON object at ``~/.mweft/mweft_manager.json`` (same dir as the hub
discovery file). It is the Manager's source of truth: the **list of project
definitions** plus the **last-active** project. Each entry is the full
definition — name, main domain, mweft DB folder (the project *anchor*),
save_tags, backend, embedding, and the per-AI MCP installs.

Format (single object — top-level ``last_active_slug`` + ``projects`` array):

    {
      "version": 2,
      "last_active_slug": "personal",
      "projects": [
        {"slug": "personal", "name": "Personal Memory",
         "project_dir": "~/.mweft/personal", "db_dir": "~/.mweft/personal",
         "domain": "ai_memory", "save_tags": ["org:acme"],
         "backend": "sqlite", "postgres_dsn": null,
         "embedding": {"provider": "local", "model": "BAAI/bge-m3", "dim": 1024},
         "ai_clients": [{"slug": "claude_code", "scope": "project",
                         "config_path": "~/.mweft/personal/.mcp.json"}],
         "last_used": "...", "created_at": "..."}
      ]
    }

``db_dir == project_dir`` — the DB folder *is* the project home
(``<db_dir>/.mweft/project.yaml`` is the generated per-project artifact).

A missing file migrates once from the legacy ``projects.json`` (v1, thin
slug/name/project_dir) if present, **without writing** — the migrated view is
returned in memory and persisted only on the first mutation. The legacy file
is never deleted (kept as a backup).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REGISTRY_DIR_NAME = ".mweft"
REGISTRY_FILE_NAME = "mweft_manager.json"
LEGACY_FILE_NAME = "projects.json"
REGISTRY_VERSION = 2


def registry_path() -> Path:
    """Resolve ``~/.mweft/mweft_manager.json``. Same dir as ``hub.json``."""
    return Path.home() / REGISTRY_DIR_NAME / REGISTRY_FILE_NAME


def legacy_registry_path() -> Path:
    """Legacy v1 registry ``~/.mweft/projects.json`` (migrated-from)."""
    return Path.home() / REGISTRY_DIR_NAME / LEGACY_FILE_NAME


@dataclass
class ProjectEntry:
    slug: str
    name: str
    project_dir: str
    db_dir: str | None = None                       # == project_dir (anchor / DATA_DIR)
    domain: str | None = None
    group: str | None = None
    search_targets: list[dict[str, Any]] | None = None
    save_tags: list[str] = field(default_factory=list)
    backend: str | None = None                      # "sqlite" | "postgres"
    postgres_dsn: str | None = None
    embedding: dict[str, Any] | None = None
    ai_clients: list[dict[str, Any]] = field(default_factory=list)
    last_used: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d.get("db_dir"):
            d["db_dir"] = d["project_dir"]
        return d


# Enriched fields that update_project / add_project accept (besides identity).
_ENRICHED_FIELDS = (
    "project_dir", "db_dir", "domain", "group", "search_targets", "save_tags",
    "backend", "postgres_dsn", "embedding", "ai_clients", "name",
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slugify(text: str) -> str:
    """Lowercase, [a-z0-9-_] only. Empty → ``project``."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", str(text).lower()).strip("-_")
    return cleaned or "project"


def _ensure_unique_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def _empty() -> dict[str, Any]:
    return {"version": REGISTRY_VERSION, "last_active_slug": None, "projects": []}


def _migrate_legacy(legacy: Path) -> dict[str, Any]:
    """Read legacy v1 projects.json → v2 in-memory view (no write)."""
    try:
        old = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    items = old.get("projects") if isinstance(old, dict) else None
    out: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict) or "project_dir" not in item:
            continue
        pd = str(item["project_dir"])
        slug = str(item.get("slug") or _slugify(item.get("name") or "project"))
        out.append({
            "slug": slug,
            "name": str(item.get("name") or slug),
            "project_dir": pd,
            "db_dir": pd,
            "domain": None,
            "save_tags": [],
            "backend": None,
            "postgres_dsn": None,
            "embedding": None,
            "ai_clients": [],
            "last_used": item.get("last_used"),
            "created_at": item.get("created_at"),
        })
    return {"version": REGISTRY_VERSION, "last_active_slug": None, "projects": out}


def _enrich_item_from_yaml(item: dict[str, Any]) -> dict[str, Any]:
    """Backfill an entry from ``<db_dir>/.mweft/project.yaml`` (Option A migration).

    Best-effort, in-memory only (persisted on first mutation). Skips entries that
    already carry registry-native config (``group``/``search_targets`` set), so
    once migrated it does no further file I/O. Aligns ``db_dir`` to the yaml's
    ``data_dir`` (the real DATA location = the project anchor).
    """
    if not isinstance(item, dict):
        return item
    if item.get("group") is not None or item.get("search_targets") is not None:
        return item
    anchor = item.get("db_dir") or item.get("project_dir")
    if not anchor:
        return item
    try:
        from k2g.ui.project_config import load_project_config, project_config_path
        if not project_config_path(Path(anchor)).is_file():
            return item
        cfg = load_project_config(Path(anchor))
    except Exception:  # noqa: BLE001 — migration is best-effort
        return item
    item["group"] = cfg.group
    item["search_targets"] = list(cfg.search_targets or [])
    if item.get("domain") is None:
        item["domain"] = cfg.domain
    if not item.get("save_tags"):
        item["save_tags"] = list(cfg.save_tags or [])
    if item.get("backend") is None:
        item["backend"] = "postgres" if cfg.postgres_dsn else "sqlite"
    if cfg.postgres_dsn and not item.get("postgres_dsn"):
        item["postgres_dsn"] = cfg.postgres_dsn
    if item.get("embedding") is None:
        item["embedding"] = {
            "provider": cfg.embedding.provider,
            "model": cfg.embedding.model,
            "dim": cfg.embedding.dim,
        }
    # The DB folder = anchor: align db_dir to the yaml's data_dir (where the DB
    # actually lives), not where project.yaml happened to sit.
    if cfg.data_dir:
        item["db_dir"] = cfg.data_dir
    return item


def _load_raw(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _empty()
        if not isinstance(data, dict):
            return _empty()
        if not isinstance(data.get("projects"), list):
            data["projects"] = []
        data.setdefault("version", REGISTRY_VERSION)
        data.setdefault("last_active_slug", None)
        data["projects"] = [_enrich_item_from_yaml(i) for i in data["projects"]]
        return data
    # New file absent — migrate once from the legacy projects.json (no write).
    legacy = path.with_name(LEGACY_FILE_NAME)
    if legacy.exists():
        migrated = _migrate_legacy(legacy)
        migrated["projects"] = [_enrich_item_from_yaml(i) for i in migrated["projects"]]
        return migrated
    return _empty()


def _save_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _entry_from_item(item: dict[str, Any]) -> ProjectEntry | None:
    try:
        pd = str(item["project_dir"])
        return ProjectEntry(
            slug=str(item["slug"]),
            name=str(item.get("name") or item["slug"]),
            project_dir=pd,
            db_dir=str(item.get("db_dir") or pd),
            domain=item.get("domain"),
            group=item.get("group"),
            search_targets=item.get("search_targets"),
            save_tags=list(item.get("save_tags") or []),
            backend=item.get("backend"),
            postgres_dsn=item.get("postgres_dsn"),
            embedding=item.get("embedding"),
            ai_clients=list(item.get("ai_clients") or []),
            last_used=item.get("last_used"),
            created_at=item.get("created_at"),
        )
    except KeyError:
        return None


def list_projects(path: Path | None = None) -> list[ProjectEntry]:
    """All registered projects (file-order; UI sorts as needed)."""
    p = path or registry_path()
    raw = _load_raw(p)
    out: list[ProjectEntry] = []
    for item in raw.get("projects", []):
        if isinstance(item, dict):
            e = _entry_from_item(item)
            if e is not None:
                out.append(e)
    return out


def get_project(slug: str, path: Path | None = None) -> ProjectEntry | None:
    """One project by slug (None if absent)."""
    for e in list_projects(path):
        if e.slug == slug:
            return e
    return None


def find_by_dir(project_dir: str | Path, path: Path | None = None) -> ProjectEntry | None:
    """The entry whose ``project_dir`` or ``db_dir`` resolves to ``project_dir``."""
    try:
        target = str(Path(project_dir).resolve())
    except OSError:
        target = str(project_dir)
    for e in list_projects(path):
        for cand in (e.project_dir, e.db_dir):
            if not cand:
                continue
            try:
                if str(Path(cand).resolve()) == target:
                    return e
            except OSError:
                if str(cand) == target:
                    return e
    return None


def entry_env_dict(entry: ProjectEntry) -> dict[str, str]:
    """Build the MCP/UI env dict from a registry entry — the single source of
    truth replacing ``ProjectConfig.to_env_dict`` (same shape/keys).

    ``db_dir`` is the project anchor = ``DATA_DIR`` for sqlite. Postgres mode
    (``postgres_dsn`` set, or ``backend == 'postgres'``) emits an empty
    ``DATA_DIR`` and ``K2G_POSTGRES_DSN`` instead.
    """
    from k2g.core.config import search_targets_to_csv
    from k2g.ui.project_config import resolve_embedding_dim

    domain = entry.domain or "default"
    group = entry.group or "default"
    targets = entry.search_targets or [{"domain": domain}]
    import os

    emb = entry.embedding or {}
    model = str(emb.get("model") or "BAAI/bge-m3")
    # Fall back to the deployment env (portable bundle sets EMBEDDING_PROVIDER=onnx)
    # when the entry leaves the provider unset, so an onnx-only bundle (no torch)
    # does not default to the torch backend.
    provider = str(emb.get("provider") or os.environ.get("EMBEDDING_PROVIDER") or "local")
    dim = emb.get("dim")
    if dim in (None, ""):
        dim = resolve_embedding_dim(model)

    env: dict[str, str] = {
        "K2G_USER_MEMORY_SAVE_GROUP": group,
        "K2G_USER_MEMORY_SAVE_DOMAIN": domain,
        "K2G_USER_SEARCH_TARGETS": search_targets_to_csv(targets),
        "K2G_USER_MEMORY_SAVE_TAGS": ",".join(entry.save_tags or []),
        "K2G_DOTENV_FILE": "",
        # Defer store/embedding build to the first tool call so the MCP server
        # starts listening immediately. Eager init can take ~10s (sqlite) to ~60s
        # (remote Postgres — schema setup is many DDL round-trips over the pooler,
        # worse the farther the region), which exceeds an MCP client's startup
        # handshake timeout → the client marks the server "failed to connect"
        # even though it was merely still initializing. The one-time cost moves to
        # the first tool call instead (a much more generous timeout). Override by
        # setting K2G_MCP_LAZY_INIT in the client config env.
        "K2G_MCP_LAZY_INIT": "true",
        "EMBEDDING_PROVIDER": provider,
        "EMBEDDING_MODEL": model,
        "EMBEDDING_DIM": str(dim),
    }
    # Propagate the deployment-specific ONNX model path (set by the portable
    # launcher) so an onnx project's spawned MCP server can find the bundled model.
    if provider == "onnx":
        onnx_path = os.environ.get("EMBEDDING_ONNX_PATH")
        if onnx_path:
            env["EMBEDDING_ONNX_PATH"] = onnx_path
    is_pg = (entry.backend == "postgres") or bool(entry.postgres_dsn)
    # DATA_DIR is the *local* anchor for object storage (raw memory originals)
    # and log files — both exist even in Postgres mode (only graph/vector live
    # remotely). Always anchor it to the project's db_dir so they land under the
    # project folder. Emitting "" let the config validator expand the unset
    # sub-paths to "/objects" + "/logs", which resolve to the drive root
    # (e.g. F:\objects, F:\logs) — stray garbage folders. (Backend selection is
    # driven by K2G_POSTGRES_DSN, not DATA_DIR, so this does not flip to sqlite.)
    env["DATA_DIR"] = entry.db_dir or entry.project_dir
    if is_pg and entry.postgres_dsn:
        env["K2G_POSTGRES_DSN"] = entry.postgres_dsn
    return env


def add_project(
    *,
    name: str,
    project_dir: str | Path,
    slug: str | None = None,
    path: Path | None = None,
    **enriched: Any,
) -> ProjectEntry:
    """Register a new project. Returns the saved entry.

    Idempotent by resolved ``project_dir``: if it already exists, the existing
    entry is returned **unchanged** (use :func:`update_project` to edit fields).
    ``enriched`` (db_dir/domain/save_tags/backend/postgres_dsn/embedding/
    ai_clients) populate a *newly created* entry.
    """
    p = path or registry_path()
    data = _load_raw(p)
    items: list[dict[str, Any]] = data["projects"]
    normalized_dir = str(Path(project_dir).resolve())

    for item in items:
        if str(Path(item.get("project_dir", "")).resolve()) == normalized_dir:
            return _entry_from_item(item) or ProjectEntry(
                slug=str(item["slug"]), name=str(item.get("name") or item["slug"]),
                project_dir=item["project_dir"],
            )

    taken = {str(i.get("slug", "")) for i in items}
    final_slug = _ensure_unique_slug(_slugify(slug or name), taken)
    entry = ProjectEntry(
        slug=final_slug,
        name=name.strip() or final_slug,
        project_dir=normalized_dir,
        # db_dir is the project anchor (== project_dir): resolve to an absolute
        # path so a relative input (e.g. "MWEFTK2G") can't leak downstream and
        # get re-joined to project_dir → C:\MWEFTK2G\MWEFTK2G doubling.
        db_dir=str(Path(enriched.get("db_dir") or normalized_dir).resolve()),
        domain=enriched.get("domain"),
        save_tags=list(enriched.get("save_tags") or []),
        backend=enriched.get("backend"),
        postgres_dsn=enriched.get("postgres_dsn"),
        embedding=enriched.get("embedding"),
        ai_clients=list(enriched.get("ai_clients") or []),
        created_at=_now_iso(),
    )
    items.append(entry.to_dict())
    _save_raw(p, data)
    return entry


def add_named_project(
    name: str, *, slug: str | None = None, path: Path | None = None,
) -> ProjectEntry:
    """Register a *name-only* project (no DB folder yet) — identity by slug.

    Option A UI: registration captures just a display name; the DB folder
    (``db_dir``/anchor) is chosen later in the setup panel. ``db_dir`` stays
    unset until then (``activate`` guards on it).
    """
    p = path or registry_path()
    data = _load_raw(p)
    items: list[dict[str, Any]] = data["projects"]
    taken = {str(i.get("slug", "")) for i in items}
    final_slug = _ensure_unique_slug(_slugify(slug or name), taken)
    entry = ProjectEntry(
        slug=final_slug, name=name.strip() or final_slug,
        project_dir="", db_dir=None, created_at=_now_iso(),
    )
    items.append(entry.to_dict())
    _save_raw(p, data)
    return entry


def update_project(*, slug: str, path: Path | None = None, **fields: Any) -> bool:
    """Set enriched fields on an existing entry (by slug). True if updated."""
    p = path or registry_path()
    data = _load_raw(p)
    for item in data["projects"]:
        if str(item.get("slug")) == slug:
            for key, val in fields.items():
                if key in _ENRICHED_FIELDS and val is not None:
                    item[key] = val
            _save_raw(p, data)
            return True
    return False


def remove_project(slug: str, path: Path | None = None) -> bool:
    """Delete by slug. Returns True if removed, False if not found.

    Also clears ``last_active_slug`` if it pointed at the removed project.
    """
    p = path or registry_path()
    data = _load_raw(p)
    items: list[dict[str, Any]] = data["projects"]
    before = len(items)
    data["projects"] = [i for i in items if str(i.get("slug")) != slug]
    if len(data["projects"]) == before:
        return False
    if data.get("last_active_slug") == slug:
        data["last_active_slug"] = None
    _save_raw(p, data)
    return True


def touch_project(project_dir: str | Path, path: Path | None = None) -> None:
    """Update ``last_used`` for the entry matching ``project_dir`` (no-op if absent)."""
    p = path or registry_path()
    data = _load_raw(p)
    normalized = str(Path(project_dir).resolve())
    changed = False
    for item in data["projects"]:
        if str(Path(item.get("project_dir", "")).resolve()) == normalized:
            item["last_used"] = _now_iso()
            changed = True
            break
    if changed:
        _save_raw(p, data)


def get_last_active(path: Path | None = None) -> str | None:
    """The last-active project slug (None if unset / not present)."""
    p = path or registry_path()
    data = _load_raw(p)
    slug = data.get("last_active_slug")
    if not slug:
        return None
    if any(str(i.get("slug")) == slug for i in data.get("projects", [])):
        return str(slug)
    return None


def set_last_active(slug: str, path: Path | None = None) -> bool:
    """Pin the last-active project (must exist). True if set."""
    p = path or registry_path()
    data = _load_raw(p)
    if not any(str(i.get("slug")) == slug for i in data.get("projects", [])):
        return False
    data["last_active_slug"] = slug
    _save_raw(p, data)
    return True


__all__ = [
    "ProjectEntry",
    "registry_path",
    "legacy_registry_path",
    "list_projects",
    "get_project",
    "find_by_dir",
    "entry_env_dict",
    "add_project",
    "add_named_project",
    "update_project",
    "remove_project",
    "touch_project",
    "get_last_active",
    "set_last_active",
]
