"""BP-76 — Atomic JSON upsert with copy-paste fallback.

Reads the existing client config (or starts from {}), inserts our
``mweft`` server block, and atomically writes via ``tempfile`` +
``os.replace``.  When the write fails (sandbox / permission denied) we
return the would-be body as ``copy_paste_body`` for the UI to display.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from k2g.installer.clients import (
    ALL_CLIENTS,
    MCP_SERVER_KEY,
    MCPClientSpec,
    available_clients,
    get_client_spec,
)
from k2g.installer.locator import config_path, mirror_config_paths
from k2g.installer.prompts import (
    backup_file_once,
    install_prompt,
    uninstall_prompt,
)

logger = logging.getLogger(__name__)


@dataclass
class ClientResult:
    slug: str
    label: str
    path: str | None = None
    status: str = "pending"  # "applied" | "preview" | "removed" | "skipped" | "failed"
    detail: str = ""
    copy_paste_body: str | None = None
    # Prompt-file install (CLAUDE.md / .cursor/rules/mweft.md). For clients
    # without a file-based system prompt this stays None and the UI shows
    # the copy/paste body next to the JSON.
    prompt_path: str | None = None
    prompt_status: str | None = None    # appended / already_present / unsupported / preview / removed / not_present / failed
    prompt_detail: str = ""
    prompt_copy_paste_body: str | None = None
    # Pristine backups taken before we modified each file (config + prompt), so
    # the user can revert. None when nothing was backed up (new file).
    config_backup: str | None = None
    prompt_backup: str | None = None


@dataclass
class InstallReport:
    clients: list[ClientResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"clients": [asdict(c) for c in self.clients]}


def _resolve_k2g_mcp_command() -> str:
    """Resolve the ``k2g-mcp`` console-script path against the current venv.

    Some MCP clients (notably Gemini CLI on Windows) strip the parent
    shell's ``PATH`` when spawning the MCP subprocess, so a bare
    ``k2g-mcp`` shim name fails even when it resolves fine in the
    terminal.  Pinning to the absolute path next to ``sys.executable``
    makes the config portable across clients without depending on PATH
    inheritance.

    Falls back to the literal ``"k2g-mcp"`` when the script is not
    present alongside the active interpreter (test environments, alt
    installs); clients that inherit PATH still work in that case.
    """
    exe_dir = Path(sys.executable).resolve().parent
    for name in ("k2g-mcp.exe", "k2g-mcp"):
        candidate = exe_dir / name
        if candidate.is_file():
            return str(candidate)
    return "k2g-mcp"


def _build_default_server_block(
    *,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    project_dir: str | Path | None = None,
    entry_slug: str | None = None,
) -> dict[str, Any]:
    """The ``mweft`` server entry written to each client's config.

    When ``project_dir`` is supplied and ``.mweft/project.yaml`` exists
    (or can be installed from the template), the env dict comes from
    ``ProjectConfig.to_env_dict()`` — same shape the UI subprocess uses.
    Without that, only ``K2G_DOTENV_FILE=""`` is written and the user has
    to hand-edit (which is what was happening before).

    ``command`` defaults to the absolute path of ``k2g-mcp`` next to the
    active interpreter (see :func:`_resolve_k2g_mcp_command`); pass an
    explicit value to override (e.g. for tests).
    """
    block: dict[str, Any] = {
        "command": command if command is not None else _resolve_k2g_mcp_command(),
        "args": list(args or []),
    }

    if env is not None:
        block["env"] = dict(env)
        return block

    block["env"] = _resolve_env(project_dir, entry_slug)
    return block


class ProjectNotInitialized(Exception):
    """No registry entry for this project — run the Project setup panel first."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        super().__init__(
            f"Project not configured for {project_dir}. "
            f"Open the 'Project setup' panel first to register it "
            f"(group / domain / SQLite or Postgres) in mweft_manager.json."
        )


