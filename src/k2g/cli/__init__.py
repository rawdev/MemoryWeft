"""K2G CLI — `python -m k2g` entry point.

Legacy paths for Qdrant / Neo4j / IngestionPipeline have all been removed.
Currently supported subcommands operate on DbStore + trainer + reader +
covenant store.

Supported subcommands:
    k2g search-entity <name>            — Substring entity name lookup
    k2g stats [--domain D]              — Node counts per domain
    k2g health                          — graph/vector/content ping
    k2g train <phase> [--domain D]      — Run a single trainer phase
                                          phase in {jaccard,hdbscan,control_node}
    k2g covenant list/add/remove/
              enable/disable/validate   — Covenant management

Removed subcommands:
    query / generate-scene              — legacy adapter chain (deferred
                                          until adapters are ported)
    ingest vcs                          — moved to scripts/k2g_build.py CLI
"""

from __future__ import annotations

import argparse
import json as _json
import logging
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _open_db() -> Any:
    """DbStore factory — calls all three setup_schema methods."""
    from k2g.core.config import get_settings
    from k2g.db_store import DbStore

    settings = get_settings()
    db = DbStore.from_settings(settings)
    for method_name in ("setup_schema", "setup_training_schema", "setup_bp30_schema"):
        fn = getattr(db.graph, method_name, None)
        if fn is None:
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).debug(
                "graph.%s skip/fail: %s", method_name, exc,
            )
    return db


def _print_error(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# search-entity
# ---------------------------------------------------------------------------


def cmd_search_entity(args: argparse.Namespace) -> int:
    """Substring entity lookup via graph.search_entities_by_name.

    reader.GraphQueryService.entity_lookup performs an exact (name, domain)
    match and is therefore unsuitable for CLI substring search. This command
    calls db.graph.search_entities_by_name directly for a LIKE search.
    """
    db = _open_db()
    try:
        rows = db.graph.search_entities_by_name(
            args.name, domain=args.domain, limit=20,
        )
        if not rows:
            print(f"No entities found: {args.name}")
            return 0
        for r in rows:
            print(
                f"  {str(r.get('id','?')):<16}  {r.get('name','?')}"
                f"  (domain={r.get('domain','?')})"
            )
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def cmd_stats(args: argparse.Namespace) -> int:
    """Domain node counts via graph.get_statistics + list_domains summary."""
    db = _open_db()
    try:
        stats = db.graph.get_statistics(domain=args.domain)
        for key, value in stats.items():
            print(f"  {key:<18}{value}")
        if args.domain is None:
            domains = db.graph.list_domains()
            if domains:
                print(f"\n  domains         {', '.join(domains)}")
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace) -> int:  # noqa: ARG001
    """DbStore backend ping + schema presence check."""
    db = _open_db()
    checks: list[tuple[str, bool, str]] = []

    for name, store in (
        ("graph", db.graph),
        ("vector", db.vector),
        ("content", db.content),
    ):
        try:
            ok = bool(store.ping()) if hasattr(store, "ping") else True
            detail = "ping ok" if ok else "ping failed"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"exception: {exc}"
        checks.append((name, ok, detail))

    try:
        info = db.vector.get_collection_info()
        checks.append((
            "vector.info", True,
            f"backend={info.get('backend','?')}, dim={info.get('dim','?')}",
        ))
    except Exception as exc:  # noqa: BLE001
        checks.append(("vector.info", False, f"get_collection_info failed: {exc}"))

    all_ok = all(ok for _, ok, _ in checks)
    label = "HEALTHY" if all_ok else "UNHEALTHY"
    print(f"[{label}]")
    for name, ok, detail in checks:
        sym = "✓" if ok else "✗"
        print(f"  {sym} {name:<14}  {detail}")

    db.close()
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# train (single phase)
# ---------------------------------------------------------------------------


