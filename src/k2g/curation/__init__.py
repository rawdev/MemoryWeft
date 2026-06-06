"""K2G curation track — entity merge + related recompute (BP-87).

Curation does *write* operations against the DbStore (alias mapping,
deprecated marking, audit trail, vector / jaccard recompute). It is kept
separate from `k2g.graph` (read-only algorithms) to make permission
boundaries explicit.
"""
from k2g.curation.entity_merge import MergeResult, merge_entities, unmerge_entity
from k2g.curation.jaccard_recompute import recompute_jaccard_for_entity
from k2g.curation.merge_candidates import MergeCandidate, find_candidates
from k2g.curation.vector_recompute import recompute_entity_vector

__all__ = [
    "merge_entities",
    "unmerge_entity",
    "find_candidates",
    "recompute_entity_vector",
    "recompute_jaccard_for_entity",
    "MergeResult",
    "MergeCandidate",
]
