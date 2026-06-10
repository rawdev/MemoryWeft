"""Prompt installer — copies/appends the MWeft prompt to MCP clients
that consume a per-project (or global) prompt file.

Some MCP clients have file-based system prompts (Claude Code's
``CLAUDE.md``, Cursor's ``.cursor/rules/*.md``). The installer
appends our block guarded by START/END markers so that:

- Re-install is idempotent (marker present → skip)
- Uninstall removes only our block (user's other content preserved)

Clients without a documented file-prompt convention
(Claude Desktop, Continue, ChatGPT) get a ``copy_paste_body`` for
the UI to display.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from k2g.installer.locator import detect_platform

logger = logging.getLogger(__name__)

START_MARKER = "<!-- mweft:prompt:begin -->"
END_MARKER = "<!-- mweft:prompt:end -->"

# Suffix for the one-time pristine backup taken before we first modify a user
# file (MCP config or prompt file). Lets the user revert an install.
BACKUP_SUFFIX = ".mweft-bak"


def backup_file_once(path: Path) -> str | None:
    """Snapshot ``path`` → ``path<.mweft-bak>`` once, before we modify it.

    No-op when the file does not exist (a brand-new file we create needs no
    backup) or when a backup is already present (preserve the *pristine*,
    pre-mweft copy across re-installs). Returns the backup path when one exists
    for this file, else ``None`` — so callers can tell the user recovery is
    possible. Never raises into the install path."""
    if not path.is_file():
        return None
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if not bak.exists():
        try:
            import shutil
            shutil.copy2(path, bak)
        except OSError as exc:
            logger.warning("backup of %s failed: %s", path, exc)
            return None
    return str(bak)

# BP-95 layered separation (design evt_c7aff47): the manifest-ingestion
# procedure ships as an AI-agnostic canonical guide + a Claude Skill wrapper.
# Claude Code keeps the Skill reference; every other client points at the
# neutral guide we drop — at ``<project>/.mweft/`` for project clients, or
# ``~/.mweft/`` for global clients (whose prompt also lives in a home file).
_MANIFEST_SKILL_REF = "`.claude/skills/manifest-ingestion/GUIDE.md` (Skill: `manifest-ingestion`)"
_MANIFEST_NEUTRAL_REF = "`.mweft/manifest-ingestion-guide.md` (or `k2g-ingest-manifest --help`)"
_MANIFEST_NEUTRAL_REF_GLOBAL = "`~/.mweft/manifest-ingestion-guide.md` (or `k2g-ingest-manifest --help`)"


def _template_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "ui" / "templates"


def load_template() -> str:
    """Return the canonical MWeft prompt body (no markers)."""
    return (_template_dir() / "CLAUDE.md").read_text(encoding="utf-8")


def _load_manifest_guide() -> str:
    """The AI-agnostic manifest-ingestion procedure (client-neutral)."""
    return (_template_dir() / "manifest_ingestion_guide.md").read_text(encoding="utf-8")


def _adapt_template(slug: str, body: str) -> str:
    """Claude keeps the Skill reference; other clients get the neutral guide.

    Project clients reference a repo-relative ``.mweft/…`` path; global clients
    (no project dir) reference the home copy ``~/.mweft/…``."""
    if slug == "claude_code":
        return body
    ref = _MANIFEST_NEUTRAL_REF_GLOBAL if slug.endswith("_global") else _MANIFEST_NEUTRAL_REF
    return body.replace(_MANIFEST_SKILL_REF, ref)


def _manifest_guide_target(slug: str, project_dir: str | Path | None) -> Path | None:
    """Where the neutral guide is dropped for non-Claude clients.

    Claude uses the Skill bundle (:func:`_claude_skill_targets`). Project clients
    get ``<project>/.mweft/…``; global-scope clients have no project dir but still
    install a home prompt file (``~/.codex/AGENTS.md`` etc.), so the guide goes to
    the home copy ``~/.mweft/…`` that the global prompt references (instead of the
    old ``--help``-only fallback)."""
    if slug == "claude_code":
        return None
    if slug.endswith("_global"):
        return Path.home() / ".mweft" / "manifest-ingestion-guide.md"
    if not project_dir:
        return None
    return Path(project_dir) / ".mweft" / "manifest-ingestion-guide.md"


def _claude_skill_targets(
    slug: str, project_dir: str | Path | None
) -> dict[Path, str] | None:
    """Claude Code manifest-ingestion Skill bundle → ``{dest_path: template}``.

    The prompt's manifest line references ``.claude/skills/manifest-ingestion/
    GUIDE.md`` (Skill: manifest-ingestion), so installing the Claude client must
    also drop the bundle — otherwise the reference dangles. The bootstrap path
    (``install_mcp_assets``) writes the same files; this makes the *install-AI*
    path self-contained too. Non-Claude clients use the neutral guide instead."""
    if slug != "claude_code" or not project_dir:
        return None
    base = Path(project_dir) / ".claude" / "skills" / "manifest-ingestion"
    return {
        base / "SKILL.md": "skill_manifest_ingestion.md",
        base / "GUIDE.md": "manifest_ingestion_guide.md",
    }


def _bounded_block(slug: str) -> str:
    body = _adapt_template(slug, load_template().rstrip())
    return f"{START_MARKER}\n{body}\n{END_MARKER}"


@dataclass
class PromptResult:
    slug: str
    target_path: str | None = None   # absolute path of the file we (would) write
    status: str = "pending"          # appended | already_present | preview | written |
                                     # removed | not_present | unsupported | failed
    detail: str = ""
    copy_paste_body: str | None = None
    backup_path: str | None = None   # pristine backup of the prompt file, if taken


def _claude_code_path(project_dir: str | Path | None) -> Path | None:
    if not project_dir:
        return None
    return Path(project_dir) / "CLAUDE.md"


def _cursor_project_path(project_dir: str | Path | None) -> Path | None:
    if not project_dir:
        return None
    return Path(project_dir) / ".cursor" / "rules" / "mweft.md"


def _cursor_global_path() -> Path:
    return Path.home() / ".cursor" / "rules" / "mweft.md"


def _gemini_project_path(project_dir: str | Path | None) -> Path | None:
    if not project_dir:
        return None
    return Path(project_dir) / "GEMINI.md"


def _codex_project_path(project_dir: str | Path | None) -> Path | None:
    if not project_dir:
        return None
    return Path(project_dir) / "AGENTS.md"


def _codex_global_path() -> Path:
    return Path.home() / ".codex" / "AGENTS.md"


def target_for(slug: str, *, project_dir: str | Path | None) -> Path | None:
    """Resolve target file for a given client slug, or ``None`` when this
    client has no canonical project-prompt file."""
    if slug == "claude_code":
        return _claude_code_path(project_dir)
    if slug == "cursor_project":
        return _cursor_project_path(project_dir)
    if slug == "cursor_global":
        return _cursor_global_path()
    if slug == "gemini_project":
        return _gemini_project_path(project_dir)
    if slug == "codex_project":
        return _codex_project_path(project_dir)
    if slug == "codex_global":
        return _codex_global_path()
    return None


def _append_block(path: Path, block: str) -> tuple[bool, str]:
    """Append ``block`` to ``path`` if not already present.

    Returns ``(was_appended, detail)``. ``was_appended=False`` means
    the markers were already in the file (idempotent skip).
    """
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if START_MARKER in text and END_MARKER in text:
            return False, f"already present in {path}"
        sep = "" if text.endswith("\n") else "\n"
        new_text = text + sep + "\n" + block + "\n"
    else:
        new_text = block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True, f"appended to {path}"


def _remove_block(path: Path) -> tuple[bool, str]:
    """Remove the bounded block from ``path``. Returns ``(removed, detail)``."""
    if not path.is_file():
        return False, f"no file at {path}"
    text = path.read_text(encoding="utf-8")
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        return False, f"markers not found in {path}"
    end_full = end + len(END_MARKER)
    # Also eat the trailing newline immediately after the END marker.
    while end_full < len(text) and text[end_full] in "\r\n":
        end_full += 1
    # And any blank line just before the START marker we inserted.
    while start > 0 and text[start - 1] in "\r\n":
        start -= 1
    new_text = text[:start] + text[end_full:]
    # If the file is now empty (was only our block + 1 newline), remove it.
    if not new_text.strip():
        path.unlink()
        return True, f"removed {path} (was our content only)"
    path.write_text(new_text, encoding="utf-8")
    return True, f"removed block from {path}"


def install_prompt(
    slug: str,
    *,
    project_dir: str | Path | None,
    dry_run: bool = False,
) -> PromptResult:
    """Install the MWeft prompt for one MCP client."""
    body = _bounded_block(slug)

    # Per-project clients need a project_dir before we can resolve the path.
    if slug in ("claude_code", "cursor_project", "gemini_project", "codex_project") and project_dir is None:
        return PromptResult(
            slug=slug,
            status="failed",
            detail=f"{slug} requires project_dir",
            copy_paste_body=load_template(),
        )

    target = target_for(slug, project_dir=project_dir)
    if target is None:
        # Client has no file-prompt convention — return copy/paste body.
        return PromptResult(
            slug=slug,
            status="unsupported",
            detail="No file-based system prompt for this client — copy/paste it.",
            copy_paste_body=load_template(),
        )

    guide_target = _manifest_guide_target(slug, project_dir)
    skill_targets = _claude_skill_targets(slug, project_dir)

    if dry_run:
        detail = (
            f"would append to {target}" if target.is_file()
            else f"would create {target}"
        )
        if guide_target is not None:
            detail += f"; would write manifest guide → {guide_target}"
        if skill_targets is not None:
            detail += "; would install manifest-ingestion skill (.claude/skills/)"
        return PromptResult(
            slug=slug,
            target_path=str(target),
            status="preview",
            detail=detail,
            copy_paste_body=body,
        )

    # Back up the prompt file (CLAUDE.md / AGENTS.md / …) before we append, so
    # the user can revert. No-op for a file we're creating fresh.
    backup = backup_file_once(target)

    try:
        appended, detail = _append_block(target, body)
    except OSError as exc:
        return PromptResult(
            slug=slug,
            target_path=str(target),
            status="failed",
            detail=f"cannot write {target}: {exc}",
            copy_paste_body=body,
            backup_path=backup,
        )

    # BP-95: manifest-ingestion procedure — Claude gets the Skill bundle, every
    # other project client gets the AI-agnostic neutral guide. Install whichever
    # applies so the prompt's reference never dangles.
    detail += _write_manifest_guide(guide_target)
    detail += _install_claude_skill(skill_targets)

    return PromptResult(
        slug=slug,
        target_path=str(target),
        status="appended" if appended else "already_present",
        detail=detail,
        backup_path=backup,
    )


def _write_manifest_guide(guide_target: Path | None) -> str:
    """Write the neutral manifest guide if needed. Returns a detail suffix."""
    if guide_target is None:
        return ""
    if guide_target.is_file():
        return f"; manifest guide present ({guide_target})"
    try:
        guide_target.parent.mkdir(parents=True, exist_ok=True)
        guide_target.write_text(_load_manifest_guide(), encoding="utf-8")
    except OSError as exc:
        return f"; manifest guide write failed: {exc}"
    return f"; manifest guide → {guide_target}"


def _install_claude_skill(skill_targets: dict[Path, str] | None) -> str:
    """Write the Claude manifest-ingestion Skill bundle (idempotent per file).

    Returns a detail suffix. Existing files are left untouched so a user's edits
    survive re-install."""
    if not skill_targets:
        return ""
    wrote = False
    for dest, template_name in skill_targets.items():
        if dest.is_file():
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                (_template_dir() / template_name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            wrote = True
        except OSError as exc:
            return f"; skill install failed: {exc}"
    base = next(iter(skill_targets)).parent
    return f"; installed manifest-ingestion skill ({base})" if wrote \
        else f"; manifest-ingestion skill present ({base})"


def _remove_manifest_assets(slug: str, project_dir: str | Path | None) -> str:
    """Remove the manifest-ingestion artifacts that :func:`install_prompt` dropped,
    so uninstall is symmetric and leaves no orphans.

    Mirrors :func:`_install_claude_skill` (Claude Skill bundle under
    ``.claude/skills/manifest-ingestion/``) and :func:`_write_manifest_guide`
    (neutral guide at ``.mweft/manifest-ingestion-guide.md``). Only mweft's own
    files are deleted; the ``manifest-ingestion`` folder is removed only when it
    is left empty (a user's extra files survive), and the parent ``.claude/skills``
    directory and any unrelated skills are never touched. Best-effort: an OS error
    on any single path is swallowed. Returns a detail suffix."""
    removed: list[str] = []
    skill_targets = _claude_skill_targets(slug, project_dir)
    if skill_targets:
        skill_dir: Path | None = None
        for dest in skill_targets:
            skill_dir = dest.parent
            try:
                if dest.is_file():
                    dest.unlink()
                    removed.append(dest.name)
            except OSError:
                continue
        # Drop the manifest-ingestion folder only when empty (only mweft's bundle).
        if skill_dir is not None:
            try:
                if skill_dir.is_dir() and not any(skill_dir.iterdir()):
                    skill_dir.rmdir()
            except OSError:
                pass
    guide_target = _manifest_guide_target(slug, project_dir)
    if guide_target is not None:
        try:
            if guide_target.is_file():
                guide_target.unlink()
                removed.append(guide_target.name)
        except OSError:
            pass
    return f"; removed manifest-ingestion assets ({', '.join(removed)})" if removed else ""


def uninstall_prompt(
    slug: str,
    *,
    project_dir: str | Path | None,
) -> PromptResult:
    """Remove the MWeft block from a prompt file (idempotent).

    Also strips the manifest-ingestion assets the install path wrote (Claude Skill
    bundle / neutral guide), so removing mweft cleans up after itself — see
    :func:`_remove_manifest_assets`."""
    target = target_for(slug, project_dir=project_dir)
    if target is None:
        return PromptResult(
            slug=slug, status="unsupported",
            detail="no file-based prompt — nothing to remove",
        )
    try:
        removed, detail = _remove_block(target)
    except OSError as exc:
        return PromptResult(
            slug=slug, target_path=str(target),
            status="failed", detail=f"cannot edit {target}: {exc}",
        )
    # Mirror install: clean up the manifest-ingestion bundle/guide too. Run even
    # when the prompt block was absent so orphaned assets are always swept.
    detail += _remove_manifest_assets(slug, project_dir=project_dir)
    return PromptResult(
        slug=slug,
        target_path=str(target),
        status="removed" if removed else "not_present",
        detail=detail,
    )
