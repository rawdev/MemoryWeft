"""아카이브가 대상보다 컬럼이 적을 때 — DDL DEFAULT 가 살아나야 한다.

exporter 는 각 테이블을 *소스* DB 에 실재하는 컬럼으로 좁힌다
(``DomainExporter._present``). 그래서 owner-ACL 컬럼이 추가되기 전 스키마에서
내보낸 아카이브는 ``groups.visibility`` 같은 컬럼을 정당하게 빠뜨린다.

importer 가 schema.py 목록 그대로 INSERT 를 만들고 없는 컬럼에 NULL 을 바인딩하면
대상의 DEFAULT 가 무력화되고 NOT NULL 컬럼이 깨진다 -- 실사용에서 관측된
``NOT NULL constraint failed: groups.visibility`` 가 정확히 이것이다.
아카이브에 있는 컬럼만 INSERT 하면 대상이 자기 DEFAULT 를 적용한다.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from k2g.portable.archive_io import DirWriter
from k2g.portable.importer import DomainImporter, _effective_columns
from k2g.portable.manifest import (
    ArchiveManifest,
    ArchiveOptions,
    ArchiveSource,
    utc_now_iso,
)
from k2g.portable.schema import (
    ENTITIES_COLUMNS,
    ENTITY_CONNECTION_COLUMNS,
    EVENT_MEMBER_OF_COLUMNS,
    EVENT_SEQUENTIAL_NEXT_COLUMNS,
    EVENTS_COLUMNS,
    GROUPS_COLUMNS,
    PARTICIPATED_IN_COLUMNS,
    dumps,
)

DOMAIN = "drift"
_NOW = "2026-07-28T00:00:00Z"

# 관측된 실패 아카이브의 groups 헤더 그대로 — owner-ACL 컬럼이 없고 org_id 만 있다.
_LEAN_GROUPS = (
    "id", "name", "level", "domain", "parent_id", "discriminator",
    "original_name", "source", "user_tag", "summary", "deprecated",
    "created_at", "org_id",
)


def _write(root: Path) -> None:
    with DirWriter(root) as w:
        w.add_jsonl(
            "db/entities.jsonl", rows=iter([]),
            header={"table": "entities", "columns": list(ENTITIES_COLUMNS)},
            json_dumps=dumps,
        )
        w.add_jsonl(
            "db/events.jsonl", rows=iter([]),
            header={"table": "events", "columns": list(EVENTS_COLUMNS)},
            json_dumps=dumps,
        )
        # groups 만 좁은 컬럼 -- visibility 부재
        w.add_jsonl(
            "db/groups.jsonl",
            rows=iter([
                {"id": "grp_1", "name": "root", "level": 0, "domain": DOMAIN,
                 "parent_id": None, "discriminator": None, "original_name": None,
                 "source": "user", "user_tag": None, "summary": None,
                 "deprecated": False, "created_at": _NOW, "org_id": "org_x"},
            ]),
            header={"table": "groups", "columns": list(_LEAN_GROUPS)},
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
            w.add_jsonl(member, rows=iter([]),
                        header={"table": table, "columns": list(cols)},
                        json_dumps=dumps)
        w.add_text("manifest.json", ArchiveManifest(
            source=ArchiveSource(project=DOMAIN, group=DOMAIN, domain=DOMAIN),
            exported_at=utc_now_iso(),
            options=ArchiveOptions(
                include_vectors=False, include_derived=False,
                include_segments=False, include_plan=False, include_content=False,
            ),
            row_counts={"groups": 1},
        ).to_json())


@pytest.fixture()
def target(tmp_path: Path):
    from k2g.db_store.sqlite.graph import SqliteGraphStore

    g = SqliteGraphStore(str(tmp_path / "g.db"))
    yield g
    g._conn.close()


def test_missing_column_falls_back_to_ddl_default(tmp_path: Path, target) -> None:
    """핵심 회귀: 이 import 가 예전엔 NOT NULL 로 죽었다."""
    root = tmp_path / "arc"
    _write(root)
    DomainImporter(SimpleNamespace(_conn=target._conn), "sqlite").import_archive(
        root, mode="restore", confirm_replace=True,
    )
    row = target._conn.execute(
        "SELECT visibility, org_id, name FROM groups WHERE id = 'grp_1'"
    ).fetchone()
    assert row[0] == "public", "대상의 DDL DEFAULT 가 적용돼야 한다"
    assert row[1] == "org_x", "아카이브가 실은 컬럼은 그대로 들어가야 한다"
    assert row[2] == "root"


def test_effective_columns_narrows_to_archive_header(tmp_path: Path) -> None:
    root = tmp_path / "arc"
    _write(root)
    from k2g.portable.archive_io import DirReader

    with DirReader(root) as r:
        cols = _effective_columns(GROUPS_COLUMNS, r, "db/groups.jsonl")
    assert "visibility" not in cols
    assert "org_id" in cols and "id" in cols
    # 선언 순서는 유지 -- INSERT 컬럼/값 순서가 어긋나면 안 된다
    assert list(cols) == [c for c in GROUPS_COLUMNS if c in cols]


def test_full_header_is_left_alone(tmp_path: Path) -> None:
    """컬럼이 모두 있는 아카이브는 좁히지 않는다."""
    root = tmp_path / "arc"
    _write(root)
    from k2g.portable.archive_io import DirReader

    with DirReader(root) as r:
        cols = _effective_columns(ENTITIES_COLUMNS, r, "db/entities.jsonl")
    assert cols == ENTITIES_COLUMNS


def test_missing_header_falls_back_to_declared(tmp_path: Path) -> None:
    """헤더 없는 낡은 아카이브는 종전대로 전체 선언 목록을 쓴다."""
    root = tmp_path / "arc"
    root.mkdir(parents=True)
    (root / "db").mkdir()
    (root / "db" / "groups.jsonl").write_text(
        json.dumps({"id": "grp_1"}) + "\n", encoding="utf-8",
    )
    from k2g.portable.archive_io import DirReader

    with DirReader(root) as r:
        assert _effective_columns(
            GROUPS_COLUMNS, r, "db/groups.jsonl",
        ) == GROUPS_COLUMNS
