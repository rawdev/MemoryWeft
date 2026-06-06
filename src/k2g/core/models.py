"""
K2G core domain models (Pydantic v2).
All nodes and records follow the append-only principle.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# ID generation helpers
# ---------------------------------------------------------------------------

def _make_id(prefix: str) -> str:
    """Generate an ID in the format prefix_{uuid4}."""
    return f"{prefix}_{uuid.uuid4().hex}"


def new_entity_id() -> str:
    return _make_id("ent")


def new_event_id() -> str:
    return _make_id("evt")


def new_group_id() -> str:
    return _make_id("grp")


def new_content_id() -> str:
    return _make_id("cnt")


def new_vector_id() -> str:
    return _make_id("vec")


def make_sourcefile_id(covenant_id: str, relative_path: str) -> str:
    """Path-based deterministic SourceFile ID: sha256(covenant_id + relative_path)."""
    raw = f"{covenant_id}{relative_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Graph DB node models
# ---------------------------------------------------------------------------

class EntityNode(BaseModel):
    """
    Entity node in the graph DB.
    Link-or-Create policy: name + domain form the composite unique key.
    Nodes with the same name+domain are reused (Emergent Identity + Entity Dedup).
    type stores the NER extraction hint but is not used for identity resolution.
    """

    id: str = Field(default_factory=new_entity_id)
    name: str = Field(..., min_length=1, description="Entity name (e.g. Liu Bei)")
    type: str | None = Field(default=None, description="Entity type hint (PERSON, CLASS, etc.)")
    domain: str | None = Field(default=None, description="Owning domain")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"frozen": True}

    @field_validator("name", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class EventNode(BaseModel):
    """
    Event node in the graph DB.
    Represents a single narrative or technical event.
    Linked to the Vector DB via vector_id.
    """

    id: str = Field(default_factory=new_event_id)
    vector_id: str = Field(..., description="Record ID in the Vector DB")
    timestamp: str | None = Field(
        default=None,
        description="Event occurrence time (domain-specific format: historical year, ISO-8601, etc.)",
    )
    order_index: int = Field(
        default=0,
        ge=0,
        description="Event ordering index within the domain",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"frozen": True}


class GroupNode(BaseModel):
    """
    Group node in the graph DB.
    Forms the domain classification tree; hierarchy is expressed via CHILD_OF edges.
    """

    id: str = Field(default_factory=new_group_id)
    name: str = Field(..., min_length=1, description="Group name (e.g. Shu)")
    level: int = Field(..., ge=0, description="Tree depth (root=0)")
    domain: str = Field(..., description="Owning domain (e.g. three-kingdoms)")
    summary: str = Field(
        default="",
        description="Group hierarchy path summary (e.g. three-kingdoms > Shu)",
    )
    parent_id: str | None = Field(
        default=None,
        description="id of the parent GroupNode (None if root)",
    )
    discriminator: str | None = Field(
        default=None,
        description="Top-level group kind ('path'|'vcs'|'db'|None=legacy)",
    )
    original_name: str | None = Field(
        default=None,
        description="Pre-normalization original name (preserves case, etc.)",
    )
    source: str | None = Field(
        default=None,
        description=(
            "Ingestion path that first created this group "
            "('filesystem'|'doxygen'|'novel'|'vcs'|'lazy'|'yaml_tree')"
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Content Store record
# ---------------------------------------------------------------------------

class ContentRecord(BaseModel):
    """
    Record in the PostgreSQL content_store table.
    Actual content is stored in Object Storage; this model holds only metadata.
    """

    content_id: str = Field(default_factory=new_content_id)
    domain: str
    vector_id: str
    content_type: str = Field(
        description="MIME type (e.g. text/plain, text/x-code, image/jpeg)"
    )
    storage_uri: str = Field(
        description="Object Storage URI (file:///... or s3://...)"
    )
    inline_meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    model_config = {"frozen": False}  # mutable to allow adding orphan flag


# ---------------------------------------------------------------------------
# Vector DB metadata
# ---------------------------------------------------------------------------

class VectorMetadata(BaseModel):
    """
    Payload structure for a Qdrant point.
    Records which entities/events/groups/content a vector is linked to.
    """

    entity_ids: list[str] = Field(default_factory=list)
    event_id: str
    group_ids: list[str] = Field(default_factory=list)
    content_id: str
    domain: str

    def to_payload(self) -> dict[str, Any]:
        """Convert to a Qdrant payload dictionary."""
        return self.model_dump()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "VectorMetadata":
        """Restore from a Qdrant payload dictionary."""
        return cls(**payload)


# ---------------------------------------------------------------------------
# NER interface output models
# ---------------------------------------------------------------------------

class NEREntity(BaseModel):
    """A single entity extracted by NER."""

    name: str = Field(..., min_length=1)
    type: str = Field(..., min_length=1)

    @field_validator("name", "type", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class NEREvent(BaseModel):
    """Event information extracted by NER."""

    summary: str = Field(
        ...,
        min_length=1,
        description="Core event description (used as the embedding text)",
    )
    timestamp: str | None = Field(
        default=None,
        description="Event occurrence time (domain-specific format)",
    )


class NERContent(BaseModel):
    """Original content to be processed by NER."""

    content_type: str = Field(
        default="text/plain",
        description="MIME type",
    )
    data: str = Field(..., description="Raw content data (string for text content)")
    inline_meta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def auto_fill_meta(self) -> "NERContent":
        """Auto-compute char_count for text content."""
        if self.content_type.startswith("text/") and "char_count" not in self.inline_meta:
            self.inline_meta["char_count"] = len(self.data)
        return self


class NEROutput(BaseModel):
    """
    Final output returned by a NER provider.
    This structure is the input contract for the entire pipeline.
    """

    entities: list[NEREntity] = Field(
        default_factory=list,
        description="List of extracted entities",
    )
    event: NEREvent
    groups: list[str] = Field(
        default_factory=list,
        description=(
            "Structural group name list (domain/folder/file). "
            "Set by InputRunner; not determined by the LLM."
        ),
    )
    content: NERContent

    @model_validator(mode="after")
    def validate_groups_not_empty(self) -> "NEROutput":
        """Empty groups list is permitted (event with no group classification)."""
        return self