def cmd_train(args: argparse.Namespace) -> int:
    """Run a single trainer phase. Iterates over all domains if none specified."""
    from k2g.trainer import Trainer

    db = _open_db()
    try:
        trainer = Trainer()
        if trainer.get(args.phase) is None:
            _print_error(
                f"unknown phase: {args.phase!r} "
                f"(available: jaccard/hdbscan/control_node)",
            )
            return 2

        domains: list[str] = [args.domain] if args.domain else db.graph.list_domains()
        if not domains:
            print("[k2g train] no target domains (events table is empty)")
            return 0

        failed = False
        for dom in domains:
            result = trainer.run_phase(args.phase, db, domain=dom)
            sym = "✓" if result.success else "✗"
            counts = ", ".join(f"{k}={v}" for k, v in result.counts.items())
            print(f"  {sym} {args.phase:<14} domain={dom:<12}  {counts}")
            if result.error:
                print(f"      error: {result.error}")
                failed = True
        return 1 if failed else 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# covenant — managed source registry
# ---------------------------------------------------------------------------


def _open_covenant_store() -> Any:
    from k2g.core.config import get_settings
    from k2g.stores.covenant_store import CovenantStore

    return CovenantStore(get_settings().covenant_db_path)


def _parse_config_arg(raw: str) -> dict[str, Any]:
    if raw.startswith("@"):
        return _json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    return _json.loads(raw)


def _summarize_config(type_: str, config: dict[str, Any]) -> str:
    if type_ == "filesystem":
        root = config.get("root", "?")
        inc = config.get("include", [])
        return f"root={root}, {','.join(inc) if inc else '*'}"
    if type_ == "vcs":
        return (
            f"{config.get('vcs_type','git')}, "
            f"branch={config.get('branch','?')}, "
            f"mode={config.get('mode','direct')}"
        )
    if type_ == "database":
        return str(config.get("connection", "?"))[:60]
    return str(config)[:60]


def cmd_covenant_list(args: argparse.Namespace) -> int:
    store = _open_covenant_store()
    records = store.list(domain=args.domain)
    if not records:
        scope = f"domain={args.domain}" if args.domain else "all"
        print(f"No covenants registered ({scope})")
        return 0

    title = f"[K2G Covenants] domain: {args.domain or '(ALL)'}"
    print(title)
    header = f"{'source_id':<16}{'type':<12}{'config summary':<48}{'state':<6}"
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    for r in records:
        state = "on" if r.enabled else "off"
        summary = _summarize_config(r.type, r.config)
        if len(summary) > 46:
            summary = summary[:45] + "…"
        print(f"{r.source_id:<16}{r.type:<12}{summary:<48}{state:<6}")
    return 0


def cmd_covenant_add(args: argparse.Namespace) -> int:
    from k2g.stores.covenant_store import CovenantRecord

    store = _open_covenant_store()
    try:
        config = _parse_config_arg(args.config)
    except Exception as e:  # noqa: BLE001
        _print_error(f"config parse failed: {e}")
        return 1

    record = CovenantRecord(
        domain=args.domain,
        source_id=args.source_id,
        type=args.type,
        config=config,
        group_policy=args.group_policy,
        description=args.description,
    )
    try:
        new_id = store.add(record)
    except ValueError as e:
        _print_error(str(e))
        return 1
    print(f"registered: id={new_id} domain={args.domain} source_id={args.source_id}")
    return 0


def cmd_covenant_remove(args: argparse.Namespace) -> int:
    store = _open_covenant_store()
    if store.remove(args.domain, args.source_id):
        print(f"removed: domain={args.domain} source_id={args.source_id}")
        return 0
    print(f"covenant not found: domain={args.domain} source_id={args.source_id}")
    return 1


def cmd_covenant_enable(args: argparse.Namespace) -> int:
    store = _open_covenant_store()
    if store.set_enabled(args.domain, args.source_id, True):
        print(f"enabled: domain={args.domain} source_id={args.source_id}")
        return 0
    _print_error("covenant not found")
    return 1


def cmd_covenant_disable(args: argparse.Namespace) -> int:
    store = _open_covenant_store()
    if store.set_enabled(args.domain, args.source_id, False):
        print(f"disabled: domain={args.domain} source_id={args.source_id}")
        return 0
    _print_error("covenant not found")
    return 1


