"""``mweft-init`` — offline project bootstrap CLI.

The web UI (mweft-ui + mweft-ui-project) is overkill for the common case:
single Claude Code user pointing at a SQLite DB. This CLI writes the same
``.mweft/project.yaml`` + ``.mcp.json`` + ``CLAUDE.md`` triple **without
running any server** — Claude Code's MCP starts the in-process embedding +
write inline (no hub fall-through).

Typical flow::

    mweft-init my-project        # answer 5 prompts
    cd my-project
    # open Claude Code here → mweft tools work, no servers running

Or fully scripted::

    mweft-init my-project --group myteam --domain ai_memory \\
        --data-dir D:/mweft-data --client claude_code --yes
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from k2g.installer.installer import (
    InstallReport,
    ProjectNotInitialized,
    apply as installer_apply,
)
from k2g.installer.clients import available_clients
from k2g.ui.project_init import (
    PostgresBackend,
    SqliteBackend,
)
from k2g.ui.project_registry import (
    add_project,
    entry_env_dict,
    get_project,
    update_project,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str | None = None) -> str:
    """Read a string from stdin with an optional default shown in brackets."""
    cue = f"{prompt}"
    if default is not None:
        cue += f" [{default}]"
    cue += ": "
    raw = input(cue).strip()
    if not raw and default is not None:
        return default
    return raw


def _ask_choice(prompt: str, options: list[tuple[str, str]], default_idx: int = 0) -> str:
    """Ask the user to pick from a numbered list. Returns the option key."""
    print(prompt)
    for i, (_key, label) in enumerate(options, start=1):
        marker = " *" if i - 1 == default_idx else "  "
        print(f"{marker}{i}) {label}")
    raw = input(f"Choice [1-{len(options)}, default {default_idx + 1}]: ").strip()
    if not raw:
        return options[default_idx][0]
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
    except ValueError:
        pass
    print(f"  ⚠ invalid choice {raw!r}; using default")
    return options[default_idx][0]


def _confirm(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    raw = input(prompt + suffix + ": ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mweft-init",
        description=(
            "Bootstrap a MWeft project folder offline — writes "
            ".mweft/project.yaml + .mcp.json + CLAUDE.md, no servers required."
        ),
    )
    parser.add_argument(
        "project_dir", nargs="?",
        help="Project folder (will hold .mcp.json + CLAUDE.md). Prompted if omitted.",
    )
    parser.add_argument("--group", help="Group identifier (defaults to folder name).")
    parser.add_argument("--domain", help="Default memory domain (default: ai_memory).")
    parser.add_argument(
        "--data-dir",
        help="SQLite DB folder (absolute). Defaults to <project_dir>/data. "
             "Ignored when --postgres-dsn is set.",
    )
    parser.add_argument(
        "--postgres-dsn",
        help="Use Postgres backend with this DSN instead of SQLite.",
    )
    parser.add_argument(
        "--client", action="append",
        choices=[c["slug"] for c in available_clients()],
        help=(
            "MCP client to register mweft in (.mcp.json). Repeatable. "
            "Default: claude_code."
        ),
    )
    parser.add_argument(
        "--no-mcp", action="store_true",
        help="Skip the installer step — write project.yaml only.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing project.yaml.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Non-interactive: accept all defaults, fail if required args missing.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose log output.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    # 1. project_dir
    if args.project_dir is None:
        if args.yes:
            print("error: project_dir required in --yes mode", file=sys.stderr)
            return 2
        raw = _ask("Project folder (where .mcp.json + CLAUDE.md will live)")
        if not raw:
            print("error: project_dir is required", file=sys.stderr)
            return 2
        args.project_dir = raw
    project_dir = Path(args.project_dir).expanduser().resolve()
    print(f"  → project_dir = {project_dir}")

    # 2. group / domain
    group = args.group
    if group is None:
        group = project_dir.name if args.yes else _ask("Group", default=project_dir.name)
    domain = args.domain
    if domain is None:
        domain = "ai_memory" if args.yes else _ask("Domain", default="ai_memory")

    # 3. backend
    if args.postgres_dsn:
        backend: SqliteBackend | PostgresBackend = PostgresBackend(dsn=args.postgres_dsn)
        backend_label = f"postgres ({args.postgres_dsn})"
    else:
        if args.yes or args.data_dir is not None:
            data_dir = args.data_dir or str(project_dir / "data")
        else:
            kind = _ask_choice(
                "\nBackend:",
                [("sqlite", "SQLite (local files — single user)"),
                 ("postgres", "Postgres (multi-user — DSN required)")],
                default_idx=0,
            )
            if kind == "postgres":
                dsn = _ask("Postgres DSN (postgresql://user:pass@host:5432/db)")
                if not dsn:
                    print("error: DSN is required for postgres backend", file=sys.stderr)
                    return 2
                backend = PostgresBackend(dsn=dsn)
                backend_label = f"postgres ({dsn})"
                # short-circuit data_dir branch
                return _finish(args, project_dir, group, domain, backend, backend_label)
            data_dir = _ask("Data directory (SQLite DBs)", default=str(project_dir / "data"))
        backend = SqliteBackend(data_dir=data_dir)
        backend_label = f"sqlite (data_dir={data_dir})"

    return _finish(args, project_dir, group, domain, backend, backend_label)


def _finish(
    args: argparse.Namespace,
    project_dir: Path,
    group: str,
    domain: str,
    backend: SqliteBackend | PostgresBackend,
    backend_label: str,
) -> int:
    """Write project.yaml + (optionally) run the MCP installer."""
    print()
    print("  group   :", group)
    print("  domain  :", domain)
    print("  backend :", backend_label)

    if not args.yes and not _confirm("\nProceed?", default=True):
        print("aborted.")
        return 1

    # Option A: register the project in mweft_manager.json (single source of
    # truth) — no per-project project.yaml is written. project_dir = the IDE
    # working folder (where per-project .mcp.json goes); db_dir = the DB data
    # folder (= DATA_DIR). They are distinct.
    proj_dir = str(Path(project_dir).expanduser().resolve())
    if isinstance(backend, SqliteBackend):
        db_dir = str(Path(backend.data_dir).expanduser().resolve())
        backend_kind = "sqlite"
        dsn: str | None = None
        try:
            Path(db_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"error: data_dir not creatable: {exc}", file=sys.stderr)
            return 1
    else:
        dsn = backend.dsn
        if not dsn.startswith(("postgres://", "postgresql://")):
            print(f"error: postgres dsn must start with 'postgresql://': {dsn!r}",
                  file=sys.stderr)
            return 1
        db_dir = proj_dir   # postgres has no data folder — anchor on project_dir
        backend_kind = "postgres"

    e = add_project(name=Path(proj_dir).name or proj_dir, project_dir=proj_dir, db_dir=db_dir)
    update_project(
        slug=e.slug, group=group, domain=domain,
        search_targets=[{"domain": domain}], save_tags=[],
        backend=backend_kind, postgres_dsn=dsn,
        embedding={"provider": "local", "model": "BAAI/bge-m3", "dim": None},
        project_dir=proj_dir, db_dir=db_dir,
    )
    env = entry_env_dict(get_project(e.slug) or e)
    print(f"  ✓ registered '{e.slug}' in mweft_manager.json")
    print(f"  ✓ env keys: {', '.join(sorted(env))}")

    if args.no_mcp:
        print(
            "\nDone (--no-mcp). Add the .mcp.json entry manually using the env "
            "block above.",
        )
        return 0

    clients = args.client or ["claude_code"]
    print(f"\n→ Installing mweft for MCP clients: {', '.join(clients)}")
    report: InstallReport
    try:
        report = installer_apply(clients, project_dir=proj_dir)
    except ProjectNotInitialized as exc:  # pragma: no cover — entry just created
        print(f"error: {exc}", file=sys.stderr)
        return 1

    any_failed = False
    for cr in report.clients:
        marker = {
            "applied": "✓", "removed": "✓", "preview": "→",
            "skipped": "⚠", "failed": "✗",
        }.get(cr.status, "?")
        print(f"  {marker} [{cr.status}] {cr.label}")
        if cr.path:
            print(f"      path: {cr.path}")
        if cr.detail:
            print(f"      {cr.detail}")
        if cr.prompt_status:
            prompt_marker = {
                "appended": "✓", "already_present": "✓", "removed": "✓",
                "not_present": "·", "preview": "→",
                "unsupported": "·", "failed": "✗",
            }.get(cr.prompt_status, "?")
            print(f"      {prompt_marker} prompt: [{cr.prompt_status}]"
                  + (f" {cr.prompt_path}" if cr.prompt_path else ""))
        if cr.status == "failed":
            any_failed = True

    print()
    if any_failed:
        print("Some clients failed — see above. Project is still usable for "
              "clients that succeeded.")
    else:
        print(f"Done. Open {project_dir} in Claude Code (or your MCP client) and "
              f"`mweft_*` tools will work immediately.")
        print("First call downloads BGE-M3 (~2 GB). Run `k2g-warmup` to pre-fetch.")
    return 0 if not any_failed else 1


if __name__ == "__main__":
    sys.exit(main())
