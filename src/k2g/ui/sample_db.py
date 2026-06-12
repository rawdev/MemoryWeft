"""First-run sample project — seed a ready-made demo DB so a fresh install
opens with explorable data instead of an empty "create a database" prompt.

The sample is a single SQLite all-in-one DB (``k2g_all_in_one.db``, domain
``sample_work``) shipped under ``asset/mweft_sample/``. On the bootstrap first
run (no last-active project) the desktop launcher copies it into the bootstrap
project folder and configures that registry entry to point at it. A marker file
(``.mweft_sample.json``) records what was seeded so the Manager can show a
one-time "sample created" intro popup, then never again.

Locating the asset spans two layouts:
- dev checkout: ``<repo>/asset/mweft_sample/k2g_all_in_one.db``
- portable bundle: the launcher exports ``MWEFT_SAMPLE_DIR=<bundle>/asset/mweft_sample``

Seeding is strictly non-destructive: it never overwrites an existing DB, so a
real project (or an already-seeded sample) is left untouched and the intro
fires only once.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_DB_NAME = "k2g_all_in_one.db"
SAMPLE_DOMAIN = "sample_work"
SAMPLE_MARKER_NAME = ".mweft_sample.json"
SAMPLE_PROJECT_NAME = "MemoryWeft Sample"


def sample_db_asset() -> Path | None:
    """Locate the bundled sample DB across dev + portable layouts.

    Order: explicit ``MWEFT_SAMPLE_DB`` (full file path) → ``MWEFT_SAMPLE_DIR``
    (portable launcher) → repo-root ``asset/`` (dev checkout). ``None`` when no
    asset is present (e.g. an online portable build that skipped bundling it).
    """
    env_file = os.environ.get("MWEFT_SAMPLE_DB", "").strip()
    if env_file:
        p = Path(env_file).expanduser()
        if p.is_file():
            return p
    env_dir = os.environ.get("MWEFT_SAMPLE_DIR", "").strip()
    if env_dir:
        p = Path(env_dir).expanduser() / SAMPLE_DB_NAME
        if p.is_file():
            return p
    # Dev checkout: the k2g package is <repo>/src/k2g, so the asset is three
    # parents up (ui/ -> k2g -> src -> repo) / asset/mweft_sample.
    try:
        repo = Path(__file__).resolve().parents[3]
        p = repo / "asset" / "mweft_sample" / SAMPLE_DB_NAME
        if p.is_file():
            return p
    except IndexError:
        pass
    return None


def _db_counts(db_path: Path) -> dict[str, Any]:
    """Best-effort row counts + dominant domain for the intro popup."""
    out: dict[str, Any] = {"domain": SAMPLE_DOMAIN}
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        logger.debug("sample db open failed: %s", exc)
        return out
    try:
        cur = conn.cursor()
        for key, sql in (
            ("events", "SELECT COUNT(*) FROM events"),
            ("entities", "SELECT COUNT(*) FROM entities"),
            ("groups", "SELECT COUNT(*) FROM groups"),
        ):
            try:
                out[key] = int(cur.execute(sql).fetchone()[0])
            except sqlite3.Error:
                out[key] = None
        try:
            row = cur.execute(
                "SELECT domain, COUNT(*) FROM events GROUP BY domain "
                "ORDER BY 2 DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                out["domain"] = str(row[0])
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    return out


def marker_path(project_dir: Path | str) -> Path:
    return Path(project_dir) / SAMPLE_MARKER_NAME


def read_marker(project_dir: Path | str) -> dict[str, Any] | None:
    """Return the sample marker for ``project_dir`` (None if not sample-seeded)."""
    p = marker_path(project_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def seed_sample_project(project_dir: Path | str) -> dict[str, Any] | None:
    """Copy the sample DB into ``project_dir`` and configure its registry entry.

    Returns the sample info dict (also written as the marker) on a fresh seed, or
    ``None`` when there is no asset to seed or the folder already holds a DB — so
    a real project is never clobbered and the intro never re-pops.
    """
    asset = sample_db_asset()
    if asset is None:
        return None
    project_dir = Path(project_dir)
    target = project_dir / SAMPLE_DB_NAME
    # Non-destructive: an existing DB means a real project or an already-seeded
    # sample. Leave it untouched.
    if target.exists():
        return None
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, target)
    except OSError as exc:
        logger.warning("sample seed copy failed (%s -> %s): %s", asset, target, exc)
        return None

    info = _db_counts(target)
    info.update({"db_path": str(target), "data_dir": str(project_dir)})

    # Configure the registry entry so the sample domain is the active save/search
    # target. (Without this the entry would read domain="default" and search the
    # wrong/empty domain.)
    try:
        from k2g.ui.project_registry import (
            add_project,
            find_by_dir,
            update_project,
        )

        entry = find_by_dir(project_dir)
        if entry is None:
            entry = add_project(
                name=SAMPLE_PROJECT_NAME,
                project_dir=str(project_dir),
                db_dir=str(project_dir),
            )
        domain = str(info.get("domain") or SAMPLE_DOMAIN)
        provider = os.environ.get("EMBEDDING_PROVIDER") or "local"
        update_project(
            slug=entry.slug,
            domain=domain,
            group="default",
            backend="sqlite",
            search_targets=[{"domain": domain}],
            # The sample DB is built with BAAI/bge-m3 @ dim 1024 (k2g_meta);
            # match it so query embeddings line up with the stored vectors.
            embedding={"provider": provider, "model": "BAAI/bge-m3", "dim": 1024},
        )
        info["slug"] = entry.slug
    except Exception as exc:  # noqa: BLE001 — seeding is best-effort
        logger.warning("sample registry config failed: %s", exc)

    try:
        marker_path(project_dir).write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("sample marker write failed: %s", exc)

    logger.info(
        "Seeded sample project at %s (domain=%s, events=%s, entities=%s)",
        project_dir, info.get("domain"), info.get("events"), info.get("entities"),
    )
    return info


__all__ = [
    "SAMPLE_DB_NAME",
    "SAMPLE_DOMAIN",
    "SAMPLE_MARKER_NAME",
    "sample_db_asset",
    "marker_path",
    "read_marker",
    "seed_sample_project",
]
