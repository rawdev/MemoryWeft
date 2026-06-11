"""App metadata — installed version + a best-effort latest-release check.

Powers the appbar "current version / update available" nudge. The latest-release
lookup hits the public GitHub Releases API; it is *best-effort* (short timeout,
cached, failures degrade to current-only) so the Manager never blocks or errors
on a missing/slow network. No auto-update is performed — we only surface a link
to the release page.
"""
from __future__ import annotations

import logging
import re
import time
from importlib.metadata import PackageNotFoundError, version as _dist_version
from typing import Any

import httpx
from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["app"])

# Public repo that ships the portable releases. Keep in sync with the publish
# target (scripts/mweft_publish.py origin).
_REPO = "rawdev/MemoryWeft"
_RELEASES_API = f"https://api.github.com/repos/{_REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{_REPO}/releases/latest"

# GitHub's unauthenticated API allows 60 req/h per IP; a long cache keeps us well
# clear and avoids a network round-trip on every appbar load.
_CACHE_TTL = 6 * 3600.0
_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def _current_version() -> str:
    """The installed distribution version.

    The wheel ships as ``mweft`` (OSS / portable) but the dev tree installs as
    ``k2g`` — try both before giving up so the appbar shows a real version in
    every layout.
    """
    for dist in ("mweft", "k2g"):
        try:
            return _dist_version(dist)
        except PackageNotFoundError:
            continue
    return "0.0.0-dev"


def _ver_tuple(v: str | None) -> tuple[int, ...]:
    """Numeric (major, minor, patch) for comparison. Ignores any pre/build suffix."""
    if not v:
        return (0,)
    core = re.split(r"[-+]", v.lstrip("vV"), maxsplit=1)[0]
    nums = [int(n) for n in re.findall(r"\d+", core)[:3]]
    return tuple(nums) or (0,)


def _fetch_latest() -> dict[str, Any] | None:
    """Latest release {tag, name, url, published_at}, cached. None when unknown.

    Best-effort: on any error we return the last cached value (possibly None) and
    never raise — the appbar simply shows the current version without a nudge.
    """
    now = time.time()
    cached = _cache.get("data")
    if cached is not None and now - _cache["ts"] < _CACHE_TTL:
        return cached
    try:
        r = httpx.get(
            _RELEASES_API,
            timeout=3.0,
            headers={"Accept": "application/vnd.github+json"},
        )
        r.raise_for_status()
        j = r.json()
        data = {
            "tag": j.get("tag_name"),
            "name": j.get("name"),
            "url": j.get("html_url") or _RELEASES_PAGE,
            "published_at": j.get("published_at"),
        }
        _cache.update(ts=now, data=data)
        return data
    except Exception as exc:  # noqa: BLE001 — update check is optional
        logger.debug("latest-release check failed: %s", exc)
        # Refresh the timestamp on failure too, so a flaky network doesn't make
        # every appbar load pay the 3s timeout.
        _cache["ts"] = now
        return cached


@router.get("/app/version")
def app_version() -> dict[str, Any]:
    """Current installed version + best-effort latest-release nudge.

    Returns ``{current, latest, update_available, release_url, release_name,
    checked}``. ``checked`` is False when the latest-release lookup could not be
    completed (offline / rate-limited) so the UI can stay quiet instead of
    implying the app is up to date.
    """
    current = _current_version()
    latest = _fetch_latest()
    latest_ver = (latest or {}).get("tag")
    if latest_ver:
        latest_ver = latest_ver.lstrip("vV")
    update = bool(latest_ver) and _ver_tuple(latest_ver) > _ver_tuple(current)
    return {
        "current": current,
        "latest": latest_ver,
        "update_available": update,
        "release_url": (latest or {}).get("url", _RELEASES_PAGE),
        "release_name": (latest or {}).get("name"),
        "checked": latest is not None,
    }
