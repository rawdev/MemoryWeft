"""Shared memory save-context / tag-cascade / forced-tag resolution.

Backend-neutral helpers extracted so that MCP ``remember``
(``k2g.mcp.memory_tools``) and CLI ``ManifestProducer``
(``k2g.producer.manifest``) produce the *same* group / tag structure.
As long as both paths call only this module, the following are guaranteed
to match:

- **forced domain** — ``K2G_USER_MEMORY_SAVE_DOMAIN`` env → fallback
  ``ai_memory``. Neither the LLM caller nor the manifest can override
  this (prevents domain scatter).
- **save-root** — ``K2G_USER_MEMORY_SAVE_GROUP`` as the single
  working_folder.
- **category cascade** — per-call tag resolved as a child group under
  the working_folder root. Normalized (case- and whitespace-insensitive)
  matching prevents fragmentation into near-duplicate groups.
- **forced save_tags** — *existing predefined groups* listed in
  ``K2G_USER_MEMORY_SAVE_TAGS`` (full name) are looked up by name and
  attached to every saved event via ``event_member_of`` (multi-membership).
  This is a separate axis from the per-call tag; it is always applied
  regardless of category on/off. Purpose: attach cross-cutting metadata
  (org name, user, etc.) that would not appear in event content.

This module depends only on backend-neutral graph store methods
(``link_or_create_group`` / ``link_event_member_of`` / ``list_groups``)
and ``graph._conn`` (raw lookup to check root existence).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_MEMORY_DOMAIN = "ai_memory"


# ---------------------------------------------------------------------------
# domain / working_folder resolution (env-forced / default)
# ---------------------------------------------------------------------------

def resolve_save_domain(settings: Any) -> tuple[str, dict[str, Any]]:
    """Resolve the save domain.

    Priority: ``K2G_USER_MEMORY_SAVE_DOMAIN`` env → fallback ``ai_memory``.
    The caller (LLM / manifest) cannot influence the result — only server
    env decides (prevents domain scatter). Returns
    ``(domain, applied_defaults)`` where applied_defaults always contains
    ``{"domain": ...}``.
    """
    domain = DEFAULT_MEMORY_DOMAIN
    if settings is not None:
        save_domain = getattr(settings, "user_memory_save_domain", None)
        if save_domain:
            domain = save_domain
    return domain, {"domain": domain}


def resolve_working_folder(
    settings: Any, working_folder: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """Apply ``K2G_USER_MEMORY_SAVE_GROUP`` env default when working_folder is None.

    Returns ``(working_folder, applied_defaults)``. When the env value is
    applied, applied_defaults contains a ``working_folder`` key; otherwise
    it is an empty dict.
    """
    applied: dict[str, Any] = {}
    if working_folder is None and settings is not None:
        save_group = getattr(settings, "user_memory_save_group", None)
        if save_group:
            working_folder = save_group
            applied["working_folder"] = working_folder
    return working_folder, applied


# ---------------------------------------------------------------------------
# tag cascade + forced save_tags
# ---------------------------------------------------------------------------

@dataclass
class TagResolution:
    """Result of ``resolve_tag_groups`` — groups the event will join + surface metadata.

    - ``group_id`` — leaf category group for the per-call tag (or root when
      category is off / not specified). Primary ``event_member_of`` target.
    - ``forced_tag_ids`` — forced save_tags group ids (multiple). Secondary
      membership for the event.
    - Remaining fields are for response surfacing
      (tag / tag_tree / applied_save_tags).
    """

    group_id: str
    group_created: bool
    category_enabled: bool
    category_resolved: str | None = None
    category_created: bool = False
    category_tree: list[str] = field(default_factory=list)
    forced_cfg: list[str] = field(default_factory=list)
    forced_tag_ids: list[str] = field(default_factory=list)

    @property
    def final_tag_created(self) -> bool:
        """Whether the final resolved tag (sub if present, else root) was
        created in this call."""
        if self.category_resolved is not None:
            return self.category_created
        return self.group_created


def resolve_tag_groups(
    graph: Any,
    *,
    domain: str,
    working_folder: str,
    category: str | None,
    settings: Any,
    forced_tags_override: list[str] | None = None,
) -> TagResolution:
    """Resolve root group + category cascade + forced save_tags.

    Extracts the group/tag logic from memory_tools.remember_tool §7.
    Calling with the same graph/settings produces an identical group
    structure (parity guarantee). Event attachment is done separately
    via :func:`attach_event_memberships`, supporting both deferred
    attachment (manifest produce/load) and immediate attachment (MCP).

    ``forced_tags_override`` (for CLI manifest document builds):
    - ``None`` (default): apply session forced tags from env
      ``K2G_USER_MEMORY_SAVE_TAGS`` (interactive remember behavior).
    - ``list`` (including empty list): use this list *instead of* the env
      session tags as the forced tags
      (source=``mweft_save_tag`` → SOURCE_AXIS ``forced``/Discovery).
      Document builds may have a different producer, so env session tags
      must not be inherited; user-confirmed tags are attached as forced.
      To suppress autotag, the caller passes ``category=None``
      (category cascade is then not run).
    """
    backend = "postgres" if "Postgres" in type(graph).__name__ else "sqlite"
    ph = "%s" if backend == "postgres" else "?"

    # 7.1 root group (working_folder) — preserve existing behavior
    cur = graph._conn.cursor()
    cur.execute(f"SELECT id FROM groups WHERE name = {ph}", (working_folder,))
    group_created = cur.fetchone() is None
    root_gid = graph.link_or_create_group(
        name=working_folder,
        level=0,
        domain=domain,
        source="working_folder",
    )

    # 7.2 category: resolve as a child group under the working_folder root.
    category_enabled = (
        settings is not None
        and getattr(settings, "user_memory_category", True)
    )
    category_resolved: str | None = None
    category_created = False
    category_tree: list[str] = []
    group_id = root_gid                 # default when category is off / not set

    if category_enabled:
        prefix = f"{working_folder}/"
        existing_by_lower = {
            g["name"].lower(): g["name"]
            for g in graph.list_groups(domain)
        }

        def _resolve_tag_under_root(
            path_str: str,
        ) -> tuple[str, bool, str | None]:
            """Resolve a slash-separated path under the working_folder root
            using cascade matching.

            Returns (leaf_group_id, created_this_call, resolved_subpath).
            An empty path returns (root_gid, False, None). Matching is
            case- and whitespace-insensitive to prevent fragmentation.
            """
            norm = [
                " ".join(seg.split())
                for seg in str(path_str).replace("\\", "/").split("/")
            ]
            norm = [p for p in norm if p]
            if not norm:
                return root_gid, False, None
            cum = working_folder
            parent = root_gid
            created = False
            for level, seg in enumerate(norm, start=1):
                proposed = f"{cum}/{seg}"
                exact = existing_by_lower.get(proposed.lower())
                if exact is not None:
                    cum = exact                        # reuse existing spelling
                else:
                    cum = proposed
                    created = True
                    existing_by_lower[cum.lower()] = cum   # reuse in later tags
                parent = graph.link_or_create_group(
                    name=cum,
                    level=level,
                    domain=domain,
                    parent_id=parent,
                    original_name=cum.rsplit("/", 1)[-1],
                    discriminator="category",
                    source="mweft_category",
                )
            return parent, created, cum[len(prefix):]

        # per-call tag — category sub-path chosen by the AI/LLM (context axis)
        if category and category.strip():
            group_id, category_created, category_resolved = (
                _resolve_tag_under_root(category)
            )

        # category_tree — all category paths under root (refetch to include new ones)
        category_tree = sorted(
            g["name"][len(prefix):]
            for g in graph.list_groups(domain)
            if g["name"].startswith(prefix)
        )

    # K2G_USER_MEMORY_SAVE_TAGS — forced tags attached to every save event.
    # Purpose: attach cross-cutting metadata (org, user, etc.) that would not
    # appear in event content. Separate axis from per-call tag/category;
    # always applied regardless of category on/off. These are existing
    # predefined groups chosen in the Manager UI (full name) — not sub-paths
    # under working_folder, so they are looked up by name directly rather than
    # re-nested via cascade (preserves cross-project provenance facet).
    # If missing (config drift), a flat group is created.
    forced_tag_ids: list[str] = []
    _forced_raw = (
        forced_tags_override
        if forced_tags_override is not None
        else (getattr(settings, "user_memory_save_tags", None) or [])
    )
    forced_cfg = [s for s in (str(t).strip() for t in _forced_raw) if s]
    if forced_cfg:
        existing_by_name = {g["name"]: g["id"] for g in graph.list_groups(domain)}
        for name in forced_cfg:
            fgid = existing_by_name.get(name)
            if fgid is None:
                fgid = graph.link_or_create_group(
                    name=name, level=0, domain=domain, source="mweft_save_tag",
                )
                existing_by_name[name] = fgid
            if fgid not in forced_tag_ids:
                forced_tag_ids.append(fgid)

    return TagResolution(
        group_id=group_id,
        group_created=group_created,
        category_enabled=category_enabled,
        category_resolved=category_resolved,
        category_created=category_created,
        category_tree=category_tree,
        forced_cfg=forced_cfg,
        forced_tag_ids=forced_tag_ids,
    )


def attach_event_memberships(
    graph: Any, event_id: str, res: TagResolution,
) -> None:
    """Attach the event to its per-call group (or root) + forced save_tags
    via ``event_member_of``.

    Primary: per-call tag (or root). Secondary: forced save_tags (ON
    CONFLICT DO NOTHING when the per-call group overlaps — handled by the
    graph backend).
    """
    graph.link_event_member_of(
        event_id=event_id, group_id=res.group_id, kind="contains",
    )
    for fgid in res.forced_tag_ids:
        if fgid != res.group_id:
            graph.link_event_member_of(
                event_id=event_id, group_id=fgid, kind="contains",
            )


__all__ = [
    "DEFAULT_MEMORY_DOMAIN",
    "TagResolution",
    "attach_event_memberships",
    "resolve_save_domain",
    "resolve_tag_groups",
    "resolve_working_folder",
]
