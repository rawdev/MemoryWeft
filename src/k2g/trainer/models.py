"""Trainer shared models — migrated from legacy training/models.py.

- CGStage: ContextGroup 4-stage lifecycle enum (HdbscanPhase / StageTransition).
- PlanStage / DirectionStage: Plan lifecycle enums.
- new_* id factories: plan/etg/plan_node/direction id generation (uuid4 hex prefix).
- ETGRecord / PlanNodeRecord / PlanDirectionRecord / PlanEvaluation:
  pydantic DTOs exchanged by ETGBuilder / PlanBuilder / PlanEvaluator.
"""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, Field


class CGStage(str, Enum):
    """Context Group 4-stage lifecycle."""

    SEED = "seed"               # 2~4 members
    SPROUT = "sprout"           # 5~9 members
    ESTABLISHED = "established"  # 10~19 members
    CORE = "core"               # 20+ members


class BP30PlanStage(str, Enum):
    """PlanNode lifecycle."""

    DRAFT = "draft"
    PLANNED = "planned"
    ACTIVE = "active"
    REALIZED = "realized"
    ABANDONED = "abandoned"


class DirectionStage(str, Enum):
    """PlanDirectionNode lifecycle."""

    ACTIVE = "active"
    REALIZED = "realized"
    ABANDONED = "abandoned"


def new_cg_id() -> str:
    return f"cg_{uuid.uuid4().hex}"


def new_plan_id() -> str:
    return f"plan_{uuid.uuid4().hex}"


def new_etg_id() -> str:
    return f"etg_{uuid.uuid4().hex}"


def new_plan_node_id() -> str:
    return f"pn_{uuid.uuid4().hex}"


def new_direction_id() -> str:
    return f"dn_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# DTOs exchanged by Builder / Evaluator
# ---------------------------------------------------------------------------


class ETGRecord(BaseModel):
    """EventTemplateGroup — explicitly created by user, based on Plan blueprint."""

    id: str = Field(default_factory=new_etg_id)
    name: str = Field(default="")
    domain: str = Field(default="")
    transition_pattern: list[str] = Field(default_factory=list)
    structure_description: str = Field(default="")
    entity_list: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    instance_count: int = Field(default=0, ge=0)
    source_event_ids: list[str] = Field(default_factory=list)


class PlanNodeRecord(BaseModel):
    """PlanNode — ETG-based stage blueprint."""

    id: str = Field(default_factory=new_plan_node_id)
    plan_id: str
    etg_id: str = Field(default="")
    domain: str = Field(default="")
    name: str = Field(default="")
    stage: BP30PlanStage = Field(default=BP30PlanStage.DRAFT)
    objective_summary: str = Field(default="")
    structure_description: str = Field(default="")
    entity_ids: list[str] = Field(default_factory=list)
    entity_summary: str = Field(default="")
    order_index: int = Field(default=0)
    realized_event_id: str = Field(default="")
    abandon_reason: str = Field(default="")


class PlanDirectionRecord(BaseModel):
    """PlanDirectionNode — directional goal without ETG."""

    id: str = Field(default_factory=new_direction_id)
    plan_id: str
    domain: str = Field(default="")
    goal_text: str = Field(default="")
    stage: DirectionStage = Field(default=DirectionStage.ACTIVE)
    entity_ids: list[str] = Field(default_factory=list)
    entity_summary: str = Field(default="")
    order_index: int = Field(default=0)
    realized_event_id: str = Field(default="")
    abandon_reason: str = Field(default="")


class PlanEvaluation(BaseModel):
    """Plan evaluation result at ingestion time."""

    plan_id: str
    target_node_id: str = Field(default="")
    target_kind: str = Field(default="plan")
    verdict: str = Field(default="incomplete")
    qualitative_reason: str = Field(default="")
    embedding_distance: float = Field(default=0.0, ge=0.0)
    suggested_next_goal: str = Field(default="")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


__all__ = [
    "CGStage",
    "BP30PlanStage",
    "DirectionStage",
    "new_cg_id",
    "new_plan_id",
    "new_etg_id",
    "new_plan_node_id",
    "new_direction_id",
    "ETGRecord",
    "PlanNodeRecord",
    "PlanDirectionRecord",
    "PlanEvaluation",
]
