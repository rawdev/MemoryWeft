"""Memory save-context shared layer.

Backend-neutral helpers extracted so that MCP ``remember`` (memory_tools) and
CLI ``ManifestProducer`` produce *identical* group / tag structures. When both
paths call only this module, domain enforcement / save-root / category cascade /
``K2G_USER_MEMORY_SAVE_TAGS`` enforcement are kept in sync.
"""

from k2g.memory.save_context import (
    DEFAULT_MEMORY_DOMAIN,
    TagResolution,
    attach_event_memberships,
    resolve_save_domain,
    resolve_tag_groups,
    resolve_working_folder,
)

__all__ = [
    "DEFAULT_MEMORY_DOMAIN",
    "TagResolution",
    "attach_event_memberships",
    "resolve_save_domain",
    "resolve_tag_groups",
    "resolve_working_folder",
]
