"""End-to-end: import an archive whose JSONL carries *nested* JSON values.

A Postgres-sourced archive is shaped differently from a SQLite-sourced one:
psycopg2 decodes every JSONB column into a Python ``dict``/``list``, and the
exporter writes those through verbatim.  This test builds such an archive by
hand — no PostgreSQL, no exporter — and restores it into a fresh SQLite target,
which is the exact path a self-hosted user takes when importing a hosted export.

Without the dict/list branch in ``denormalize_for_column`` this fails at the
first bulk insert with "type 'dict' is not supported".
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from k2g.portable.archive_io import DirWriter
from k2g.portable.importer import DomainImporter
from k2g.portable.manifest import (
    ArchiveManifest,
    ArchiveOptions,
    ArchiveSource,
    utc_now_iso,
)
from k2g.portable.schema import (
    CONTENT_STORE_COLUMNS,
    ENTITIES_COLUMNS,
    ENTITY_CONNECTION_COLUMNS,
    EVENT_MEMBER_OF_COLUMNS,
    EVENT_SEQUENTIAL_NEXT_COLUMNS,
    EVENTS_COLUMNS,
    GROUPS_COLUMNS,
    PARTICIPATED_IN_COLUMNS,
    dumps,
)

DOMAIN = "K2G"
_NOW = "2026-07-28T00:00:00Z"

# The JSONB payloads that arrive as Python objects from psycopg2.
ACL = {"grants": ["user@example.com"], "mode": "ro"}
LOCATOR = {"path": "docs/a.md", "line": 12}
BODY = {"content": "본문 текст body"}


def _row(columns: tuple[str, ...], **values: object) -> dict[str, object]:
    """Full row dict — every declared column present, unset ones None."""
    row: dict[str, object] = dict.fromkeys(columns)
    unknown = set(values) - set(columns)
    assert not unknown, f"not archive columns: {sorted(unknown)}"
    row.update(values)
    return row


def _write_archive(root: Path) -> None:
    with DirWriter(root) as w:
        w.add_jsonl(
            "db/entities.jsonl",
            rows=iter([
                _row(
                    ENTITIES_COLUMNS,
                    id="ent_1", name="MWeft", domain=DOMAIN, type="product",
                    deprecated=False, created_at=_NOW, visibility="public",
                    acl_json=ACL,          # <- nested dict (JSONB)
                ),
            ]),
            header={"table": "entities", "columns": list(ENTITIES_COLUMNS)},
            json_dumps=dumps,
        )
        w.add_jsonl(
            "db/events.jsonl",
            rows=iter([
                _row(
                    EVENTS_COLUMNS,
                    id="evt_1", domain=DOMAIN, summary="hello",
                    vector_id="evt_1", deprecated=False, influence_score=1.0,
                    created_at=_NOW, visibility="public",
                    acl_json=ACL,               # <- nested dict
                    source_locator=LOCATOR,     # <- nested dict
                ),
            ]),
            header={"table": "events", "columns": list(EVENTS_COLUMNS)},
            json_dumps=dumps,
        )
        w.add_jsonl(
            "db/groups.jsonl",
            rows=iter([]),
            header={"table": "groups", "columns": list(GROUPS_COLUMNS)},
            json_dumps=dumps,
        )
        for member, table, cols in (
            ("db/edges/participated_in.jsonl", "participated_in",
             PARTICIPATED_IN_COLUMNS),
            ("db/edges/event_member_of.jsonl", "event_member_of",
             EVENT_MEMBER_OF_COLUMNS),
            ("db/edges/event_sequential_next.jsonl", "event_sequential_next",
             EVENT_SEQUENTIAL_NEXT_COLUMNS),
            ("db/edges/entity_connection.jsonl", "entity_connection",
             ENTITY_CONNECTION_COLUMNS),
        ):
            w.add_jsonl(
                member, rows=iter([]),
                header={"table": table, "columns": list(cols)},
                json_dumps=dumps,
            )
        w.add_jsonl(
            "db/content_store.jsonl",
            rows=iter([
                _row(
                    CONTENT_STORE_COLUMNS,
                    content_id="cnt_1", domain=DOMAIN, vector_id="evt_1",
                    content_type="text/plain", storage_uri="inline://cnt_1",
                    created_at=_NOW,
                    inline_meta=BODY,        # <- nested dict
                ),
                _row(
                    CONTENT_STORE_COLUMNS,
                    content_id="cnt_2", domain=DOMAIN, vector_id="evt_1",
                    content_type="text/plain", storage_uri="inline://cnt_2",
                    created_at=_NOW,
                    inline_meta={},          # <- NOT NULL DEFAULT '{}' — falsy dict
                ),
            ]),
            header={"table": "content_store",
                    "columns": list(CONTENT_STORE_COLUMNS)},
            json_dumps=dumps,
        )
        manifest = ArchiveManifest(
            source=ArchiveSource(
                project=DOMAIN, group=DOMAIN, domain=DOMAIN,
                embedding={"provider": "local", "model": "BAAI/bge-m3"},
            ),
            exported_at=utc_now_iso(),
            options=ArchiveOptions(
                include_vectors=False, include_derived=False,
                include_segments=False, include_plan=False,
                include_content=True,
            ),
            row_counts={"entities": 1, "events": 1, "content_store": 2},
        )
        w.add_text("manifest.json", manifest.to_json())


@pytest.fixture()
def target(tmp_path: Path):
    """Fresh SQLite graph + content store, as ``mweft-init`` would create."""
    from k2g.db_store.sqlite.content import SqliteContentStore
    from k2g.db_store.sqlite.graph import SqliteGraphStore

    graph = SqliteGraphStore(str(tmp_path / "graph.db"))
    content = SqliteContentStore(str(tmp_path / "content.db"))
    yield graph, content
    graph._conn.close()
    content._conn.close()


def test_pg_shaped_archive_restores_into_sqlite(tmp_path: Path, target) -> None:
    graph, content = target
    root = tmp_path / "archive"
    _write_archive(root)

    importer = DomainImporter(
        SimpleNamespace(_conn=graph._conn), "sqlite", content_store=content,
    )
    result = importer.import_archive(
        root, mode="restore", confirm_replace=True,
    )
    assert result["manifest"].source.domain == DOMAIN

    conn = graph._conn
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    # Nested JSON survived as a JSON string, semantically intact.
    acl = conn.execute("SELECT acl_json FROM entities WHERE id='ent_1'").fetchone()[0]
    assert isinstance(acl, str)
    assert json.loads(acl) == ACL

    loc = conn.execute(
        "SELECT source_locator FROM events WHERE id='evt_1'"
    ).fetchone()[0]
    assert json.loads(loc) == LOCATOR

    rows = dict(content._conn.execute(
        "SELECT content_id, inline_meta FROM content_store"
    ).fetchall())
    assert json.loads(rows["cnt_1"]) == BODY
    assert json.loads(rows["cnt_2"]) == {}     # falsy dict must still be stored
