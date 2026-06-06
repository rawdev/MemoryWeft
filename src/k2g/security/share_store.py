"""Backend-agnostic Share Group + Member + Audit store.

Domain-internal share groups + membership (share_member) + change audit.
Adding/removing a member = 1 INSERT/DELETE affecting N data rows
(data rows themselves are not modified).

Public API:
- ``ShareGroupStore`` — backend-agnostic CRUD
- ``ShareGroupRecord`` / ``ShareMemberRecord`` — frozen dataclass
- ``share_group_store_for(graph)`` — factory (same pattern as
  covenant_meta_store_for)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

VALID_MEMBER_KINDS = frozenset({"user", "org", "public"})
VALID_ROLES = frozenset({"reader", "writer", "owner"})

MemberKind = Literal["user", "org", "public"]
Role = Literal["reader", "writer", "owner"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShareGroupRecord:
    id: str
    domain: str
    name: str
    description: str | None = None
    owner_id: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ShareMemberRecord:
    share_group_id: str
    member_kind: str
    member_id: str
    role: str = "reader"
    added_at: str = ""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ShareGroupStore:
    """Supports both SQLite and Postgres backends.

    Same pattern as ``CovenantMetaStore``: reuses graph_store's _conn +
    branches on backend-specific placeholder (``?`` vs ``%s``).
    """

    def __init__(
        self,
        conn: Any,
        *,
        backend: Literal["sqlite", "postgres"] = "sqlite",
    ) -> None:
        self._conn = conn
        self._backend = backend
        self._ph = "?" if backend == "sqlite" else "%s"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _commit(self) -> None:
        self._conn.commit()

    def _rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:
            pass

    def _serialize_json(self, value: dict | None) -> Any:
        if value is None:
            return None
        if self._backend == "postgres":
            from psycopg2.extras import Json
            return Json(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _row_to_group(self, row: Any) -> ShareGroupRecord:
        get = (lambda k: row[k]) if hasattr(row, "keys") else (lambda k: row[k])
        created = row["created_at"]
        updated = row["updated_at"]
        if isinstance(created, datetime):
            created = created.isoformat()
        if isinstance(updated, datetime):
            updated = updated.isoformat()
        return ShareGroupRecord(
            id=row["id"],
            domain=row["domain"],
            name=row["name"],
            description=row["description"],
            owner_id=row["owner_id"],
            created_at=created or "",
            updated_at=updated or "",
        )

    def _row_to_member(self, row: Any) -> ShareMemberRecord:
        added = row["added_at"]
        if isinstance(added, datetime):
            added = added.isoformat()
        return ShareMemberRecord(
            share_group_id=row["share_group_id"],
            member_kind=row["member_kind"],
            member_id=row["member_id"],
            role=row["role"],
            added_at=added or "",
        )

    # ------------------------------------------------------------------
    # Group CRUD
    # ------------------------------------------------------------------

    def create_group(
        self,
        record: ShareGroupRecord,
        *,
        actor_id: str | None = None,
    ) -> str:
        """Create a new share_group and append an audit record."""
        now = _now()
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO k2g_share_group "
                f"(id, domain, name, description, owner_id, "
                f" created_at, updated_at) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph}, {self._ph}, {self._ph})",
                (
                    record.id, record.domain, record.name,
                    record.description, record.owner_id,
                    now, now,
                ),
            )
            self._append_audit(
                cur, action="create_group",
                share_group_id=record.id,
                actor_id=actor_id,
                before=None,
                after={
                    "domain": record.domain,
                    "name": record.name,
                    "owner_id": record.owner_id,
                },
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()
        return record.id

    def get_group(self, group_id: str) -> ShareGroupRecord | None:
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT * FROM k2g_share_group WHERE id = {self._ph}",
            (group_id,),
        )
        row = cur.fetchone()
        cur.close()
        return self._row_to_group(row) if row else None

    def list_groups(
        self,
        domain: str | None = None,
    ) -> list[ShareGroupRecord]:
        cur = self._conn.cursor()
        if domain is None:
            cur.execute("SELECT * FROM k2g_share_group ORDER BY domain, id")
        else:
            cur.execute(
                f"SELECT * FROM k2g_share_group WHERE domain = {self._ph} "
                f"ORDER BY id",
                (domain,),
            )
        rows = cur.fetchall()
        cur.close()
        return [self._row_to_group(r) for r in rows]

    def delete_group(
        self,
        group_id: str,
        *,
        actor_id: str | None = None,
    ) -> bool:
        existing = self.get_group(group_id)
        if existing is None:
            return False
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"DELETE FROM k2g_share_group WHERE id = {self._ph}",
                (group_id,),
            )
            self._append_audit(
                cur, action="delete_group",
                share_group_id=group_id,
                actor_id=actor_id,
                before={
                    "domain": existing.domain,
                    "name": existing.name,
                },
                after=None,
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()
        return True

    # ------------------------------------------------------------------
    # Member CRUD (real-time add / remove)
    # ------------------------------------------------------------------

    def add_member(
        self,
        record: ShareMemberRecord,
        *,
        actor_id: str | None = None,
    ) -> bool:
        """Add a member — 1 INSERT immediately affects N data rows."""
        if record.member_kind not in VALID_MEMBER_KINDS:
            raise ValueError(
                f"invalid member_kind: {record.member_kind} "
                f"(valid: {sorted(VALID_MEMBER_KINDS)})",
            )
        if record.role not in VALID_ROLES:
            raise ValueError(
                f"invalid role: {record.role} "
                f"(valid: {sorted(VALID_ROLES)})",
            )
        # Check for existing membership
        existing = self.get_member(
            record.share_group_id, record.member_kind, record.member_id,
        )
        if existing is not None:
            return False  # already member, no-op

        now = _now()
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO k2g_share_member "
                f"(share_group_id, member_kind, member_id, role, added_at) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
                f"{self._ph})",
                (
                    record.share_group_id, record.member_kind,
                    record.member_id, record.role, now,
                ),
            )
            self._append_audit(
                cur, action="add_member",
                share_group_id=record.share_group_id,
                member_kind=record.member_kind,
                member_id=record.member_id,
                actor_id=actor_id,
                before=None,
                after={"role": record.role},
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()
        return True

    def remove_member(
        self,
        share_group_id: str,
        member_kind: str,
        member_id: str,
        *,
        actor_id: str | None = None,
    ) -> bool:
        existing = self.get_member(share_group_id, member_kind, member_id)
        if existing is None:
            return False

        cur = self._conn.cursor()
        try:
            cur.execute(
                f"DELETE FROM k2g_share_member "
                f"WHERE share_group_id = {self._ph} "
                f"AND member_kind = {self._ph} AND member_id = {self._ph}",
                (share_group_id, member_kind, member_id),
            )
            self._append_audit(
                cur, action="remove_member",
                share_group_id=share_group_id,
                member_kind=member_kind,
                member_id=member_id,
                actor_id=actor_id,
                before={"role": existing.role},
                after=None,
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        finally:
            cur.close()
        return True

    def get_member(
        self,
        share_group_id: str,
        member_kind: str,
        member_id: str,
    ) -> ShareMemberRecord | None:
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT * FROM k2g_share_member "
            f"WHERE share_group_id = {self._ph} "
            f"AND member_kind = {self._ph} AND member_id = {self._ph}",
            (share_group_id, member_kind, member_id),
        )
        row = cur.fetchone()
        cur.close()
        return self._row_to_member(row) if row else None

    def list_members(
        self,
        share_group_id: str,
    ) -> list[ShareMemberRecord]:
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT * FROM k2g_share_member "
            f"WHERE share_group_id = {self._ph} "
            f"ORDER BY member_kind, member_id",
            (share_group_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return [self._row_to_member(r) for r in rows]

    def list_groups_for_user(
        self,
        member_kind: str,
        member_id: str,
    ) -> list[str]:
        """List all share_group_ids the user belongs to."""
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT share_group_id FROM k2g_share_member "
            f"WHERE member_kind = {self._ph} AND member_id = {self._ph}",
            (member_kind, member_id),
        )
        rows = cur.fetchall()
        cur.close()
        return [r["share_group_id"] for r in rows]

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def list_audit(
        self,
        share_group_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        cur = self._conn.cursor()
        if share_group_id is None:
            cur.execute(
                f"SELECT * FROM k2g_share_audit ORDER BY id DESC "
                f"LIMIT {self._ph}",
                (limit,),
            )
        else:
            cur.execute(
                f"SELECT * FROM k2g_share_audit "
                f"WHERE share_group_id = {self._ph} "
                f"ORDER BY id DESC LIMIT {self._ph}",
                (share_group_id, limit),
            )
        rows = cur.fetchall()
        cur.close()
        out = [dict(r) for r in rows]
        # Normalize JSON columns
        for item in out:
            for col in ("before_json", "after_json"):
                v = item.get(col)
                if isinstance(v, str):
                    try:
                        item[col] = json.loads(v)
                    except Exception:
                        pass
        return out

    def _append_audit(
        self,
        cur: Any,
        *,
        action: str,
        share_group_id: str | None,
        member_kind: str | None = None,
        member_id: str | None = None,
        actor_id: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
    ) -> None:
        before_param = self._serialize_json(before)
        after_param = self._serialize_json(after)
        cur.execute(
            f"INSERT INTO k2g_share_audit "
            f"(action, share_group_id, member_kind, member_id, actor_id, "
            f" before_json, after_json, acted_at) "
            f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, "
            f"{self._ph}, {self._ph}, {self._ph}, {self._ph})",
            (
                action, share_group_id, member_kind, member_id, actor_id,
                before_param, after_param, _now(),
            ),
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def share_group_store_for(graph_store: Any) -> ShareGroupStore:
    """Auto-detect backend (same pattern as covenant_meta_store_for)."""
    cls_name = type(graph_store).__name__
    if "Sqlite" in cls_name:
        backend = "sqlite"
    elif "Postgres" in cls_name:
        backend = "postgres"
    else:
        raise ValueError(
            f"unknown graph_store type: {cls_name} "
            "(expected SqliteGraphStore or PostgresGraphStore)",
        )
    conn = getattr(graph_store, "_conn", None)
    if conn is None:
        raise AttributeError(f"{cls_name} has no _conn attribute")
    return ShareGroupStore(conn, backend=backend)


__all__ = [
    "ShareGroupStore",
    "ShareGroupRecord",
    "ShareMemberRecord",
    "share_group_store_for",
    "VALID_MEMBER_KINDS",
    "VALID_ROLES",
]
