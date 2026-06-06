"""Suggest entity merge candidates (BP-87 §11.3).

Two signal layers:
  1. Normalization match — lowercase + NFKC + whitespace collapse →
     identical → confidence 1.0
  2. Fuzzy match — token-set ratio via rapidfuzz (optional dep) → score

Only emits pairs *between non-deprecated entities of the same domain*.
The viewer presents these as suggestions for manual confirmation; never
auto-merge.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from k2g.db_store import DbStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeCandidate:
    alias_id: str
    canonical_id: str
    alias_name: str
    canonical_name: str
    domain: str
    confidence: float
    reason: str  # 'normalize' | 'fuzzy'


_WS = re.compile(r"\s+")


def _normalize(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name).strip().lower()
    s = _WS.sub(" ", s)
    return s


def find_candidates(
    db: "DbStore",
    *,
    domain: str | None = None,
    similarity_threshold: float = 0.85,
    max_results: int = 50,
    fuzzy: bool = True,
) -> list[MergeCandidate]:
    """Enumerate candidate (alias, canonical) pairs.

    Heuristic for canonical pick: within a normalize-equivalent cluster,
    the entity with the most participated_in events is the canonical;
    others become aliases.
    """
    rows = _list_all_entities(db, domain=domain)
    if not rows:
        return []

    # Bucket by normalized name within (domain, type).
    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        if r.get("deprecated"):
            continue
        nm = _normalize(r.get("name") or "")
        if not nm:
            continue
        key = (nm, r.get("domain") or "", r.get("type") or "")
        buckets.setdefault(key, []).append(r)

    candidates: list[MergeCandidate] = []

    # Layer 1 — normalize match
    for (_norm, dom, _typ), members in buckets.items():
        if len(members) < 2:
            continue
        canonical = _pick_canonical(db, members)
        for m in members:
            if m["id"] == canonical["id"]:
                continue
            candidates.append(
                MergeCandidate(
                    alias_id=m["id"],
                    canonical_id=canonical["id"],
                    alias_name=m.get("name") or "",
                    canonical_name=canonical.get("name") or "",
                    domain=dom,
                    confidence=1.0,
                    reason="normalize",
                )
            )

    # Layer 2 — fuzzy match across normalized buckets (different normals)
    if fuzzy:
        try:
            from rapidfuzz import fuzz
        except ImportError:
            logger.info("rapidfuzz not installed — skipping fuzzy candidates")
        else:
            keys = list(buckets.keys())
            for i in range(len(keys)):
                norm_i, dom_i, typ_i = keys[i]
                for j in range(i + 1, len(keys)):
                    norm_j, dom_j, typ_j = keys[j]
                    if dom_i != dom_j or typ_i != typ_j:
                        continue
                    # Skip pairs already in normalize buckets
                    if norm_i == norm_j:
                        continue
                    ratio = fuzz.token_set_ratio(norm_i, norm_j) / 100.0
                    if ratio < similarity_threshold:
                        continue
                    rep_i = _pick_canonical(db, buckets[keys[i]])
                    rep_j = _pick_canonical(db, buckets[keys[j]])
                    if _event_count(db, rep_i["id"]) >= _event_count(db, rep_j["id"]):
                        canonical, alias = rep_i, rep_j
                    else:
                        canonical, alias = rep_j, rep_i
                    candidates.append(
                        MergeCandidate(
                            alias_id=alias["id"],
                            canonical_id=canonical["id"],
                            alias_name=alias.get("name") or "",
                            canonical_name=canonical.get("name") or "",
                            domain=dom_i,
                            confidence=ratio,
                            reason="fuzzy",
                        )
                    )

    candidates.sort(key=lambda c: (c.reason != "normalize", -c.confidence))
    return candidates[:max_results]


def _list_all_entities(db: "DbStore", *, domain: str | None) -> list[dict]:
    out: list[dict] = []
    page = 1
    page_size = 1000
    domains = [domain] if domain else _list_domains(db)
    for dom in domains:
        while True:
            rows, total = db.graph.list_entities(domain=dom, page=page, size=page_size)
            if not rows:
                break
            out.extend(rows)
            if len(out) >= total:
                break
            page += 1
        page = 1
    return out


def _list_domains(db: "DbStore") -> list[str]:
    if hasattr(db.graph, "list_domains"):
        return db.graph.list_domains() or []
    return []


def _event_count(db: "DbStore", entity_id: str) -> int:
    _rows, total = db.graph.get_entity_events(
        entity_id, include_deleted=False, page=1, size=1,
    )
    return int(total)


def _pick_canonical(db: "DbStore", members: list[dict]) -> dict:
    """Pick the entity with the most events as canonical. Tie → earliest created."""
    scored = [
        (_event_count(db, m["id"]), m.get("created_at") or "", m)
        for m in members
    ]
    # Highest event_count, then oldest created_at, then deterministic by id.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]["id"]))
    return scored[0][2]
