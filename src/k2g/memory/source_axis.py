"""Tag provenance (``groups.source``) → analysis axis / role mapping.

**Why this module exists.** Live code already stamps the ``source`` field
on groups (see ``save_context.resolve_tag_groups``):

- ``mweft_save_tag``  — Manager UI forced cross-cutting tag (human-assigned)
- ``mweft_category``  — LLM per-call category cascade
- ``working_folder``  — save-root (ai_memory) container marker
- (NULL)              — build pipeline (``ensure_group_tree`` /
                        ``register_path_group_with_ancestors``) does not stamp

In other words, provenance is not "mostly absent" — the memory path is
fully covered (just with different vocabulary), and only the build path
lacks stamps. The analysis vocabulary (forced/user/autotag/build/llm_build)
needs to *translate* the storage vocabulary.

This module is the **single source of truth** for that mapping
(storage label → analysis axis + role). It corresponds to the
(source axis × criterion) registry in §6.1: the ``axis`` column maps
to ``axis`` here, and the criterion profile maps to ``role`` /
``authority`` / ``discoverable``.

**Design decision (Strategy 2 = mapping layer).** The storage vocabulary
is not rewritten in the DB — we do not touch the hot remember path, and
we deliberately preserve legacy labels (see ``memory_tools`` comments).
Instead this map accepts both legacy and canonical labels and translates
them at analysis time. The only real gap (build path NULL) is filled by a
one-time backfill (``scripts/bp96_source_backfill.py``, NULL→build)
and future stamps.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- roles -------------------------------------------------------------------
ROLE_DISCOVERY = "discovery"   # forced/user: human/system ground-truth vs. structure
ROLE_QC = "qc"                 # autotag/llm_build: AI classification quality check
ROLE_BASELINE = "baseline"     # build: structural noise, useful only as cross-path bridge
ROLE_CONTAINER = "container"   # working_folder root: non-topical, excluded from criteria
ROLE_UNKNOWN = "unknown"       # NULL / unregistered: unknown provenance → skip typed axis


@dataclass(frozen=True)
class AxisProfile:
    """Analysis profile for a single storage source label.

    - ``axis``         — canonical source token (unified key for ranking/display).
    - ``role``         — criterion profile selector.
    - ``authority``    — 0–1. Discovery weight; reflects trustworthiness in hint ranking.
    - ``discoverable`` — whether this axis is included in residual discovery.
                         False = excluded from discovery output.
    - ``note``         — human-readable description.
    """

    axis: str
    role: str
    authority: float
    discoverable: bool
    note: str = ""


# Storage source labels (groups.source) → AxisProfile.
# Both legacy (live) labels and canonical labels are accepted as keys,
# so the DB can contain either without affecting the resolved profile.
SOURCE_AXIS: dict[str | None, AxisProfile] = {
    # --- canonical labels (new stamps / post-backfill) ---
    "forced":    AxisProfile("forced",    ROLE_DISCOVERY, 1.0, True,
                             "Manager human-assigned tag"),
    "user":      AxisProfile("user",      ROLE_DISCOVERY, 1.0, True,
                             "Group system decision (future)"),
    "autotag":   AxisProfile("autotag",   ROLE_QC,        0.4, False,
                             "LLM auto tag"),
    "llm_build": AxisProfile("llm_build", ROLE_QC,        0.4, False,
                             "LLM batch remember"),
    "build":     AxisProfile("build",     ROLE_BASELINE,  0.2, False,
                             "Ingestion structure tag"),
    # --- legacy live labels → canonical translation ---
    "mweft_save_tag": AxisProfile("forced",  ROLE_DISCOVERY, 1.0, True,
                                  "legacy→forced (Manager forced tag)"),
    "mweft_category": AxisProfile("autotag", ROLE_QC,        0.4, False,
                                  "legacy→autotag (LLM category)"),
    "working_folder": AxisProfile("autotag", ROLE_CONTAINER, 0.0, False,
                                  "save-root container (non-topical)"),
    "mweft_save_tag_root": AxisProfile("forced", ROLE_DISCOVERY, 1.0, True,
                                       "legacy variant"),
    # --- unknown provenance ---
    None: AxisProfile("unknown", ROLE_UNKNOWN, 0.0, False,
                      "unknown provenance → skip"),
}

_UNKNOWN = SOURCE_AXIS[None]


def resolve_axis(source: str | None) -> AxisProfile:
    """Resolve a ``groups.source`` value to an AxisProfile.
    Unregistered labels return the unknown profile."""
    if source is None:
        return _UNKNOWN
    return SOURCE_AXIS.get(source, _UNKNOWN)


def is_discoverable(source: str | None) -> bool:
    """Return whether this source is included in the residual discovery axis."""
    return resolve_axis(source).discoverable


def role_of(source: str | None) -> str:
    """Return the analysis role for this source."""
    return resolve_axis(source).role


def canonical_axis(source: str | None) -> str:
    """Normalize any legacy or canonical label to the canonical source token."""
    return resolve_axis(source).axis


__all__ = [
    "ROLE_DISCOVERY",
    "ROLE_QC",
    "ROLE_BASELINE",
    "ROLE_CONTAINER",
    "ROLE_UNKNOWN",
    "AxisProfile",
    "SOURCE_AXIS",
    "resolve_axis",
    "is_discoverable",
    "role_of",
    "canonical_axis",
]
