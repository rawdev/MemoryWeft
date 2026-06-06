"""Producer module base contract (Protocol + shared dataclasses).

Producer implementations convert source material (md / pdf / pptx /
doxygen XML / git commit, etc.) into staging records, append them via
`StageWriter`, and then load them through `StageReader` -> `DbStore`
in a separate subprocess.  They do not interact directly with anything
outside db_store / staging.

This Protocol generalises the legacy `DoxygenInputRunner`,
`NovelInputRunner`, `VcsInputRunner`, and
`ingest_doc_batch.run_phase_produce/load`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Protocol, runtime_checkable

if TYPE_CHECKING:
    from k2g.core.config import DomainConfig
    from k2g.db_store import DbStore
    from k2g.staging import StageReader, StageWriter


@dataclass
class Scope:
    """Processing scope: data_root + include/exclude glob + sample_limit.

    ``config_overrides`` is a copy of PipelineStep.config_overrides,
    serving as the channel for step-level options (e.g. VCS branch/since,
    novel divider config).  The orchestrator's ``_run_produce`` /
    ``_run_load`` copies it automatically.
    """

    data_root: Path
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    sample_limit: int | None = None
    config_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProduceResult:
    """Result of producing a single WorkUnit."""

    success: bool
    local_ref: str | None = None
    entity_count: int = 0
    group_count: int = 0
    error: str | None = None
    skipped: bool = False  # incremental skip


@dataclass
class LoadResult:
    """Load result for the entire session."""

    success_count: int = 0
    fail_count: int = 0
    event_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Only populated by VCS; other producers leave as None.
    jaccard_edges: int | None = None


@runtime_checkable
class Producer(Protocol):
    """Producer contract -- discover / produce / load in three phases.

    The ``kind`` identifier is used by the planner/executor when
    assembling subprocesses.
    """

    kind: str

    def discover(
        self, config: "DomainConfig", scope: Scope
    ) -> Iterator[Any]:
        """Iterate over WorkUnits to process. Concrete type is a per-producer dataclass."""
        ...

    def produce(
        self,
        unit: Any,
        *,
        writer: "StageWriter",
        updater: Any | None = None,
    ) -> ProduceResult:
        """Write a single WorkUnit to staging. Does not access DbStore."""
        ...

    def load(
        self,
        reader: "StageReader",
        db: "DbStore",
        *,
        domain_config: "DomainConfig",
        updater: Any | None = None,
    ) -> LoadResult:
        """Load entire session staging into DbStore."""
        ...


__all__ = [
    "Producer",
    "Scope",
    "ProduceResult",
    "LoadResult",
]
