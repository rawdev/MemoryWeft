"""DataOwner dataclass + DDL constants.

Adds 5 columns to each primary table (events / entities / groups /
context_groups / k2g_covenant / k2g_source_file / event_template_groups
/ plan_nodes / plan_direction_nodes):

- ``owner_id``       TEXT NULL               -- future user_id (no FK)
- ``org_id``         TEXT NULL               -- future org_id
- ``visibility``     TEXT NOT NULL DEFAULT 'public'  -- public|private|shared
- ``acl_json``       TEXT NULL               -- future ACL representation
- ``share_group_id`` TEXT NULL               -- share group reference

The producer attaches ``DataOwner`` values to each record at INSERT
time.  The RLS policy reads these columns to enforce access control.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

VisibilityLevel = Literal["public", "private", "shared"]


# ---------------------------------------------------------------------------
# Developer-only sentinel owner_id
# ---------------------------------------------------------------------------
#
# Reserved value used in place of owner_id before a real user system
# (k2g_user table + OAuth) is available -- for dev / test / single-user
# operation.
#
# Format: ``sub:<id>`` -- same namespace as existing tests
# (`sub:alice`, `sub:bob`).  `sub:dev` does not collide with real
# OAuth provider subjects (e.g. `google-oauth2|<num>`, `auth0|<num>`).
#
# When transitioning to production: introduce the `k2g_user` table and
# either keep a single `sub:dev` row or migrate all sentinel values to
# real user ids.
DEV_OWNER_ID: str = "sub:dev"


# ---------------------------------------------------------------------------
# DDL constants -- same column semantics across both backends
# ---------------------------------------------------------------------------

# Target tables (primary tables, excluding operational audit tables)
DATA_OWNER_TABLES: tuple[str, ...] = (
    "events",
    "entities",
    "groups",
    "context_groups",
    "k2g_covenant",
    "k2g_source_file",
    "event_template_groups",
    "plan_nodes",
    "plan_direction_nodes",
)

# Column names (used by Producer / RLS policy / query filter)
DATA_OWNER_COLUMNS: tuple[str, ...] = (
    "owner_id",
    "org_id",
    "visibility",
    "acl_json",
    "share_group_id",
)

# Operational audit / manifest tables -- *excluded* from column addition
DATA_OWNER_EXCLUDED: frozenset[str] = frozenset({
    "events_audit",
    "build_audit",
    "train_state_manifest",
    "build_failure_manifest",
    "build_file_manifest",
    "build_segment_manifest",
    "k2g_covenant_history",
    "cluster_narrative_cache",
    "k2g_share_audit",
    "k2g_sql_audit",
})

# Edge tables -- no columns added (visibility inherited from endpoints)
DATA_OWNER_EDGE_INHERIT: frozenset[str] = frozenset({
    "participated_in",
    "event_member_of",
    "event_sequential_next",
    "event_jaccard_connected",
    "entity_connection",
    "event_belongs_to_context",
    "entity_embedding_meta",
    "plan_from",
    "plan_next",
    "realized_as",
    "raw_archive_ref",
})


# ---------------------------------------------------------------------------
# DataOwner dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataOwner:
    """Ownership + visibility + ACL for a data row.

    Attached to each record by the producer at INSERT time.  The RLS
    policy (Postgres) or app-level filter (SQLite) inspects these
    values to determine access.

    Attrs:
        owner_id: OAuth subject that will map to ``k2g_user.id``.
            None means unspecified.
        org_id: Future ``k2g_org.id``.  None means unspecified.
        visibility: ``public`` (default -- anyone can read),
            ``private`` (owner only), ``shared`` (share_group members).
        acl: Future ACL in ``{"users":[], "orgs":[], "roles":[]}``
            form.  None means ACL is not used (visibility /
            share_group only).
        share_group_id: ``k2g_share_group.id``.  Required when
            visibility is ``shared``.
    """

    owner_id: str | None = None
    org_id: str | None = None
    visibility: VisibilityLevel = "public"
    acl: dict[str, Any] | None = None
    share_group_id: str | None = None

    @classmethod
    def public(cls) -> "DataOwner":
        """Default -- public to everyone. Fallback for existing builds."""
        return cls()

    @classmethod
    def private_to(cls, owner_id: str) -> "DataOwner":
        """Private to a single user."""
        return cls(owner_id=owner_id, visibility="private")

    @classmethod
    def shared_in(
        cls,
        share_group_id: str,
        owner_id: str | None = None,
    ) -> "DataOwner":
        """Visible only to share_group members."""
        return cls(
            owner_id=owner_id,
            visibility="shared",
            share_group_id=share_group_id,
        )

    def to_columns(self) -> dict[str, Any]:
        """Convert to column values for producer INSERT.

        Returns:
            ``{owner_id, org_id, visibility, acl_json, share_group_id}``
            -- matching schema column names.  ``acl`` is serialized as
            a JSON string.
        """
        return {
            "owner_id": self.owner_id,
            "org_id": self.org_id,
            "visibility": self.visibility,
            "acl_json": (
                None if self.acl is None
                else json.dumps(self.acl, ensure_ascii=False, sort_keys=True)
            ),
            "share_group_id": self.share_group_id,
        }

    def merged_with(self, other: "DataOwner | None") -> "DataOwner":
        """Merge two owners -- caller (other) takes priority.

        Used when the producer receives an explicit per-record owner
        to overlay on the default (self).  None fields in ``other``
        are preserved from ``self``.
        """
        if other is None:
            return self
        return DataOwner(
            owner_id=other.owner_id if other.owner_id is not None else self.owner_id,
            org_id=other.org_id if other.org_id is not None else self.org_id,
            visibility=other.visibility,  # always override (default 'public')
            acl=other.acl if other.acl is not None else self.acl,
            share_group_id=(
                other.share_group_id if other.share_group_id is not None
                else self.share_group_id
            ),
        )


# ---------------------------------------------------------------------------
# DDL helpers -- generate ALTER statements per backend
# ---------------------------------------------------------------------------


def sqlite_alter_statements(table: str) -> list[str]:
    """5 SQLite ALTER TABLE statements (idempotent ALTER handled in graph.py)."""
    return [
        f"ALTER TABLE {table} ADD COLUMN owner_id TEXT",
        f"ALTER TABLE {table} ADD COLUMN org_id TEXT",
        f"ALTER TABLE {table} ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public'",
        f"ALTER TABLE {table} ADD COLUMN acl_json TEXT",
        f"ALTER TABLE {table} ADD COLUMN share_group_id TEXT",
    ]


def postgres_alter_statements(table: str) -> list[str]:
    """Postgres ALTER TABLE -- idempotent via IF NOT EXISTS."""
    return [
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS owner_id VARCHAR(128)",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS org_id VARCHAR(128)",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS visibility VARCHAR(16) "
        f"NOT NULL DEFAULT 'public'",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS acl_json JSONB",
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS share_group_id VARCHAR(128)",
    ]


def apply_data_owner_alters_sqlite(cur: Any) -> int:
    """ALTER 5 columns on 9 tables via SQLite cursor (idempotent).

    Called at the end of setup_schema / setup_training_schema /
    setup_bp30_schema.  Only processes tables that already exist;
    skips columns that are already present.

    Returns:
        Number of columns actually added (for testing/debugging).
    """
    import logging
    import sqlite3
    log = logging.getLogger(__name__)

    added = 0
    for table in DATA_OWNER_TABLES:
        # Check if table exists
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        )
        if cur.fetchone() is None:
            continue
        cur.execute(f"PRAGMA table_info({table})")
        existing = {r["name"] if hasattr(r, "keys") else r[1]
                    for r in cur.fetchall()}
        for stmt in sqlite_alter_statements(table):
            col = stmt.split("ADD COLUMN ")[1].split()[0]
            if col in existing:
                continue
            try:
                cur.execute(stmt)
                added += 1
            except sqlite3.OperationalError as exc:
                log.warning("SQLite %s.%s ALTER skip: %s", table, col, exc)
    return added


def apply_data_owner_alters_postgres(cur: Any) -> int:
    """ALTER 5 columns on 9 tables via Postgres cursor (IF NOT EXISTS)."""
    import logging
    log = logging.getLogger(__name__)

    added = 0
    for table in DATA_OWNER_TABLES:
        # Check if table exists
        cur.execute(
            "SELECT to_regclass(%s) AS t",
            (f"public.{table}",),
        )
        row = cur.fetchone()
        # RealDictCursor -> dict, standard cursor -> tuple
        existing = (row["t"] if isinstance(row, dict) else row[0]) if row else None
        if existing is None:
            continue
        for stmt in postgres_alter_statements(table):
            try:
                cur.execute(stmt)
                added += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Postgres %s ALTER skip: %s", table, exc)
    return added


__all__ = [
    "DataOwner",
    "VisibilityLevel",
    "DEV_OWNER_ID",
    "DATA_OWNER_TABLES",
    "DATA_OWNER_COLUMNS",
    "DATA_OWNER_EXCLUDED",
    "DATA_OWNER_EDGE_INHERIT",
    "sqlite_alter_statements",
    "postgres_alter_statements",
    "apply_data_owner_alters_sqlite",
    "apply_data_owner_alters_postgres",
]
