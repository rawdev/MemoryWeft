"""ProjectionEngine — vector-projection-based Persona/Vibe extraction engine.

Computes entity/segment vector centroids, retrieves representative source texts,
and extracts LLM keywords. Stateless: writes nothing to the store.

Moved from ``k2g/web/projection.py`` — this is a vector projection engine, not
a web concern, so it was extracted out of ``web/``.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class ProjectionEngine:
    """Computes entity/segment vector centroids and retrieves representative source texts."""

    # Method metadata value stored with entity vectors (single algorithm).
    ENTITY_VECTOR_METHOD = "mean_l2norm"

    def __init__(
        self,
        graph_store: Any,
        vector_store: Any,
        content_store: Any,
        object_storage: Any,
        llm_client: Any = None,
        llm_provider: str = "anthropic",
        llm_model: str = "claude-sonnet-4-6",
        embedding_client: Any = None,
    ) -> None:
        self._graph = graph_store
        self._vector = vector_store
        self._content = content_store
        self._objects = object_storage
        self._llm_client = llm_client
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._embedding = embedding_client

    # ------------------------------------------------------------------
    # Centroid computation — Mean + L2 normalize
    # ------------------------------------------------------------------

    def compute_entity_centroid(
        self,
        entity_id: str,
        *,
        use_cache: bool = True,
    ) -> list[float] | None:
        """Mean + L2 normalize of embedding vectors from Events the Entity participated in.

        Deterministic single algorithm — no attention/fallback/threshold.
        Returns None when there are no events or centroid norm is 0 (mathematical
        necessity).

        Args:
            entity_id: Target Entity ID.
            use_cache: If True, use the Entity Vector DB cache.

        Returns:
            L2-normalized centroid vector or None.
        """
        # 1. Fetch event list
        events, total = self._graph.get_entity_events(
            entity_id=entity_id, page=1, size=10000,
        )
        if not events:
            return None

        current_event_count = total

        # 2. Check cache (when use_cache + Entity Vector DB available)
        if use_cache and hasattr(self._vector, "get_entity_vector"):
            try:
                cached = self._vector.get_entity_vector(entity_id)
            except NotImplementedError:
                cached = None
            if cached and cached.get("vector"):
                cached_count = cached["metadata"].get("ref_event_count", 0)
                if cached_count == current_event_count:
                    logger.debug(
                        "Entity vector cache hit: entity_id=%s, events=%d",
                        entity_id, current_event_count,
                    )
                    return cached["vector"]
                logger.debug(
                    "Entity vector stale: entity_id=%s, cached=%d, current=%d",
                    entity_id, cached_count, current_event_count,
                )

        # 3. Collect event vectors
        vector_ids = [e["vector_id"] for e in events if e.get("vector_id")]
        if not vector_ids:
            return None

        vectors_map = self._vector.get_vectors(vector_ids)
        if not vectors_map:
            return None

        ordered = [
            vectors_map[e["vector_id"]]
            for e in events
            if e.get("vector_id") and e["vector_id"] in vectors_map
        ]
        if not ordered:
            return None

        arr = np.array(ordered, dtype=np.float32)

        # 4. Centroid: mean + L2 normalize
        centroid = arr.mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm == 0.0:
            # degenerate (all events cancel out exactly) — undefined
            return None
        centroid = centroid / norm
        result = centroid.tolist()

        # 5. Save to cache
        if hasattr(self._vector, "upsert_entity_vector"):
            try:
                entity_data = self._graph.get_entity_by_id(entity_id)
                entity_name = entity_data.get("name", "") if entity_data else ""
                domain = entity_data.get("domain", "") if entity_data else ""

                from datetime import datetime, timezone
                self._vector.upsert_entity_vector(
                    entity_id=entity_id,
                    vector=result,
                    metadata={
                        "entity_name": entity_name,
                        "domain": domain,
                        "computed_at": datetime.now(timezone.utc).isoformat(),
                        "ref_event_count": current_event_count,
                        "method": self.ENTITY_VECTOR_METHOD,
                        # pre-normalization norm = mean resultant length R in [0,1]
                        "coherence": norm,
                    },
                )
            except Exception as e:
                logger.warning("Entity vector cache save failed: %s", e)

        return result

    def compute_entity_vectors(
        self,
        domain: str,
        *,
        page_size: int = 1000,
        use_cache: bool = False,
    ) -> dict[str, Any]:
        """Batch-compute mean+L2norm centroids for all entities in a domain.

        ``list_entities`` uses page/size pagination, so this method iterates
        pages of ``page_size`` (default 1000).

        Returns:
            {total, computed, errors, skipped}
        """
        stats = {
            "total": 0, "computed": 0, "errors": 0, "skipped": 0,
        }

        page = 1
        seen = 0
        total_count = None
        while True:
            entity_list, total = self._graph.list_entities(
                domain=domain, page=page, size=page_size,
            )
            if total_count is None:
                total_count = total
                stats["total"] = total_count
            if not entity_list:
                break

            for ent in entity_list:
                eid = ent.get("id", "")
                if not eid:
                    continue
                try:
                    result = self.compute_entity_centroid(
                        entity_id=eid, use_cache=use_cache,
                    )
                    if result is not None:
                        stats["computed"] += 1
                    else:
                        stats["skipped"] += 1
                except Exception as e:
                    logger.warning(
                        "Entity vector compute failed: entity_id=%s, error=%s", eid, e,
                    )
                    stats["errors"] += 1

            seen += len(entity_list)
            logger.info(
                "Entity vectors progress: domain=%s, page=%d, seen=%d/%d, computed=%d",
                domain, page, seen, total_count, stats["computed"],
            )
            if seen >= total_count:
                break
            page += 1

        logger.info(
            "Entity vectors batch complete: domain=%s, %s", domain, stats,
        )
        return stats

    def compute_segment_centroid(
        self,
        event_ids: list[str],
    ) -> list[float] | None:
        """Compute the mean of embedding vectors for the specified Events.

        Args:
            event_ids: List of Event IDs.

        Returns:
            Centroid vector or None.
        """
        if not event_ids:
            return None

        # Event ID → vector_id mapping
        vector_ids = []
        for eid in event_ids:
            ev = self._graph.get_event_by_id(eid)
            if ev and ev.get("vector_id"):
                vector_ids.append(ev["vector_id"])

        if not vector_ids:
            return None

        vectors_map = self._vector.get_vectors(vector_ids)
        if not vectors_map:
            return None

        vecs = [v for v in vectors_map.values()]
        if not vecs:
            return None

        arr = np.array(vecs, dtype=np.float32)
        centroid = arr.mean(axis=0)
        return centroid.tolist()

    # ------------------------------------------------------------------
    # Representative source text retrieval
    # ------------------------------------------------------------------

    def find_representative_events(
        self,
        centroid: list[float],
        domain: str,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """Retrieve the top_k Event source texts nearest to the centroid vector.

        Returns:
            [{"event_id", "score", "original_text"}]
        """
        hits = self._vector.search(
            query_vector=centroid,
            filter_domain=domain or None,
            limit=top_k,
            score_threshold=0.0,
        )

        results = []
        for hit in hits:
            metadata = hit.get("metadata", {})
            event_id = metadata.get("event_id", "")
            content_id = metadata.get("content_id", "")

            original_text = ""
            if content_id:
                record = self._content.get(content_id)
                if record and record.storage_uri:
                    try:
                        original_text = self._objects.get_text(record.storage_uri)
                    except Exception:
                        pass

            results.append({
                "event_id": event_id,
                "score": hit.get("score", 0.0),
                "original_text": original_text[:500] if original_text else "",
            })

        return results

    # ------------------------------------------------------------------
    # Tension score
    # ------------------------------------------------------------------

    def compute_tension_score(
        self,
        entity_id_a: str,
        entity_id_b: str,
    ) -> dict[str, Any]:
        """Compute the tension score between two Entities based on cosine similarity of their personality vectors."""
        centroid_a = self.compute_entity_centroid(entity_id_a)
        centroid_b = self.compute_entity_centroid(entity_id_b)

        if centroid_a is None or centroid_b is None:
            return {
                "entity_a": entity_id_a,
                "entity_b": entity_id_b,
                "cosine_similarity": None,
                "tension_score": None,
                "interpretation": "Insufficient vector data to compute",
            }

        a = np.array(centroid_a, dtype=np.float32)
        b = np.array(centroid_b, dtype=np.float32)

        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)

        if norm_a == 0 or norm_b == 0:
            cos_sim = 0.0
        else:
            cos_sim = float(np.dot(a, b) / (norm_a * norm_b))

        tension = 1.0 - cos_sim

        if tension >= 0.7:
            interpretation = "High opposition potential — narratively tense pairing"
        elif tension >= 0.4:
            interpretation = "Some opposition potential — conflict elements present"
        elif tension >= 0.2:
            interpretation = "Moderate — no clear opposition"
        else:
            interpretation = "Similar disposition — cooperative relationship likely"

        return {
            "entity_a": entity_id_a,
            "entity_b": entity_id_b,
            "cosine_similarity": round(cos_sim, 4),
            "tension_score": round(tension, 4),
            "interpretation": interpretation,
        }

    # ------------------------------------------------------------------
    # Persona / Vibe extraction (LLM keywords)
    # ------------------------------------------------------------------

    def extract_persona(
        self,
        entity_id: str,
        domain: str,
        top_k: int = 3,
    ) -> dict[str, Any]:
        """Extract an Entity's persona.

        centroid → representative source text → LLM keywords/summary.
        time_decay parameter removed (meaningless in mean+L2norm algorithm).
        """
        # Look up entity name
        entity_name = ""
        try:
            entity_data = self._graph.get_entity_by_id(entity_id)
            if entity_data:
                entity_name = entity_data.get("name", "")
        except Exception:
            pass

        centroid = self.compute_entity_centroid(entity_id)
        if centroid is None:
            return {
                "entity_id": entity_id,
                "entity_name": entity_name,
                "keywords": [],
                "summary": "Insufficient event data to extract persona.",
                "source_events": [],
            }

        representative = self.find_representative_events(centroid, domain, top_k)

        if not representative or not any(r["original_text"] for r in representative):
            return {
                "entity_id": entity_id,
                "entity_name": entity_name,
                "keywords": [],
                "summary": "No representative source text found.",
                "source_events": representative,
            }

        keywords, summary = self._llm_extract_keywords(
            entity_name=entity_name,
            texts=[r["original_text"] for r in representative if r["original_text"]],
            mode="persona",
        )

        return {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "keywords": keywords,
            "summary": summary,
            "source_events": [
                {"event_id": r["event_id"], "score": r["score"],
                 "text_preview": r["original_text"][:200]}
                for r in representative
            ],
        }

    def extract_vibe(
        self,
        event_ids: list[str],
        domain: str,
        segment_key: str = "",
        top_k: int = 3,
        narrative_summary: str = "",
    ) -> dict[str, Any]:
        """Extract the vibe of a narrative segment.

        centroid → representative source text → LLM keywords/summary.
        """
        centroid = self.compute_segment_centroid(event_ids)
        if centroid is None:
            return {
                "segment_key": segment_key,
                "keywords": [],
                "summary": "Insufficient event data to extract vibe.",
                "narrative_summary": narrative_summary,
                "source_events": [],
            }

        representative = self.find_representative_events(centroid, domain, top_k)

        texts = [r["original_text"] for r in representative if r["original_text"]]
        if narrative_summary:
            texts.insert(0, f"[segment summary] {narrative_summary}")

        if not texts:
            return {
                "segment_key": segment_key,
                "keywords": [],
                "summary": "No representative source text found.",
                "narrative_summary": narrative_summary,
                "source_events": representative,
            }

        keywords, summary = self._llm_extract_keywords(
            entity_name="",
            texts=texts,
            mode="vibe",
        )

        return {
            "segment_key": segment_key,
            "keywords": keywords,
            "summary": summary,
            "narrative_summary": narrative_summary,
            "source_events": [
                {"event_id": r["event_id"], "score": r["score"],
                 "text_preview": r["original_text"][:200]}
                for r in representative
            ],
        }

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    def _llm_extract_keywords(
        self,
        entity_name: str,
        texts: list[str],
        mode: str,
    ) -> tuple[list[str], str]:
        """Call the LLM to extract keywords and a summary.

        Returns:
            (keywords: list[str], summary: str)
        """
        if self._llm_client is None:
            return [], "[No LLM client]"

        combined = "\n---\n".join(texts)

        if mode == "persona":
            system_prompt = (
                "You are an expert at extracting personality/persona from text. "
                "Analyze the given texts to identify the character's current personality."
            )
            user_message = (
                f"The following are key events involving '{entity_name}':\n\n"
                f"{combined}\n\n"
                f"Based on the above, analyze '{entity_name}''s current personality/persona.\n"
                f"You MUST answer in the following format:\n"
                f"Keywords: [noun keyword1], [noun keyword2], [noun keyword3]\n"
                f"Summary: [one-line summary sentence]"
            )
        else:  # vibe
            system_prompt = (
                "You are an expert at extracting the mood/vibe of narrative segments "
                "from text. Analyze the given texts to identify the segment's tension, "
                "conflict, and atmosphere."
            )
            user_message = (
                f"The following are key events from a specific narrative segment:\n\n"
                f"{combined}\n\n"
                f"Based on the above, analyze this segment's mood/vibe.\n"
                f"You MUST answer in the following format:\n"
                f"Keywords: [noun keyword1], [noun keyword2], [noun keyword3]\n"
                f"Summary: [one-line summary sentence]"
            )

        raw = self._call_llm(system_prompt, user_message, max_tokens=256)
        return self._parse_keywords_response(raw)

    def _call_llm(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 256,
        temperature: float = 0.3,
    ) -> str:
        """LLM API call (Anthropic/OpenAI/Ollama)."""
        if self._llm_client is None:
            return ""

        try:
            if self._llm_provider == "anthropic":
                message = self._llm_client.messages.create(
                    model=self._llm_model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                )
                for block in message.content:
                    if hasattr(block, "text"):
                        return block.text
                return ""

            elif self._llm_provider in ("openai", "ollama"):
                response = self._llm_client.chat.completions.create(
                    model=self._llm_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return response.choices[0].message.content or ""

        except Exception as e:
            logger.error("ProjectionEngine LLM call failed: %s", e)
            return f"[LLM error: {e}]"

        return ""

    @staticmethod
    def _parse_keywords_response(raw: str) -> tuple[list[str], str]:
        """Parse keywords and summary from the LLM response.

        Expected format:
            Keywords: [kw1], [kw2], [kw3]
            Summary: summary text
        """
        keywords: list[str] = []
        summary = ""

        for line in raw.strip().splitlines():
            line = line.strip()
            low = line.lower()
            if low.startswith("keywords:") or low.startswith("keywords :"):
                kw_part = line.split(":", 1)[1].strip()
                # Handle both [kw1], [kw2] and kw1, kw2 formats
                kw_part = kw_part.replace("[", "").replace("]", "")
                keywords = [k.strip() for k in kw_part.split(",") if k.strip()]
            elif low.startswith("summary:") or low.startswith("summary :"):
                summary = line.split(":", 1)[1].strip()

        # Fallback: use entire text as summary on parse failure
        if not keywords and not summary:
            summary = raw.strip()[:200]

        return keywords, summary