def _active_project_dir() -> str | None:
    """Best-effort active project for *global*-config clients.

    A global client (e.g. Claude Desktop) writes to a fixed config path, but its
    single ``mweft`` server still points at one DB — the active project's. So
    resolve env from K2G_PROJECT_DIR (the UI server's own project), falling back
    to the last-active project recorded in mweft_manager.json.
    """
    env_dir = os.environ.get("K2G_PROJECT_DIR", "").strip()
    if env_dir:
        return env_dir
    try:
        from k2g.ui.project_registry import get_last_active, get_project
        slug = get_last_active()
        entry = get_project(slug) if slug else None
        if entry is not None:
            return entry.db_dir or entry.project_dir
    except Exception:  # noqa: BLE001 — registry is optional
        pass
    return None


def _env_for_project(project_dir: str | Path | None) -> dict[str, str]:
    """Resolve the MCP env dict from the project's ``mweft_manager.json`` entry.

    - ``project_dir is None`` (global-config clients) → resolve the *active*
      project's env (so e.g. Claude Desktop gets the real DATA_DIR/domain/
      save_tags instead of a minimal env that defaults to ``./data``). Only when
      no active project can be found do we emit the minimal env.
    - ``project_dir`` set but no registry entry → raise
      :class:`ProjectNotInitialized` so the installer flow surfaces it.

    The registry (``mweft_manager.json``) is the single source of truth; the env
    is built via :func:`k2g.ui.project_registry.entry_env_dict`.
    """
    if project_dir is None:
        project_dir = _active_project_dir()
    if project_dir is None:
        return {"K2G_DOTENV_FILE": ""}
    from k2g.ui.project_registry import entry_env_dict, find_by_dir
    entry = find_by_dir(project_dir)
    if entry is None:
        raise ProjectNotInitialized(Path(project_dir))
    return entry_env_dict(entry)


