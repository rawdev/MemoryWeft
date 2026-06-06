"""Staging path helpers — flat layout (session_id deprecated).

All staging files live under ``{data_dir}/build_stage/{domain}/`` (no session
subdirectory). Importing only this helper means producers and loaders
accumulate data across builds naturally (yesterday's events are still visible
during today's build).

File names:
- events.ndjson         : StagedEvent records (one event per line, append-only)
- entities.ndjson       : StagedEntity records (name-unique, MERGE semantics)
- groups.ndjson         : StagedGroup records (hierarchy paths)
- embeddings.npy        : (N, dim) float32 matrix
- embeddings_index.json : local_ref → row_index
- manifest.json         : staging metadata (counts, timestamps)
- produce.state         : in_progress | done | failed (+ last_error)
- incremental.ndjson    : segment/file action side-car for incremental builds

The session_id argument is accepted but *ignored* (legacy alias). Only the
flat directory layout is used; signatures are kept for gradual caller migration.
"""
from __future__ import annotations

from pathlib import Path

from k2g.core.config import resolve_build_stage_dir

STATE_IN_PROGRESS = "in_progress"
STATE_DONE = "done"
STATE_FAILED = "failed"

EVENTS_FILE = "events.ndjson"
ENTITIES_FILE = "entities.ndjson"
GROUPS_FILE = "groups.ndjson"
EMBEDDINGS_FILE = "embeddings.npy"
EMBEDDINGS_INDEX_FILE = "embeddings_index.json"
MANIFEST_FILE = "manifest.json"
PRODUCE_STATE_FILE = "produce.state"
INCREMENTAL_FILE = "incremental.ndjson"


def build_stage_root(configured: str = "") -> Path:
    """Return the staging root. Falls back to Settings.build_stage_dir when
    ``configured`` is empty."""
    return resolve_build_stage_dir(configured)


def domain_dir(
    domain_or_session: str = "",
    domain: str = "",
    configured_root: str = "",
) -> Path:
    """Return the flat domain directory ``build_stage_root() / domain``.

    Compatible call signatures:
    - New (keyword): ``domain_dir(domain="K2G")``
    - New (positional): ``domain_dir("K2G")``
    - Legacy: ``domain_dir(session_id, domain)`` — session_id is ignored

    Because session_id is ignored, any session resolves to the same directory.
    """
    # Argument dispatch — if the second positional arg (domain) is set,
    # this is a legacy call of the form (session_id, domain).
    if domain:
        actual_domain = domain
    else:
        actual_domain = domain_or_session
    if not actual_domain:
        raise ValueError("domain_dir: domain argument required")
    return build_stage_root(configured_root) / actual_domain


def session_dir(
    session_id: str, configured_root: str = "",
) -> Path:
    """Legacy alias — equivalent to the staging root.

    session_id is ignored. Callers using the ``session_dir(sid) / domain``
    pattern will resolve to ``build_stage_root() / domain`` (flat layout).
    """
    return build_stage_root(configured_root)


def events_path(*args, **kwargs) -> Path:
    return _resolve_domain_dir(*args, **kwargs) / EVENTS_FILE


def entities_path(*args, **kwargs) -> Path:
    return _resolve_domain_dir(*args, **kwargs) / ENTITIES_FILE


def groups_path(*args, **kwargs) -> Path:
    return _resolve_domain_dir(*args, **kwargs) / GROUPS_FILE


def embeddings_path(*args, **kwargs) -> Path:
    return _resolve_domain_dir(*args, **kwargs) / EMBEDDINGS_FILE


def embeddings_index_path(*args, **kwargs) -> Path:
    return _resolve_domain_dir(*args, **kwargs) / EMBEDDINGS_INDEX_FILE


def manifest_path(*args, **kwargs) -> Path:
    return _resolve_domain_dir(*args, **kwargs) / MANIFEST_FILE


def produce_state_path(*args, **kwargs) -> Path:
    return _resolve_domain_dir(*args, **kwargs) / PRODUCE_STATE_FILE


def incremental_path(*args, **kwargs) -> Path:
    """Path to the incremental build side-car (segment/file action log)."""
    return _resolve_domain_dir(*args, **kwargs) / INCREMENTAL_FILE


def _resolve_domain_dir(*args, **kwargs) -> Path:
    """Internal helper — accepts both positional and keyword arguments.

    Compatible signatures:
    - ``(domain,)`` — flat new-style
    - ``(session_id, domain)`` — legacy; session_id is ignored
    - ``(session_id, domain, configured_root)`` — legacy 3-arg form
    - keyword ``domain=``, ``configured_root=``
    """
    configured_root = kwargs.pop("configured_root", "")
    domain_kw = kwargs.pop("domain", None)
    if kwargs:
        raise TypeError(f"unexpected kwargs: {list(kwargs.keys())}")

    if domain_kw is not None:
        actual_domain = domain_kw
        if args and len(args) == 1:
            # Single positional arg without a configured_root override:
            # args[0] is session_id and is ignored.
            pass
        elif args and len(args) >= 2:
            configured_root = configured_root or (args[2] if len(args) >= 3 else "")
    else:
        if len(args) == 0:
            raise ValueError("domain argument required")
        elif len(args) == 1:
            actual_domain = args[0]
        elif len(args) == 2:
            # Legacy (session_id, domain) — session_id ignored
            actual_domain = args[1]
        elif len(args) == 3:
            # Legacy (session_id, domain, configured_root) — session_id ignored
            actual_domain = args[1]
            configured_root = configured_root or args[2]
        else:
            raise TypeError(f"too many positional args: {args}")
    if not actual_domain:
        raise ValueError("domain required")
    return build_stage_root(configured_root) / actual_domain


def list_domains(
    session_id: str = "",
    configured_root: str = "",
) -> list[str]:
    """Return the list of domain directories in the staging root.

    session_id is ignored. Returns only immediate subdirectories of the
    flat staging root.
    """
    root = build_stage_root(configured_root)
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def ensure_domain_dir(
    domain_or_session: str = "",
    domain: str = "",
    configured_root: str = "",
) -> Path:
    """Create the domain directory if needed and return its path.

    Accepts the same call signatures as domain_dir.
    """
    path = domain_dir(domain_or_session, domain, configured_root)
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_session_id() -> str:
    """Legacy compatibility helper. Does not affect staging location in the
    flat layout — used only for log and build-id identification."""
    import time
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


__all__ = [
    "STATE_IN_PROGRESS",
    "STATE_DONE",
    "STATE_FAILED",
    "EVENTS_FILE",
    "ENTITIES_FILE",
    "GROUPS_FILE",
    "EMBEDDINGS_FILE",
    "EMBEDDINGS_INDEX_FILE",
    "MANIFEST_FILE",
    "PRODUCE_STATE_FILE",
    "INCREMENTAL_FILE",
    "build_stage_root",
    "session_dir",
    "domain_dir",
    "events_path",
    "entities_path",
    "groups_path",
    "embeddings_path",
    "embeddings_index_path",
    "manifest_path",
    "produce_state_path",
    "incremental_path",
    "list_domains",
    "ensure_domain_dir",
    "default_session_id",
]
