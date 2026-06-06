"""Covenant Validator — covenant rule verification.

Checks each CovenantRecord against the current environment (files/directories,
git branch, DB connection) and classifies results by ok/warning/error/fatal
severity.
"""

from __future__ import annotations

import fnmatch
import glob as _glob
import logging
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from k2g.stores.covenant_store import CovenantRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CovenantCheckResult:
    record: CovenantRecord
    passed: bool
    severity: str  # 'ok'|'warning'|'error'|'fatal'
    detail: str


class CovenantValidator:
    VALID_GROUP_POLICIES = frozenset({"path", "vcs", "db"})

    def validate(self, records: list[CovenantRecord]) -> list[CovenantCheckResult]:
        results: list[CovenantCheckResult] = []
        for rec in records:
            if rec.group_policy not in self.VALID_GROUP_POLICIES:
                results.append(CovenantCheckResult(
                    rec, False, "fatal",
                    f"group_policy '{rec.group_policy}' is outside "
                    f"defined set (valid: {sorted(self.VALID_GROUP_POLICIES)})",
                ))
                continue
            if rec.type == "filesystem":
                results.append(self._check_filesystem(rec))
            elif rec.type == "vcs":
                results.append(self._check_vcs(rec))
            elif rec.type == "database":
                results.append(self._check_database(rec))
            elif rec.type == "email":  # BP-46
                results.append(self._check_email(rec))
            elif rec.type == "rss":  # BP-46
                results.append(self._check_rss(rec))
            else:
                results.append(CovenantCheckResult(
                    rec, False, "fatal", f"unknown type: {rec.type}",
                ))
        return results

    # ------------------------------------------------------------------
    # email / rss validators
    # ------------------------------------------------------------------

    def _check_email(self, rec: CovenantRecord) -> CovenantCheckResult:
        """email source — validate imap_url format (no actual connection).

        Format: ``imap[s]://[user@]host[:port][/folder]``. Both config.imap_url
        and config.url are accepted.
        """
        cfg = rec.config or {}
        url = cfg.get("imap_url") or cfg.get("url")
        if not url:
            return CovenantCheckResult(
                rec, False, "error",
                "email: config.imap_url not set (e.g. imap://user@host:993/INBOX)",
            )
        try:
            parsed = urlparse(url)
        except Exception as exc:
            return CovenantCheckResult(
                rec, False, "error", f"email: imap_url parse failed ({exc})",
            )
        if parsed.scheme not in ("imap", "imaps"):
            return CovenantCheckResult(
                rec, False, "error",
                f"email: scheme must be imap/imaps (got: {parsed.scheme})",
            )
        if not parsed.hostname:
            return CovenantCheckResult(
                rec, False, "error", "email: host missing",
            )
        return CovenantCheckResult(
            rec, True, "ok", f"email: {parsed.scheme}://{parsed.hostname}",
        )

    def _check_rss(self, rec: CovenantRecord) -> CovenantCheckResult:
        """rss source — validate feed_url format (no actual fetch)."""
        cfg = rec.config or {}
        url = cfg.get("feed_url") or cfg.get("url")
        if not url:
            return CovenantCheckResult(
                rec, False, "error",
                "rss: config.feed_url not set (e.g. https://example.com/rss)",
            )
        try:
            parsed = urlparse(url)
        except Exception as exc:
            return CovenantCheckResult(
                rec, False, "error", f"rss: feed_url parse failed ({exc})",
            )
        if parsed.scheme not in ("http", "https"):
            return CovenantCheckResult(
                rec, False, "error",
                f"rss: scheme must be http/https (got: {parsed.scheme})",
            )
        if not parsed.hostname:
            return CovenantCheckResult(
                rec, False, "error", "rss: host missing",
            )
        return CovenantCheckResult(
            rec, True, "ok", f"rss: {parsed.scheme}://{parsed.hostname}",
        )

    # ------------------------------------------------------------------
    # filesystem
    # ------------------------------------------------------------------

    def _check_filesystem(self, rec: CovenantRecord) -> CovenantCheckResult:
        cfg = rec.config
        root = cfg.get("root")
        if not root:
            return CovenantCheckResult(rec, False, "error", "filesystem: config.root not set")

        root_path = Path(root)
        if not root_path.is_dir():
            return CovenantCheckResult(
                rec, False, "error", f"filesystem: root path not found ({root})",
            )

        includes: list[str] = cfg.get("include") or []
        excludes: list[str] = cfg.get("exclude") or []

        invalid_excludes: list[str] = []
        for pat in excludes:
            if not isinstance(pat, str):
                invalid_excludes.append(f"{pat!r} (not str)")
                continue
            try:
                fnmatch.translate(pat)
            except Exception as exc:
                invalid_excludes.append(f"{pat} ({exc})")
        if invalid_excludes:
            return CovenantCheckResult(
                rec, True, "warning",
                f"filesystem: root={root}, exclude pattern syntax error: {invalid_excludes}",
            )

        if not includes:
            return CovenantCheckResult(
                rec, True, "ok",
                f"filesystem: root={root} (no include patterns, {len(excludes)} excludes)",
            )

        match_count = 0
        for pattern in includes:
            full = str(root_path / pattern)
            for _ in _glob.iglob(full, recursive=True):
                match_count += 1
                if match_count > 0:
                    break
            if match_count > 0:
                break

        if match_count == 0:
            return CovenantCheckResult(
                rec, True, "warning",
                f"filesystem: root={root}, include={includes} — 0 matching files",
            )

        return CovenantCheckResult(
            rec, True, "ok",
            f"filesystem: root={root}, include={includes}, "
            f"{len(excludes)} excludes — matches found",
        )

    # ------------------------------------------------------------------
    # vcs
    # ------------------------------------------------------------------

    def _check_vcs(self, rec: CovenantRecord) -> CovenantCheckResult:
        cfg = rec.config
        vcs_type = cfg.get("vcs_type", "git")
        expected_branch = cfg.get("branch")
        cwd = cfg.get("cwd", ".")

        if vcs_type != "git":
            return CovenantCheckResult(
                rec, True, "warning", f"vcs: {vcs_type} validation unsupported (git only)",
            )

        if not expected_branch:
            return CovenantCheckResult(
                rec, False, "error", "vcs: config.branch not set",
            )

        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=5,
            )
        except FileNotFoundError:
            return CovenantCheckResult(rec, False, "error", "vcs: git command not found")
        except subprocess.TimeoutExpired:
            return CovenantCheckResult(rec, False, "error", "vcs: git rev-parse timeout")

        if proc.returncode != 0:
            return CovenantCheckResult(
                rec, False, "error",
                f"vcs: git call failed (cwd={cwd}): {proc.stderr.strip()[:120]}",
            )

        current = proc.stdout.strip()
        if current != expected_branch:
            return CovenantCheckResult(
                rec, False, "error",
                f"vcs: current branch '{current}' != covenant '{expected_branch}'",
            )

        patterns = cfg.get("jaccard_exclude_patterns") or []
        invalid: list[str] = []
        if patterns:
            if not isinstance(patterns, list):
                return CovenantCheckResult(
                    rec, True, "warning",
                    f"vcs: branch={current} ok, jaccard_exclude_patterns is not a list",
                )
            for pat in patterns:
                if not isinstance(pat, str):
                    invalid.append(f"{pat!r} (not str)")
                    continue
                try:
                    fnmatch.translate(pat)
                except Exception as exc:
                    invalid.append(f"{pat} ({exc})")

        if invalid:
            return CovenantCheckResult(
                rec, True, "warning",
                f"vcs: branch={current} ok, jaccard_exclude_patterns syntax error: {invalid}",
            )

        return CovenantCheckResult(
            rec, True, "ok",
            f"vcs: git branch={current} (match), {len(patterns)} stop-list patterns",
        )

    # ------------------------------------------------------------------
    # database
    # ------------------------------------------------------------------

    def _check_database(self, rec: CovenantRecord) -> CovenantCheckResult:
        cfg = rec.config
        conn = cfg.get("connection")
        if not conn:
            return CovenantCheckResult(
                rec, False, "error", "database: config.connection not set",
            )

        if conn.startswith("sqlite:///"):
            path = conn[len("sqlite:///"):]
            if Path(path).exists():
                return CovenantCheckResult(
                    rec, True, "ok", f"database: sqlite file ok ({path})",
                )
            return CovenantCheckResult(
                rec, False, "error", f"database: sqlite file not found ({path})",
            )

        if conn.startswith(("postgresql://", "postgres://", "mysql://")):
            parsed = urlparse(conn)
            host = parsed.hostname
            port = parsed.port or (5432 if "postgres" in conn else 3306)
            if not host:
                return CovenantCheckResult(
                    rec, False, "error", f"database: host parse failed ({conn})",
                )
            try:
                with socket.create_connection((host, port), timeout=2):
                    pass
            except OSError as e:
                return CovenantCheckResult(
                    rec, False, "error",
                    f"database: TCP unreachable {host}:{port} ({e})",
                )
            return CovenantCheckResult(
                rec, True, "ok",
                f"database: TCP reachable {host}:{port} (auth/schema not verified)",
            )

        return CovenantCheckResult(
            rec, True, "warning",
            f"database: unsupported prefix — not verified ({conn[:40]}...)",
        )


__all__ = ["CovenantValidator", "CovenantCheckResult"]
