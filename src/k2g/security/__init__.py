"""k2g.security — Data Owner / Visibility / RLS.

Per-domain data ownership + access control. RLS (Row-Level Security) enforces
permissions automatically at the *read query* stage.

Phase A — DataOwner dataclass + 5-column schema (this module)
Phase B — ShareGroupStore + member + audit
Phase C — RLS policy + sqlite app-level filter + cross-domain primitives
Phase D — LLM free-form SQL MCP tool + safety layer
"""

from k2g.security.data_owner import DATA_OWNER_COLUMNS, DataOwner
from k2g.security.share_store import (
    ShareGroupRecord,
    ShareGroupStore,
    ShareMemberRecord,
    share_group_store_for,
)

__all__ = [
    "DataOwner",
    "DATA_OWNER_COLUMNS",
    "ShareGroupStore",
    "ShareGroupRecord",
    "ShareMemberRecord",
    "share_group_store_for",
]
