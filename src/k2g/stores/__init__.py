"""k2g.stores — auxiliary stores remaining after module restructuring.

Currently maintained:
    covenant_store.py   — legacy SQLite-only CRUD (separate covenant.db).
                          Used by covenant_validator / plan_policy. Kept for
                          backward compatibility.
    covenant_meta.py    — backend-agnostic store (sqlite + postgres).
                          Uses k2g_covenant etc. 3 tables in the main DB.

Main backend usage paths (reference):
    from k2g.db_store import DbStore
    from k2g.db_store.postgres.graph import PostgresGraphStore
    from k2g.db_store.sqlite.graph import SqliteGraphStore
    from k2g.db_store.postgres.vector import PgVectorStore
    from k2g.db_store.sqlite.vector import SqliteVectorStore
    from k2g.db_store.postgres.content import PostgresContentStore
    from k2g.db_store.sqlite.content import SqliteContentStore
    from k2g.db_store.object.local import LocalObjectStorage
"""

from k2g.stores.covenant_meta import CovenantMetaStore, covenant_meta_store_for
from k2g.stores.covenant_store import CovenantRecord, CovenantStore

__all__ = [
    "CovenantMetaStore",
    "covenant_meta_store_for",
    "CovenantRecord",
    "CovenantStore",
]
