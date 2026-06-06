"""EventBrancher — Event-centric branching generation (Vector2Text).

Rewritten on DbStore (no Neo4j). LLM calls delegate to
``k2g.trainer.llm_utils.call_llm`` to reuse anthropic/openai/ollama branching.

Two generation paths:
    branch(base_event_id, condition, ...)         — event_id → graph lookup → LLM
    branch_by_search(query, condition, entities)  — vector search → entity merge → LLM

Guardrail: when control_node_id is given, the CG's narrative_summary + event
sequence are injected into the system prompt.

This module produces a BranchedEvent for the caller to persist via
DocBatchProducer / NovelProducer load paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class SearchedEventContext:
    """Event context discovered via vector search."""

    event_id: str
    score: float
    summary: str = ""
    entities: list[str] = field(default_factory=list)
    content_id: str = ""


@dataclass
class BranchedEvent:
    """Generated branched event — not yet persisted to the graph."""

    text: str
    base_event_id: str
    condition: str
    entities: list[str] = field(default_factory=list)
    domain: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


_DOMAIN_HINTS_BRANCH = {
    "novel": "Generate a narrative scene continuation.",
    "code": "Generate a technical event description (e.g., code change, API call).",
    "project": "Generate a project event (e.g., task completion, decision).",
}

_DOMAIN_HINTS_SEARCH = {
    "novel": "Generate a narrative scene based on similar events found.",
    "code": "Generate a technical event (code change, refactoring, bug fix) based on similar patterns found.",
    "project": "Generate a project event (milestone, decision, incident) based on similar events found.",
}


# ---------------------------------------------------------------------------
# EventBrancher
# ---------------------------------------------------------------------------


class EventBrancher:
    """Conditional branching event generator based on existing events + entities.

    graph_store is ``DbStore.graph`` (SqliteGraphStore / PostgresGraphStore).
    """

    def __init__(
        self,
        graph_store: Any,
        content_store: Any = None,
        vector_store: Any = None,
        embedding_client: Any = None,
        llm_client: Any = None,
        llm_provider: str = "anthropic",
        llm_model: str = "claude-sonnet-4-6",
    ) -> None:
        self._graph = graph_store
        self._content = content_store
        self._vector = vector_store
        self._embedding = embedding_client
        self._llm_client = llm_client
        self._llm_provider = llm_provider
        self._llm_model = llm_model

    # ------------------------------------------------------------------
    # Path 1: event_id → graph context → LLM
    # ------------------------------------------------------------------

    def branch(
        self,
        base_event_id: str,
        condition: str,
        entities: list[str] | None = None,
        domain: str = "",
        control_node_id: str | None = None,
        **llm_params: Any,
    ) -> BranchedEvent:
        """Generate the next event conditionally from a base event."""
        logger.info(
            "EventBrancher.branch: base=%s, condition=%r, cg=%s",
            base_event_id, (condition or "")[:80], control_node_id,
        )

        event_context = self._get_event_context_text(base_event_id)
        if entities is None:
            entities = self._get_participating_entities(base_event_id)
        guardrail_context = self._build_guardrail_context(control_node_id)

        text = self._generate_event(
            event_context=event_context,
            condition=condition,
            entities=entities,
            domain=domain,
            guardrail_context=guardrail_context,
            **llm_params,
        )
        return BranchedEvent(
            text=text,
            base_event_id=base_event_id,
            condition=condition,
            entities=entities,
            domain=domain,
        )

    # ------------------------------------------------------------------
    # Path 2: query → vector search → entity merge → LLM
    # ------------------------------------------------------------------

    def branch_by_search(
        self,
        query: str,
        condition: str,
        entities: list[str],
        domain: str = "",
        search_limit: int = 5,
        score_threshold: float = 0.3,
        merge_searched_entities: bool = True,
        **llm_params: Any,
    ) -> BranchedEvent:
        """Search for similar events via vector search → merge entities → generate new event."""
        if self._vector is None or self._embedding is None:
            raise RuntimeError(
                "branch_by_search requires vector_store + embedding_client",
            )

        query_vector = self._embedding.embed(query)
        search_results = self._vector.search(
            query_vector=list(query_vector),
            filter_domain=domain or None,
            limit=int(search_limit),
            score_threshold=float(score_threshold),
        )
        if not search_results:
            return BranchedEvent(
                text="[No search results — cannot generate event]",
                base_event_id="",
                condition=condition,
                entities=list(entities),
                domain=domain,
                metadata={"query": query, "search_hits": 0},
            )

        searched = self._collect_search_contexts(search_results)
        merged = list(entities)
        if merge_searched_entities:
            for ctx in searched:
                for ent in ctx.entities:
                    if ent not in merged:
                        merged.append(ent)

        context_text = self._build_search_context_text(searched)
        text = self._generate_event_from_search(
            search_context=context_text,
            condition=condition,
            entities=merged,
            user_entities=list(entities),
            domain=domain,
            **llm_params,
        )
        best = searched[0].event_id if searched else ""
        return BranchedEvent(
            text=text,
            base_event_id=best,
            condition=condition,
            entities=merged,
            domain=domain,
            metadata={
                "query": query,
                "search_hits": len(search_results),
                "searched_event_ids": [c.event_id for c in searched],
                "user_entities": list(entities),
            },
        )

    # ------------------------------------------------------------------
    # Graph helpers — DbStore-based
    # ------------------------------------------------------------------

    def _get_event_context_text(self, event_id: str) -> str:
        """graph.get_event_by_id → summary / timestamp / domain as text."""
        try:
            ev = self._graph.get_event_by_id(event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_event_by_id failed (%s): %s", event_id, exc)
            ev = None
        if not ev:
            return f"[Event {event_id}]"
        parts = [f"id={ev.get('id')}"]
        if ev.get("summary"):
            parts.append(f"summary={ev['summary']}")
        if ev.get("timestamp"):
            parts.append(f"ts={ev['timestamp']}")
        if ev.get("domain"):
            parts.append(f"domain={ev['domain']}")
        return "{" + ", ".join(parts) + "}"

    def _get_participating_entities(self, event_id: str) -> list[str]:
        """graph.get_event_context → entity name list."""
        try:
            ctx = self._graph.get_event_context(event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_event_context failed (%s): %s", event_id, exc)
            return []
        out: list[str] = []
        for ent in ctx.get("entities", []) or []:
            name = ent.get("name") if isinstance(ent, dict) else None
            if name:
                out.append(str(name))
        return out

    def _build_guardrail_context(self, control_node_id: str | None) -> str:
        """Build guardrail context from a Control Node (CG)'s narrative_summary + event sequence."""
        if not control_node_id:
            return ""
        try:
            cg = self._graph.get_context_group(control_node_id)
            events = self._graph.get_cg_events(control_node_id) if cg else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("guardrail context lookup failed: %s", exc)
            return ""
        if not cg and not events:
            return ""

        summary = (cg or {}).get("narrative_summary") or ""
        if not summary and not events:
            return ""

        lines = ["[Context — Control Node Guardrail]"]
        if summary:
            lines.append(f'Context for these events:\n"{summary}"')
        if events:
            lines.append("\nRelated event sequence:")
            for i, ev in enumerate(events, 1):
                ev_summary = ev.get("summary") or ev.get("id", "")
                lines.append(f"{i}. {ev_summary}")
        return "\n".join(lines)

    def _collect_search_contexts(
        self, search_results: list[dict[str, Any]],
    ) -> list[SearchedEventContext]:
        """Vector search hits → SearchedEventContext list.

        SqliteVectorStore.search hit format: {id, domain, summary, distance}.
        Also compatible with legacy Qdrant format (nested metadata).
        """
        contexts: list[SearchedEventContext] = []
        for hit in search_results:
            metadata = hit.get("metadata") or {}
            event_id = (
                metadata.get("event_id")
                or hit.get("event_id")
                or hit.get("id")
                or ""
            )
            content_id = metadata.get("content_id") or hit.get("content_id") or ""
            score = float(hit.get("score") or hit.get("distance") or 0.0)

            summary = hit.get("summary") or ""
            if not summary and content_id and self._content is not None:
                try:
                    rec = self._content.get(content_id)
                    if rec is not None:
                        summary = rec.inline_meta.get("event_summary", "") or ""
                except Exception:  # noqa: BLE001
                    pass

            ents = self._get_participating_entities(event_id) if event_id else []
            contexts.append(SearchedEventContext(
                event_id=str(event_id),
                score=score,
                summary=str(summary),
                entities=ents,
                content_id=str(content_id),
            ))
        return contexts

    @staticmethod
    def _build_search_context_text(
        contexts: list[SearchedEventContext],
    ) -> str:
        lines: list[str] = []
        for i, ctx in enumerate(contexts, 1):
            entity_str = ", ".join(ctx.entities) if ctx.entities else "(none)"
            summary = ctx.summary or f"[Event {ctx.event_id}]"
            lines.append(
                f"[{i}] (score={ctx.score:.2f}) {summary}\n"
                f"    Entities: {entity_str}",
            )
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _call_llm(
        self,
        *,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Delegate to trainer.llm_utils.call_llm — reuses provider branching."""
        from k2g.trainer.llm_utils import call_llm

        try:
            out = call_llm(
                client=self._llm_client,
                provider=self._llm_provider,
                model=self._llm_model,
                system_prompt=system_prompt,
                user_message=user_message,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return out or ""
        except Exception as exc:  # noqa: BLE001
            logger.error("EventBrancher LLM call failed: %s", exc)
            return f"[Event generation error: {exc}]"

    def _generate_event(
        self,
        event_context: str,
        condition: str,
        entities: list[str],
        domain: str,
        guardrail_context: str = "",
        **llm_params: Any,
    ) -> str:
        if self._llm_client is None:
            return (
                f"[No LLM — branched event placeholder]\n"
                f"Base: {event_context[:200]}\n"
                f"Condition: {condition}\n"
                f"Entities: {', '.join(entities)}"
            )

        hint = _DOMAIN_HINTS_BRANCH.get(domain, "Generate the next event.")
        system_prompt = (f"{guardrail_context}\n\n" if guardrail_context else "") + (
            f"You are an event generation engine for a knowledge graph. "
            f"{hint} Given a base event and a branching condition, produce the "
            f"text for the next event that would occur under that condition. "
            f"Keep the output concise and factual."
        )
        entity_str = ", ".join(entities) if entities else "(none)"
        user_message = (
            f"## Base Event\n{event_context}\n\n"
            f"## Participating Entities\n{entity_str}\n\n"
            f"## Branching Condition\n{condition}\n\n"
            f"Generate the next event text."
        )
        return self._call_llm(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=int(llm_params.pop("max_tokens", 1024)),
            temperature=float(llm_params.pop("temperature", 0.7)),
        )

    def _generate_event_from_search(
        self,
        search_context: str,
        condition: str,
        entities: list[str],
        user_entities: list[str],
        domain: str,
        **llm_params: Any,
    ) -> str:
        if self._llm_client is None:
            return (
                f"[No LLM — search-based event placeholder]\n"
                f"Context:\n{search_context[:500]}\n"
                f"Condition: {condition}\n"
                f"Entities: {', '.join(entities)}"
            )
        hint = _DOMAIN_HINTS_SEARCH.get(
            domain, "Generate a new event based on similar events found.",
        )
        system_prompt = (
            f"You are an event generation engine for a knowledge graph. {hint} "
            f"You are given similar events retrieved via vector search and a set "
            f"of entities that should participate in the new event. Combine the "
            f"relationship dynamics from the searched events with the given "
            f"entities and condition to produce a new, coherent event. Keep the "
            f"output concise and factual."
        )
        entity_str = ", ".join(entities) if entities else "(none)"
        user_entity_str = ", ".join(user_entities) if user_entities else "(none)"
        user_message = (
            f"## Similar Events (from vector search)\n{search_context}\n\n"
            f"## Target Entities (must appear in the new event)\n{user_entity_str}\n\n"
            f"## All Available Entities\n{entity_str}\n\n"
            f"## Generation Condition\n{condition}\n\n"
            f"Generate the new event text."
        )
        return self._call_llm(
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=int(llm_params.pop("max_tokens", 1024)),
            temperature=float(llm_params.pop("temperature", 0.7)),
        )


__all__ = ["EventBrancher", "BranchedEvent", "SearchedEventContext"]
