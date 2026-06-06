"""Staging layer Protocols and StagedRecord union.

Producers and loaders depend only on these Protocols, not on the concrete
StagingWriter or StagingLoader classes. The Protocols are runtime_checkable,
so existing classes conform automatically (isinstance checks work).

Adding a new backend (S3, Postgres staging table, etc.) only requires a new
implementation that satisfies these Protocols — no changes to producer or
loader code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Protocol, Union, runtime_checkable

if TYPE_CHECKING:
    import numpy as np

from k2g.staging.schema import (
    StagedEntity,
    StagedEvent,
    StagedGroup,
    StagingManifest,
)

# Union of all staging record types handled by producers and loaders.
StagedRecord = Union[StagedEvent, StagedEntity, StagedGroup]


@runtime_checkable
class StageWriter(Protocol):
    """Write contract for producers → staging. Implemented by StagingWriter.

    Used in the Producer ABC signature
    ``produce(unit, writer: StageWriter) -> ProduceResult``.
    Each producer calls only the write methods it needs.
    """

    def next_ref(self) -> str:
        """Issue a local_ref that is collision-free within the session/domain."""
        ...

    def write_event(
        self, event: StagedEvent, embedding: "np.ndarray | None" = None,
    ) -> None: ...

    def write_entity(self, entity: StagedEntity) -> None: ...

    def write_group(self, group: StagedGroup) -> None: ...

    def finalize_done(self) -> None:
        """Set produce.state = done and flush the manifest."""
        ...

    def finalize_failed(self, error: str) -> None:
        """Set produce.state = failed (with reason). The loader will skip this domain."""
        ...


@runtime_checkable
class StageReader(Protocol):
    """Read contract for loaders → staging. Implemented by StagingLoader.

    The loader consumes iter_* methods as streams to keep peak RSS bounded.
    Embeddings are memmap-backed and loaded into memory only when
    get_embedding(local_ref) is called.
    """

    def is_ready(self) -> bool:
        """Return True when produce.state == done."""
        ...

    def manifest(self) -> StagingManifest: ...

    def iter_events(self) -> Iterator[StagedEvent]: ...

    def iter_entities(self) -> Iterator[StagedEntity]: ...

    def iter_groups(self) -> Iterator[StagedGroup]: ...

    def get_embedding(self, local_ref: str) -> "np.ndarray | None": ...


__all__ = [
    "StagedRecord",
    "StageWriter",
    "StageReader",
]
