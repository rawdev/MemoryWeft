"""groups 유일성 = (name, domain, type) — 전역 name UNIQUE 가 아니다.

전역 `name UNIQUE` 는 멀티-도메인 설치를 표현할 수 없다: 같은 태그 이름이 두
도메인에 정당하게 존재하고, 서버측 'system' 미러가 동명 사용자 태그를 정당하게
가린다. 실사용에서 `UNIQUE constraint failed: groups.name` 으로 아카이브 복원이
막혔다.

레거시 DB(전역 UNIQUE)는 setup_schema 가 테이블을 재생성해 옮긴다 -- SQLite 는
컬럼 레벨 UNIQUE 를 drop 할 수 없기 때문.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from k2g.db_store.sqlite.graph import SqliteGraphStore

_INSERT = (
    "INSERT INTO groups (id, name, level, domain, source, type, deprecated,"
    " created_at) VALUES (?, ?, 0, ?, ?, ?, 0, '2026-07-28T00:00:00Z')"
)


def _unique_indexes(conn: sqlite3.Connection) -> list[list[str]]:
    out = []
    for row in conn.execute("PRAGMA index_list(groups)"):
        if not row[2]:
            continue
        out.append([r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})")])
    return out


@pytest.fixture()
def store(tmp_path: Path):
    g = SqliteGraphStore(str(tmp_path / "g.db"))
    yield g
    g._conn.close()


def test_unique_key_is_name_domain_type(store) -> None:
    idx = _unique_indexes(store._conn)
    assert ["name", "domain", "type"] in idx
    assert ["name"] not in idx, "전역 name UNIQUE 가 남아 있으면 안 된다"


def test_same_name_across_domains(store) -> None:
    c = store._conn
    c.execute(_INSERT, ("g1", "work", "alpha", "user", "user"))
    c.execute(_INSERT, ("g2", "work", "beta", "user", "user"))
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM groups WHERE name='work'").fetchone()[0] == 2


def test_system_mirror_coexists_with_user_tag(store) -> None:
    """BP-118 §11 — 같은 도메인에서 type 만 다른 두 행이 공존해야 한다."""
    c = store._conn
    c.execute(_INSERT, ("g1", "rawdev", "d", "system", "system"))
    c.execute(_INSERT, ("g2", "rawdev", "d", "mweft_save_tag", "user"))
    c.commit()
    rows = sorted(
        r[0] for r in c.execute("SELECT type FROM groups WHERE name='rawdev'")
    )
    assert rows == ["system", "user"]


def test_exact_triple_still_rejected(store) -> None:
    """완화가 아니라 *스코프 변경* 이다 — 3중키가 같으면 여전히 거부."""
    c = store._conn
    c.execute(_INSERT, ("g1", "dup", "d", "user", "user"))
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(_INSERT, ("g2", "dup", "d", "user", "user"))


def test_type_defaults_to_user(store) -> None:
    c = store._conn
    c.execute(
        "INSERT INTO groups (id, name, level, domain, deprecated, created_at)"
        " VALUES ('g1', 'x', 0, 'd', 0, '2026-07-28T00:00:00Z')"
    )
    c.commit()
    assert c.execute("SELECT type FROM groups WHERE id='g1'").fetchone()[0] == "user"


# --- 레거시 DB 마이그레이션 ------------------------------------------------

_LEGACY_DDL = """
CREATE TABLE groups (
    id             TEXT    PRIMARY KEY,
    name           TEXT    NOT NULL UNIQUE,
    level          INTEGER,
    domain         TEXT    NOT NULL,
    parent_id      TEXT    REFERENCES groups(id),
    discriminator  TEXT,
    original_name  TEXT,
    source         TEXT,
    user_tag       TEXT,
    summary        TEXT,
    deprecated     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def test_legacy_global_unique_is_migrated(tmp_path: Path) -> None:
    """전역 UNIQUE 로 만들어진 기존 DB 가 데이터를 보존한 채 옮겨져야 한다."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(_LEGACY_DDL)
    raw.execute(
        "INSERT INTO groups (id, name, level, domain, source, deprecated,"
        " created_at) VALUES ('old1', 'kept', 0, 'd', 'user', 0, '2026-01-01')"
    )
    raw.commit()
    raw.close()

    g = SqliteGraphStore(str(path))       # setup_schema 가 마이그레이션 수행
    try:
        idx = _unique_indexes(g._conn)
        assert ["name"] not in idx
        assert ["name", "domain", "type"] in idx
        row = g._conn.execute(
            "SELECT name, domain, type, source FROM groups WHERE id='old1'"
        ).fetchone()
        assert row[0] == "kept" and row[1] == "d"
        assert row[2] == "user", "기존 행은 DEFAULT type 을 받아야 한다"
        assert row[3] == "user", "기존 컬럼 값이 보존돼야 한다"
        # 마이그레이션 후 실제로 동명 공존이 가능해야 의미가 있다
        g._conn.execute(_INSERT, ("new1", "kept", "d", "system", "system"))
        g._conn.commit()
        assert g._conn.execute(
            "SELECT COUNT(*) FROM groups WHERE name='kept'"
        ).fetchone()[0] == 2
    finally:
        g._conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(_LEGACY_DDL)
    raw.commit()
    raw.close()
    for _ in range(2):
        g = SqliteGraphStore(str(path))
        g._conn.close()
    g = SqliteGraphStore(str(path))
    try:
        assert ["name", "domain", "type"] in _unique_indexes(g._conn)
        # 재생성 잔재가 남으면 안 된다
        left = g._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name LIKE '%legacy_unique%'"
        ).fetchall()
        assert left == []
    finally:
        g._conn.close()
