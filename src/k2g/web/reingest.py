"""Generated Event reingest -- BranchedEvent -> graph/vector single-shot load (P2).

Revives the legacy ``IngestionPipeline.ingest_text`` path ("wrap one text as
NEROutput and load into graph/vector") as a thin wrapper around the
reconstruct_module producer/loader shared helpers (``produce_ner_output`` +
``load_staged_event``).

**NER invocation is the caller's responsibility** -- the caller explicitly
provides entities (name+type) / groups / timestamp. When summary is omitted,
the first 80 chars of text are used. This module does not call an NER
provider; it trusts the entity list already determined by EventBrancher
via LLM.

Internal flow::

    Synthesize NEROutput
      -> produce_ner_output  (Object/Content put + StagedEvent capture)
        (in-memory StageWriter -- no file IO)
      -> load_staged_event   (graph + vector upsert)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from k2g.core.config import DomainConfig
from k2g.core.models import NEREntity, NEREvent, NERContent, NEROutput
from k2g.producer._shared import load_staged_event, produce_ner_output
from k2g.staging.schema import StagedEntity, StagedEvent, StagedGroup

if TYPE_CHECKING:
    import numpy as np

    from k2g.db_store import DbStore
    from k2g.embedding.client import EmbeddingClient

logger = logging.getLogger(__name__)


class _InMemoryStageWriter:
    """Minimal in-memory implementation of the StageWriter required by
    ``produce_ner_output``.

    Captures only StagedEvent + embedding in a single call without file IO.
    Since the result is passed directly to ``load_staged_event`` within the
    same function (bypassing the Loader pipeline), entity/group writes are
    not needed but are collected in lists to satisfy the protocol.
    """

    def __init__(self, ref: str = "rg_0001") -> None:
        self._ref = ref
        self.event: StagedEvent | None = None
        self.embedding: "np.ndarray | None" = None
        self.entities: list[StagedEntity] = []
        self.groups: list[StagedGroup] = []

    def next_ref(self) -> str:
        return self._ref

    def write_event(
        self, event: StagedEvent, embedding: "np.ndarray | None" = None,
    ) -> None:
        self.event = event
        self.embedding = embedding

    def write_entity(self, entity: StagedEntity) -> None:
        self.entities.append(entity)

    def write_group(self, group: StagedGroup) -> None:
        self.groups.append(group)

    def finalize_done(self) -> None:
        return None

    def finalize_failed(self, error: str) -> None:  # noqa: ARG002
        return None


@dataclass
class ReingestRequest:
    """Reingest call input -- caller fills in BranchedEvent + metadata."""

    text: str
    domain: str
    summary: str = ""
    timestamp: str = ""
    entities: list[dict[str, str]] = field(default_factory=list)
    """[{name, type}] -- defaults to 'unknown' when type is omitted."""
    groups: list[str] = field(default_factory=list)
    inline_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReingestResult:
    success: bool
    event_id: str = ""
    vector_id: str = ""
    error: str | None = None


def reingest_branched_event(
    request: ReingestRequest,
    *,
    db: "DbStore",
    embedding_client: "EmbeddingClient",
    domain_config: DomainConfig | None = None,
) -> ReingestResult:
    """Load a single BranchedEvent text into graph + vector.

    When domain_config is None, a minimal DomainConfig is synthesized
    from ``request.domain`` alone. produce_ner_output / load_staged_event
    only use ``config.domain`` and a few keys from ``config._raw``, so
    this is sufficient for the reingest path.
    """
    if not request.text or not request.text.strip():
        return ReingestResult(success=False, error="text is empty")
    if not request.domain or not request.domain.strip():
        return ReingestResult(success=False, error="domain is empty")

    cfg = domain_config or DomainConfig({"domain": request.domain})

    summary = (
        request.summary.strip()
        or request.text.strip().splitlines()[0][:80]
    )
    timestamp = (
        request.timestamp
        or datetime.now(timezone.utc).isoformat()
    )
    entities: list[NEREntity] = []
    for ent in request.entities or []:
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        etype = str(ent.get("type") or "unknown").strip() or "unknown"
        entities.append(NEREntity(name=name, type=etype))

    ner_output = NEROutput(
        entities=entities,
        event=NEREvent(summary=summary, timestamp=timestamp),
        groups=list(request.groups),
        content=NERContent(
            content_type="text/plain",
            data=request.text,
            inline_meta={
                **(request.inline_meta or {}),
                "reingest_source": "event_brancher",
            },
        ),
    )

    writer = _InMemoryStageWriter()
    local_ref = writer.next_ref()
    produce_res = produce_ner_output(
        ner_output,
        cfg,
        writer=writer,
        local_ref=local_ref,
        prev_local_ref=None,
        content=db.content,
        object_storage=db.object,
        embedding_client=embedding_client,
    )
    if not produce_res.success or writer.event is None or writer.embedding is None:
        return ReingestResult(
            success=False,
            error=f"produce failed: success={produce_res.success}",
        )

    ok, event_id, err = load_staged_event(
        writer.event,
        embedding=writer.embedding,
        domain_config=cfg,
        db=db,
        prev_event_id=None,
    )
    if not ok:
        return ReingestResult(success=False, error=err or "load failed")
    return ReingestResult(
        success=True,
        event_id=event_id,
        vector_id=writer.event.vector_id,
    )


__all__ = ["reingest_branched_event", "ReingestRequest", "ReingestResult"]
