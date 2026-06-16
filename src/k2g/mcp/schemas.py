"""
K2G ChronoGraph MCP -- response schemas (Event unit).

Pydantic models for MCP read tools. Search/entity-lookup return Event-unit
hits (unified shape for events and entities). ContextDetailResponse remains
the CN-centric view for the dedicated k2g_context_detail tool.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


TOP_K_MIN = 1
TOP_K_MAX = 50
DEPTH_MIN = 0
DEPTH_MAX = 3
ENTITY_CANDIDATE_MAX = 5


class SearchHit(BaseModel):
    """Single vector-search hit — unified shape for event and entity results."""

    kind: Literal["event", "entity"]
    id: str
    # Internal ranking only; excluded from response serialization
    # (continuous scores are noise for the LLM).
    score: float = Field(exclude=True)
    domain: str | None = None
    name_or_summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Per-hit connections (sequential window, etc.). Empty dict when
    # no data.
    connections: dict[str, Any] = Field(default_factory=dict)
    # Fixed-vocabulary tags for why/how this hit surfaced (match
    # rationale).
    reason: list[str] = Field(default_factory=list)


class HintBlock(BaseModel):
    """Connection Map -- structured connection map across all search hits.

    A lightweight map of counts, ids, and tags (not a heavy expanded
    payload). The LLM reacts in-context *after* the call. All entries
    are structures (threads / entities / context_groups / categories /
    clusters) + reason tags with no prose. Only channels with data
    appear (channel-presence-agnostic): ``available_channels``
    advertises the currently active reason tags.

    ``clusters`` groups 2+ hits sharing the same leiden community.
    ``inter_edges`` count + density category (strong/medium/weak) +
    ``top_edges`` sample let the LLM distinguish real signal from
    coincidence. ``cluster_id`` is meaningful only within this call
    -- it may change on leiden recompute; *do not memorize*.
    """

    threads: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    context_groups: list[dict[str, Any]] = Field(default_factory=list)
    categories: list[dict[str, Any]] = Field(default_factory=list)
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    available_channels: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """Response for k2g_search.

    ``hint`` is declared *before* ``hits`` so that model_dump
    serializes it first. This ensures the LLM reads the connection
    map before the (potentially long) hits array, preventing the
    hint from being buried and ignored.
    """

    query: str
    mode: Literal["hybrid", "events", "entities"]
    # Connection map. None when hits are empty or generation failed.
    hint: HintBlock | None = None
    total: int
    hits: list[SearchHit]
    # Deficit signal (additive, backward-compatible defaults). A top-k slice
    # looks self-complete; these advertise that the ranked set extends beyond
    # what is shown so the LLM can chain via mweft_search_window instead of
    # treating this response as the whole world. ``more_available`` is detected
    # with a cheap +1 over-fetch probe (not an exact match count).
    shown: int = 0
    more_available: bool = False
    # Score of the first result beyond the shown slice (cosine similarity), or
    # None when nothing is beyond it. Surfaced so the caller can judge the
    # relevance drop-off directly; more_available is gated on it.
    tail_score: float | None = None
    continuation: str | None = None


class EventSummary(BaseModel):
    """Lightweight event representation used inside lookup/context responses.

    ``influence_score`` and manifest evidence fields (source_root,
    file_path, file_hash, ingested_at) are exposed. All evidence
    fields may be None when no manifest exists or no matching row
    is found.
    """

    id: str
    vector_id: str | None = None
    domain: str | None = None
    timestamp: str | None = None
    order_index: int | None = None
    summary: str | None = None
    influence_score: float = 1.0
    source_root: str | None = None
    file_path: str | None = None
    file_hash: str | None = None
    ingested_at: str | None = None


class EntityRef(BaseModel):
    id: str
    name: str
    user_tag: str | None = None
    type: str | None = None
    domain: str | None = None


class EntityMatch(BaseModel):
    """One entity candidate with its connected events."""

    entity: dict[str, Any]
    events: list[EventSummary]
    total: int


class TagMatch(BaseModel):
    """One tag candidate (groups table) + its member events.

    Lexical integration: on substring match, tags are surfaced as
    candidates alongside entities. Internal column names remain
    ``groups`` -- only the surface name is "tag".
    """

    tag: dict[str, Any]  # {kind: "tag", id, name, domain, parent_id, user_tag}
    events: list[EventSummary]
    total: int


class EntityLookupResponse(BaseModel):
    """Response for k2g_entity_lookup.

    ``hint`` (connection map) is declared *before* the large
    ``matches`` array so the LLM reads the connection map first
    (consistent with the hint-first approach in search). This
    ensures the hint reaches the LLM even when entity_lookup is
    the terminal step in a search path.

    Lexical integration: the same substring also matches tags
    (``tag_matches``). Existing ``matches`` keeps entities only
    (backward compat). Each entity dict has ``kind="entity"``,
    each tag dict has ``kind="tag"`` (self-describing).
    """

    query_name: str
    # Connection map. None when both matches/tag_matches are empty or generation failed.
    hint: HintBlock | None = None
    suggestion: str | None = None
    matches: list[EntityMatch]
    tag_matches: list[TagMatch] = Field(default_factory=list)


class ContextDetailResponse(BaseModel):
    """Response for k2g_context_detail — the full view of one ContextNode."""

    cg_id: str
    name: str
    narrative_summary: str | None = None
    stage: str | None = None
    domain: str | None = None
    confidence: float | None = None
    member_count_own: int | None = None
    member_count_total: int | None = None
    parent: dict[str, Any] | None = None
    children: list[dict[str, Any]] = Field(default_factory=list)
    ancestors: list[dict[str, Any]] = Field(default_factory=list)
    events: list[EventSummary] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Standard error envelope — used when a tool cannot fulfill the request."""

    error: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# Influence
# ---------------------------------------------------------------------------


class InfluenceUpdate(BaseModel):
    """Response for k2g_set_influence."""

    event_id: str
    found: bool = True
    score_before: float | None = None
    score_after: float | None = None
    reason: str | None = None


class InfluenceReviewMember(BaseModel):
    event_id: str
    status: str
    influence_score: float | None = None
    file_hash: str | None = None
    ingested_at: str | None = None
    source_root: str | None = None


class InfluenceReviewCandidate(BaseModel):
    domain: str
    file_path: str
    segment_key: str
    candidates: list[InfluenceReviewMember]


class InfluenceReviewResponse(BaseModel):
    """Response for k2g_suggest_influence_review."""

    candidates: list[InfluenceReviewCandidate]
    total: int


def clamp_top_k(value: int) -> int:
    return max(TOP_K_MIN, min(TOP_K_MAX, int(value)))


def clamp_depth(value: int) -> int:
    return max(DEPTH_MIN, min(DEPTH_MAX, int(value)))