def cmd_covenant_validate(args: argparse.Namespace) -> int:
    from k2g.admin.covenant_validator import CovenantValidator

    store = _open_covenant_store()
    records = store.list(domain=args.domain, enabled_only=False)
    if not records:
        print("no covenants registered")
        return 0

    results = CovenantValidator().validate(records)
    has_fatal = False
    has_error = False
    for r in results:
        label = {"ok": "OK ", "warning": "WARN", "error": "ERR ", "fatal": "FATAL"}[r.severity]
        print(f"  [{label}] {r.record.domain}/{r.record.source_id}  {r.detail}")
        if r.severity == "fatal":
            has_fatal = True
        elif r.severity == "error":
            has_error = True

    if has_fatal:
        return 2
    if has_error:
        return 1
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="k2g", description="K2G Knowledge Graph CLI")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command")

    # search-entity
    p_search = sub.add_parser("search-entity", help="Search entities by name substring")
    p_search.add_argument("name", help="Name substring (case-insensitive LIKE)")
    p_search.add_argument("--domain", default=None, help="Filter domain")
    p_search.set_defaults(func=cmd_search_entity)

    # stats
    p_stats = sub.add_parser("stats", help="Node/event/group counts")
    p_stats.add_argument("--domain", default=None, help="Filter domain")
    p_stats.set_defaults(func=cmd_stats)

    # health
    p_health = sub.add_parser("health", help="DbStore backend ping")
    p_health.set_defaults(func=cmd_health)

    # train
    p_train = sub.add_parser("train", help="Run a single trainer phase")
    p_train.add_argument(
        "phase", choices=["jaccard", "hdbscan", "control_node"],
        help="Phase name",
    )
    p_train.add_argument("--domain", default=None, help="Target domain (default: all)")
    p_train.set_defaults(func=cmd_train)

    # covenant
    p_cov = sub.add_parser("covenant", help="Managed source covenant operations")
    cov_sub = p_cov.add_subparsers(dest="covenant_cmd")

    p_cov_list = cov_sub.add_parser("list", help="List registered covenants")
    p_cov_list.add_argument("--domain", default=None)
    p_cov_list.set_defaults(func=cmd_covenant_list)

    p_cov_add = cov_sub.add_parser("add", help="Register a new covenant")
    p_cov_add.add_argument("--domain", required=True)
    p_cov_add.add_argument("--source-id", required=True, dest="source_id")
    p_cov_add.add_argument(
        "--type", required=True, choices=["filesystem", "vcs", "database"],
    )
    p_cov_add.add_argument(
        "--config", required=True,
        help='JSON string or "@path/to/file.json"',
    )
    p_cov_add.add_argument(
        "--group-policy", required=True, choices=["path", "vcs", "db"],
        dest="group_policy",
    )
    p_cov_add.add_argument("--description", default=None)
    p_cov_add.set_defaults(func=cmd_covenant_add)

    p_cov_remove = cov_sub.add_parser("remove", help="Remove a covenant")
    p_cov_remove.add_argument("--domain", required=True)
    p_cov_remove.add_argument("source_id")
    p_cov_remove.set_defaults(func=cmd_covenant_remove)

    p_cov_enable = cov_sub.add_parser("enable", help="Enable a disabled covenant")
    p_cov_enable.add_argument("--domain", required=True)
    p_cov_enable.add_argument("source_id")
    p_cov_enable.set_defaults(func=cmd_covenant_enable)

    p_cov_disable = cov_sub.add_parser("disable", help="Disable a covenant")
    p_cov_disable.add_argument("--domain", required=True)
    p_cov_disable.add_argument("source_id")
    p_cov_disable.set_defaults(func=cmd_covenant_disable)

    p_cov_validate = cov_sub.add_parser(
        "validate", help="Validate covenants against the current environment",
    )
    p_cov_validate.add_argument("--domain", default=None)
    p_cov_validate.set_defaults(func=cmd_covenant_validate)

    return parser


def main() -> int:
    # Reconfigure stdout/stderr to UTF-8 (handles Windows cp949 consoles)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    parser = _build_parser()
    args = parser.parse_args()

    # Delegate logging setup to observability.logging_config. --verbose -> DEBUG.
    try:
        from k2g.core.config import get_settings
        from k2g.observability.logging_config import configure_logging

        settings = get_settings()
        if args.verbose:
            settings = settings.model_copy(update={"log_level": "DEBUG"})
        configure_logging(settings, log_file_basename="k2g-cli", force=True)
    except Exception:
        # If settings fail to load, fall back to basicConfig and continue
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.WARNING,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    if not args.command:
        parser.print_help()
        return 0

    if not hasattr(args, "func"):
        parser.parse_args([args.command, "--help"])
        return 0

    rc = args.func(args)
    return int(rc) if isinstance(rc, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