def _resolve_env(
    project_dir: str | Path | None, entry_slug: str | None,
) -> dict[str, str]:
    """Env for the server block, preferring an explicit registry ``entry_slug``.

    When two registry entries share the same ``project_dir`` (e.g. the IDE folder
    was pointed at the same path for two projects), resolving env by folder alone
    (:func:`find_by_dir`) returns whichever entry is listed first — so the
    project the user actually edited can be silently ignored. Passing the edited
    entry's ``slug`` resolves env from *that* entry; we fall back to the folder
    only when no slug is given or it no longer exists.
    """
    if entry_slug:
        from k2g.ui.project_registry import entry_env_dict, get_project
        entry = get_project(entry_slug)
        if entry is not None:
            return entry_env_dict(entry)
    return _env_for_project(project_dir)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("installer: failed to parse %s — starting fresh: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("installer: %s is not a JSON object — starting fresh", path)
        return {}
    return data


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- TOML (Codex) ---------------------------------------------------------
#
# Codex stores MCP servers in ``~/.codex/config.toml`` as ``[mcp_servers.NAME]``
# tables. config.toml is commonly hand-edited with comments, so we never
# round-trip the whole file (tomllib has no writer and would drop comments).
# Instead we append our block when absent, or replace just our block in place
# when present — preserving everything else verbatim.


def _toml_escape_basic(s: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _toml_key(k: str) -> str:
    """Bare key when it is a valid TOML bare key, else a quoted key."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", k):
        return k
    return f'"{_toml_escape_basic(k)}"'


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f'"{_toml_escape_basic(v)}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(val)}" for k, val in v.items())
        return "{ " + inner + " }" if inner else "{}"
    return f'"{_toml_escape_basic(str(v))}"'


def _emit_toml_table(container_key: str, server_key: str, block: dict[str, Any]) -> str:
    """Render a single ``[<container_key>.<server_key>]`` table (no sub-tables).

    ``env`` is emitted as an inline table so the whole entry is one
    contiguous block with no nested ``[...]`` headers — which keeps the
    in-place replacement boundary detection unambiguous.
    """
    lines = [f"[{_toml_key(container_key)}.{_toml_key(server_key)}]"]
    for k in ("command", "args"):
        if k in block:
            lines.append(f"{k} = {_toml_value(block[k])}")
    if "env" in block:
        lines.append(f"env = {_toml_value(block['env'])}")
    for k, v in block.items():
        if k not in ("command", "args", "env"):
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


def _toml_header_re(container_key: str, server_key: str) -> re.Pattern[str]:
    return re.compile(
        r"^[ \t]*\[[ \t]*"
        + re.escape(container_key)
        + r"[ \t]*\.[ \t]*"
        + re.escape(server_key)
        + r"[ \t]*\][ \t]*$",
        re.MULTILINE,
    )


def _toml_has_server(text: str, container_key: str, server_key: str) -> bool:
    """True if the parsed TOML already declares ``container_key.server_key``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    cont = data.get(container_key)
    return isinstance(cont, dict) and server_key in cont


def _toml_block_span(text: str, container_key: str, server_key: str) -> tuple[int, int] | None:
    """Char span of our existing ``[container.server]`` block, or None.

    The block runs from its header to the next top-level ``[`` header
    (table or array-of-tables) or EOF.
    """
    m = _toml_header_re(container_key, server_key).search(text)
    if m is None:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^[ \t]*\[", rest, re.MULTILINE)
    end = len(text) if nxt is None else m.end() + nxt.start()
    return m.start(), end


def _toml_install_text(
    text: str, container_key: str, server_key: str, block: dict[str, Any]
) -> str | None:
    """Return new file text with our block upserted, or None if it exists
    in a form we cannot safely replace (inline/dotted) → caller falls back
    to a manual copy-paste preview."""
    table = _emit_toml_table(container_key, server_key, block)
    span = _toml_block_span(text, container_key, server_key)
    if span is None:
        if _toml_has_server(text, container_key, server_key):
            return None  # present but not as a replaceable [table] header
        if text.strip():
            sep = "" if text.endswith("\n") else "\n"
            return text + sep + "\n" + table
        return table
    start, end = span
    return text[:start] + table + text[end:]


def _toml_remove_text(text: str, container_key: str, server_key: str) -> tuple[str, bool]:
    span = _toml_block_span(text, container_key, server_key)
    if span is None:
        return text, False
    start, end = span
    # Eat one blank separator line preceding our header, if we added it.
    while start > 0 and text[start - 1] in "\r\n":
        start -= 1
    new_text = text[:start] + text[end:]
    return new_text, True


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("installer: failed to read %s — starting fresh: %s", path, exc)
        return ""


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _process_one_toml(
    spec: MCPClientSpec,
    cr: ClientResult,
    path: Path | None,
    *,
    project_dir: str | Path | None,
    server_block: dict[str, Any],
    dry_run: bool,
    operation: str,
) -> ClientResult:
    """Codex / TOML path — block append/replace preserving the rest of the file."""
    table = _emit_toml_table(spec.container_key, MCP_SERVER_KEY, server_block)
    if path is None:
        cr.status = "skipped"
        cr.detail = spec.note or f"No known config location for {spec.label!r} on this OS."
        cr.copy_paste_body = table if operation == "install" else ""
        _attach_prompt_result(cr, project_dir=project_dir, dry_run=dry_run, operation=operation)
        return cr

    cr.path = str(path)
    existing = _read_text(path)

    if operation == "install":
        merged = _toml_install_text(existing, spec.container_key, MCP_SERVER_KEY, server_block)
        if merged is None:
            # mweft already declared in a non-replaceable (inline/dotted) form.
            cr.status = "preview"
            cr.detail = (
                f"'mweft' already present in {path} in an inline form — "
                f"merge the block below manually to avoid a duplicate table."
            )
            cr.copy_paste_body = table
            _attach_prompt_result(cr, project_dir=project_dir, dry_run=dry_run, operation=operation)
            return cr
    else:
        merged, _ = _toml_remove_text(existing, spec.container_key, MCP_SERVER_KEY)

    if dry_run:
        cr.status = "preview"
        cr.detail = "dry-run (no file modified)"
        cr.copy_paste_body = table if operation == "install" else merged
        _attach_prompt_result(cr, project_dir=project_dir, dry_run=dry_run, operation=operation)
        return cr

    # Uninstall that empties the file (it held only our block) → remove it,
    # but only if *we* created it (don't delete a pre-existing empty file).
    if operation == "uninstall" and not merged.strip() and existing.strip():
        try:
            path.unlink()
        except OSError as exc:
            cr.status = "failed"
            cr.detail = f"cannot remove {path}: {exc}"
            _attach_prompt_result(cr, project_dir=project_dir, dry_run=dry_run, operation=operation)
            return cr
        cr.status = "removed"
        cr.detail = f"removed {path} (was our content only)"
        _attach_prompt_result(cr, project_dir=project_dir, dry_run=dry_run, operation=operation)
        return cr

    cr.config_backup = backup_file_once(path)
    try:
        _atomic_write_text(path, merged)
    except (OSError, PermissionError) as exc:
        cr.status = "failed"
        cr.detail = f"cannot write {path}: {exc}"
        cr.copy_paste_body = table
        _attach_prompt_result(cr, project_dir=project_dir, dry_run=dry_run, operation=operation)
        return cr

    cr.status = "applied" if operation == "install" else "removed"
    cr.detail = f"wrote {path}"
    _attach_prompt_result(cr, project_dir=project_dir, dry_run=dry_run, operation=operation)
    return cr


def _process_one(
    spec: MCPClientSpec,
    *,
    project_dir: str | Path | None,
    server_block: dict[str, Any],
    dry_run: bool,
    operation: str,  # "install" | "uninstall"
) -> ClientResult:
    cr = ClientResult(slug=spec.slug, label=spec.label)
    path = config_path(spec.slug, project_dir=project_dir)
    if spec.serialization == "toml":
        return _process_one_toml(
            spec, cr, path, project_dir=project_dir, server_block=server_block,
            dry_run=dry_run, operation=operation,
        )
    if path is None:
        cr.status = "skipped"
        cr.detail = (
            spec.note
            or f"No known config location for {spec.label!r} on this OS."
        )
        # Provide a copy-paste body so the user can paste it manually.
        sample = spec.upsert({}, server_block) if operation == "install" else {}
        cr.copy_paste_body = json.dumps(sample, indent=2, ensure_ascii=False)
        _attach_prompt_result(cr, project_dir=project_dir,
                              dry_run=dry_run, operation=operation)
        return cr

    cr.path = str(path)
    existing = _read_json(path)
    if operation == "install":
        new_body = spec.upsert(dict(existing), server_block)
    else:
        new_body = spec.remove(dict(existing))

    if dry_run:
        cr.status = "preview"
        cr.detail = "dry-run (no file modified)"
        cr.copy_paste_body = json.dumps(new_body, indent=2, ensure_ascii=False)
        _attach_prompt_result(cr, project_dir=project_dir,
                              dry_run=dry_run, operation=operation)
        return cr

    cr.config_backup = backup_file_once(path)
    try:
        _atomic_write_json(path, new_body)
    except (OSError, PermissionError) as exc:
        cr.status = "failed"
        cr.detail = f"cannot write {path}: {exc}"
        cr.copy_paste_body = json.dumps(new_body, indent=2, ensure_ascii=False)
        _attach_prompt_result(cr, project_dir=project_dir,
                              dry_run=dry_run, operation=operation)
        return cr

    cr.status = "applied" if operation == "install" else "removed"
    cr.detail = f"wrote {path}"
    # Mirror to extra paths (Store/MSIX Claude: the plain %APPDATA%\Claude copy)
    # so the app-read copy and the plain copy never diverge. Best-effort — the
    # primary already succeeded; each mirror keeps its own other-servers (only
    # the mweft entry is synced).
    for mirror in mirror_config_paths(spec.slug, project_dir=project_dir):
        try:
            mbody = (
                spec.upsert(_read_json(mirror), server_block)
                if operation == "install" else spec.remove(_read_json(mirror))
            )
            backup_file_once(mirror)
            _atomic_write_json(mirror, mbody)
        except (OSError, PermissionError):
            pass  # mirror is best-effort; the primary write already succeeded
    _attach_prompt_result(cr, project_dir=project_dir,
                          dry_run=dry_run, operation=operation)
    return cr


def _attach_prompt_result(
    cr: ClientResult,
    *,
    project_dir: str | Path | None,
    dry_run: bool,
    operation: str,
) -> None:
    """Run the per-client prompt installer and copy the result onto ``cr``.

    Called after the JSON config has been (would-be) written so the UI
    sees both pieces in a single row.
    """
    if operation == "install":
        pr = install_prompt(cr.slug, project_dir=project_dir, dry_run=dry_run)
    else:
        pr = uninstall_prompt(cr.slug, project_dir=project_dir)
    cr.prompt_path = pr.target_path
    cr.prompt_status = pr.status
    cr.prompt_detail = pr.detail
    cr.prompt_copy_paste_body = pr.copy_paste_body
    cr.prompt_backup = pr.backup_path


def _not_initialized_report(
    slugs: list[str] | None,
    detail: str,
) -> InstallReport:
    """All clients fail-loud when project.yaml is missing — guides user
    to run Project setup panel first."""
    report = InstallReport()
    for spec in _normalize_clients(slugs):
        report.clients.append(ClientResult(
            slug=spec.slug,
            label=spec.label,
            status="failed",
            detail=detail,
        ))
    return report


def _normalize_clients(slugs: list[str] | None) -> list[MCPClientSpec]:
    if slugs is None:
        return list(ALL_CLIENTS)
    out: list[MCPClientSpec] = []
    for s in slugs:
        spec = get_client_spec(s)
        if spec is not None:
            out.append(spec)
    return out


def is_installed(
    spec: MCPClientSpec,
    project_dir: str | Path | None = None,
) -> tuple[bool, str | None]:
    """Probe whether the ``mweft`` server is registered in this client's config.

    Returns ``(installed, config_path|None)``. The path is None when the client
    has no resolvable config location on this OS (e.g. chatgpt). Corrupt /
    unreadable configs count as not-installed.
    """
    try:
        raw = config_path(spec.slug, project_dir=project_dir)
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return False, None
    p = Path(raw)
    if not p.is_file():
        return False, str(p)
    if spec.serialization == "toml":
        try:
            return _toml_has_server(_read_text(p), spec.container_key, MCP_SERVER_KEY), str(p)
        except Exception:  # noqa: BLE001
            return False, str(p)
    data = _read_json(p)
    if spec.schema == "array":
        servers = data.get("mcpServers")
        ok = isinstance(servers, list) and any(
            isinstance(s, dict) and s.get("name") == MCP_SERVER_KEY for s in servers
        )
    else:
        servers = data.get(spec.container_key)
        ok = isinstance(servers, dict) and MCP_SERVER_KEY in servers
    return ok, str(p)


def read_client_mweft_env(
    spec: MCPClientSpec, *, project_dir: str | Path | None = None
) -> dict[str, Any] | None:
    """Extract the embedded ``mweft`` server ``env`` dict from a client's on-disk
    config, or ``None`` if the file / mweft block / env is absent or unreadable.

    Used to reverse-match a *global* client back to the registry project it points
    at (see :func:`global_targets`). Never raises — malformed configs → ``None``.
    """
    try:
        raw = config_path(spec.slug, project_dir=project_dir)
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return None
    p = Path(raw)
    if not p.is_file():
        return None
    block: Any = None
    if spec.serialization == "toml":
        try:
            data = tomllib.loads(_read_text(p))
        except Exception:  # noqa: BLE001
            return None
        cont = data.get(spec.container_key)
        if isinstance(cont, dict):
            block = cont.get(MCP_SERVER_KEY)
    elif spec.schema == "array":
        # array-form clients aren't global — nothing to reverse-match.
        return None
    else:
        data = _read_json(p)
        cont = data.get(spec.container_key)
        if isinstance(cont, dict):
            block = cont.get(MCP_SERVER_KEY)
    if not isinstance(block, dict):
        return None
    env = block.get("env")
    return env if isinstance(env, dict) else None


def _match_env_to_entry(env: dict[str, Any]) -> Any:
    """Reverse-match an extracted client ``env`` to a registry entry (or ``None``).

    Identity is the project *anchor*, not its mutable settings: ``K2G_POSTGRES_DSN``
    for postgres, ``DATA_DIR`` (== db_dir) for sqlite. ``domain``/``group`` can
    drift from a stale snapshot, so they are only a tiebreaker when several sqlite
    entries share the same folder. DATA_DIR compared by resolved path (string
    fallback). A non-empty DSN that matches nothing returns ``None`` (never falls
    through to a spurious DATA_DIR match).
    """
    from k2g.ui.project_registry import entry_env_dict, list_projects

    def _norm(d: str) -> str:
        try:
            return str(Path(d).resolve()).lower()
        except Exception:  # noqa: BLE001
            return str(d or "").strip().lower()

    dsn = (env.get("K2G_POSTGRES_DSN") or "").strip()
    entries = list_projects()
    if dsn:
        for entry in entries:
            if (entry_env_dict(entry).get("K2G_POSTGRES_DSN") or "").strip() == dsn:
                return entry
        return None
    data_dir = env.get("DATA_DIR") or ""
    if not data_dir:
        return None
    target = _norm(data_dir)
    candidates = [
        (entry, ee)
        for entry in entries
        for ee in [entry_env_dict(entry)]
        if not (ee.get("K2G_POSTGRES_DSN") or "").strip()  # sqlite only
        and _norm(ee.get("DATA_DIR") or "") == target
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    # Several sqlite entries share this folder → disambiguate by domain+group.
    domain = env.get("K2G_USER_MEMORY_SAVE_DOMAIN")
    group = env.get("K2G_USER_MEMORY_SAVE_GROUP")
    for entry, ee in candidates:
        if (
            ee.get("K2G_USER_MEMORY_SAVE_DOMAIN") == domain
            and ee.get("K2G_USER_MEMORY_SAVE_GROUP") == group
        ):
            return entry
    return candidates[0][0]


def global_targets() -> list[dict[str, Any]]:
    """Read-only matrix for the UI: each *global* client (those with one fixed
    config path) and the registry project it currently points at.

    A global client embeds one project's env; we read it back and reverse-match.
    Per row: ``{slug, label, installed, config_path, pointed_slug, pointed_name}``.
    ``installed`` True + ``pointed_slug`` None → config present but matches no known
    project; ``installed`` False → the mweft server isn't registered in that client.
    """
    out: list[dict[str, Any]] = []
    for spec in ALL_CLIENTS:
        if spec.requires_project_dir:
            continue  # global-only
        installed, path = is_installed(spec)
        env = read_client_mweft_env(spec) if installed else None
        entry = _match_env_to_entry(env) if env else None
        out.append({
            "slug": spec.slug,
            "label": spec.label,
            "installed": installed,
            "config_path": path,
            "pointed_slug": getattr(entry, "slug", None) if entry else None,
            "pointed_name": getattr(entry, "name", None) if entry else None,
        })
    return out


def install_conflict(
    slug: str,
    *,
    project_dir: str | Path | None = None,
    entry_slug: str | None = None,
) -> dict[str, Any]:
    """Pre-install probe: would applying ``slug`` overwrite an existing mweft block
    that points at a *different* project?

    The install upsert (:func:`_upsert_map_form`) replaces any existing ``mweft``
    server entry unconditionally, so a config already wired to another project is
    silently repointed. This reads the on-disk env back and reverse-matches it so
    the UI can warn first.

    Returns ``{slug, exists, path, pointed_slug, pointed_name, same}``:
    - ``exists`` False → fresh install, no conflict (``same`` True).
    - ``exists`` True + ``same`` True → already points at the entry we're about to
      (re)install — safe, no warning.
    - ``exists`` True + ``same`` False → overwrite: currently points at
      ``pointed_name`` (or an unknown project when ``pointed_slug`` is None).
    Never raises — any read error degrades to ``exists`` False.
    """
    spec = get_client_spec(slug)
    if spec is None:
        return {"slug": slug, "exists": False, "same": True}
    try:
        installed, path = is_installed(spec, project_dir)
    except Exception:  # noqa: BLE001 — preflight must never break the install flow
        return {"slug": slug, "exists": False, "same": True}
    if not installed:
        return {"slug": slug, "exists": False, "path": path, "same": True}
    env = read_client_mweft_env(spec, project_dir=project_dir)
    pointed = _match_env_to_entry(env) if env else None
    pointed_slug = getattr(pointed, "slug", None) if pointed else None
    same = bool(entry_slug and pointed_slug and pointed_slug == entry_slug)
    return {
        "slug": slug,
        "exists": True,
        "path": path,
        "pointed_slug": pointed_slug,
        "pointed_name": getattr(pointed, "name", None) if pointed else None,
        "same": same,
    }


def installer_status(project_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Per-client install status for the UI list (installed/scope/path).

    Each *installed* client also carries ``bound_slug``/``bound_name`` — the
    registry project its embedded env actually points at (reverse-matched). The
    per-project setup list filters on this so a client shows up only under the
    project it's bound to (a global client points at ONE project; a project
    client at a shared folder belongs to whichever project its env names), not
    under every project that merely has it installed.
    """
    out: list[dict[str, Any]] = []
    for spec in ALL_CLIENTS:
        scope = "project" if spec.requires_project_dir else "global"
        pd = project_dir if spec.requires_project_dir else None
        installed, path = is_installed(spec, project_dir=pd)
        bound = _match_env_to_entry(read_client_mweft_env(spec, project_dir=pd) or {}) \
            if installed else None
        out.append({
            "slug": spec.slug,
            "label": spec.label,
            "scope": scope,
            "requires_project_dir": spec.requires_project_dir,
            "config_path": path,
            "installed": installed,
            "bound_slug": getattr(bound, "slug", None) if bound else None,
            "bound_name": getattr(bound, "name", None) if bound else None,
            "note": spec.note,
        })
    return out


def _persist_installed_ai_clients(
    project_dir: str | Path | None, entry_slug: str | None
) -> None:
    """Record the matching registry entry's current installed-AI set after an
    install/uninstall: ``[{slug, scope, config_path}, …]``.

    The registry is the source of truth for the UI's installed list AND for
    :func:`reapply_for_entry` (activation re-points global clients only if this
    list is populated). Syncing it here — on the operation that actually changes
    the client configs, not as a frontend side-effect — keeps it correct for
    every caller (web route, CLI, activation). Best-effort: never raises into the
    install path. Resolves the entry by ``entry_slug`` first (authoritative when
    several entries share a folder), else by ``project_dir``.
    """
    try:
        from k2g.ui.project_registry import (
            find_by_dir,
            get_project,
            update_project,
        )
        entry = get_project(entry_slug) if entry_slug else None
        if entry is None and project_dir:
            entry = find_by_dir(project_dir)
        if entry is None:
            return
        installed = [
            {"slug": c["slug"], "scope": c["scope"], "config_path": c["config_path"]}
            for c in installer_status(project_dir=project_dir)
            if c.get("installed")
        ]
        update_project(slug=entry.slug, ai_clients=installed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("persist ai_clients to registry failed: %s", exc)


def preview(
    slugs: list[str] | None = None,
    *,
    project_dir: str | Path | None = None,
    server_block: dict[str, Any] | None = None,
    entry_slug: str | None = None,
) -> InstallReport:
    """Dry-run — returns the JSON that *would* be written for each client."""
    try:
        sb = server_block or _build_default_server_block(
            project_dir=project_dir, entry_slug=entry_slug)
    except ProjectNotInitialized as exc:
        return _not_initialized_report(slugs, str(exc))
    report = InstallReport()
    for spec in _normalize_clients(slugs):
        report.clients.append(
            _process_one(spec, project_dir=project_dir, server_block=sb,
                         dry_run=True, operation="install"),
        )
    return report


def apply(
    slugs: list[str] | None = None,
    *,
    project_dir: str | Path | None = None,
    server_block: dict[str, Any] | None = None,
    entry_slug: str | None = None,
) -> InstallReport:
    """Atomically write each client's config; copy-paste body on failure.

    ``entry_slug`` pins which registry entry's env is written when several entries
    share the same ``project_dir`` (resolves env by slug instead of by folder).
    """
    try:
        sb = server_block or _build_default_server_block(
            project_dir=project_dir, entry_slug=entry_slug)
    except ProjectNotInitialized as exc:
        return _not_initialized_report(slugs, str(exc))
    report = InstallReport()
    for spec in _normalize_clients(slugs):
        report.clients.append(
            _process_one(spec, project_dir=project_dir, server_block=sb,
                         dry_run=False, operation="install"),
        )
    # The project's ``ai_clients`` list is *not* synced here: it is user-managed
    # via the setup panel (add/remove → PUT /projects/{slug}/ai-clients). Applying
    # config (the "Install" button / apply-all) must never rewrite that list from a
    # disk scan, or it would re-add clients the user removed and drop ones added
    # but not yet installed.
    return report


def uninstall(
    slugs: list[str] | None = None,
    *,
    project_dir: str | Path | None = None,
    entry_slug: str | None = None,
) -> InstallReport:
    """Atomically remove the mweft entry from each client's config.

    ``entry_slug`` pins which registry entry's installed-AI list is refreshed
    when several entries share the same ``project_dir``.
    """
    report = InstallReport()
    for spec in _normalize_clients(slugs):
        report.clients.append(
            _process_one(spec, project_dir=project_dir, server_block={},
                         dry_run=False, operation="uninstall"),
        )
    # See apply(): the registry ``ai_clients`` list is user-managed by the panel,
    # not derived from a disk scan here. The "Remove" button removes the slug from
    # the list explicitly (PUT) in addition to calling this uninstall.
    return report


def reapply_for_entry(entry: Any) -> dict[str, Any]:
    """Re-apply the MCP config for every AI client a project has installed.

    Used when a project is *activated*: global-only clients (Claude Desktop,
    cursor_global, …) write to one fixed config path, so switching projects in
    the Manager UI must rewrite their env to point at the newly-activated
    project. Project-scoped clients are re-applied against ``entry.project_dir``
    (falling back to ``db_dir`` for name-only projects).

    Returns ``{"reapplied": [<ClientResult dict>, …],
               "restart_needed": [{"slug", "label"}, …]}``.
    Global clients land in ``restart_needed`` because their host app must be
    restarted to pick up the rewritten MCP server env. Best-effort: a write
    error for one client is captured in its result and never raised, so the
    caller (activation) always completes.
    """
    clients = [
        c for c in (getattr(entry, "ai_clients", None) or [])
        if isinstance(c, dict) and c.get("slug")
    ]
    if not clients:
        return {"reapplied": [], "restart_needed": []}

    entry_pd = getattr(entry, "project_dir", None) or getattr(entry, "db_dir", None)
    slug = getattr(entry, "slug", None)
    # Apply each client with ITS OWN project_dir. A project-scoped client
    # (claude_code, cursor_project, …) may live in a folder different from the
    # project anchor, and several entries may each target a distinct folder.
    # Using one shared project_dir for all would collapse every project client
    # onto the same .mcp.json (the last folder wins). Global clients ignore
    # project_dir (fixed config path), so the value is moot for them.
    reapplied: list[dict[str, Any]] = []
    for c in clients:
        cslug = c["slug"]
        c_pd = c.get("project_dir") or entry_pd
        try:
            report = apply([cslug], project_dir=c_pd, entry_slug=slug)
            reapplied.extend(report.to_dict()["clients"])
        except Exception as exc:  # noqa: BLE001 — activation must never fail on a client write
            logger.warning("reapply_for_entry: %s failed: %s", cslug, exc)
            reapplied.append({"slug": cslug, "status": "failed", "detail": str(exc)})

    restart_needed: list[dict[str, str]] = []
    for c in clients:
        if not isinstance(c, dict) or c.get("scope") != "global":
            continue
        slug = c.get("slug")
        if not slug:
            continue
        spec = get_client_spec(slug)
        restart_needed.append({"slug": slug, "label": spec.label if spec else slug})
    return {"reapplied": reapplied, "restart_needed": restart_needed}
