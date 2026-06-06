"""Group Policy — unified ID conversion functions.

All Group IDs converge to the ``{domain}::{discriminator}/{normalized_id}``
format. Separate conversion functions are provided per ingestion path
(filesystem, Doxygen, VCS, DB) to guarantee that the same entity always
produces the same canonical name regardless of the entry path.

Ingestion code must always go through these functions rather than
assembling name strings directly.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


def _to_posix(raw_path: str) -> str:
    """Replace backslashes with forward slashes."""
    return raw_path.replace("\\", "/")


def _is_absolute(raw_path: str) -> bool:
    """Determine whether a path is absolute regardless of OS."""
    return PurePosixPath(raw_path).is_absolute() or PureWindowsPath(raw_path).is_absolute()


def _relative_to_root(raw_path: str, domain_root: str | Path) -> str:
    """Return the POSIX-style path relative to domain_root.

    Raises ValueError if an absolute path falls outside the root.
    Relative path inputs pass through as-is.
    """
    posix_path = _to_posix(str(raw_path))
    if not _is_absolute(posix_path):
        return posix_path.lstrip("./")

    root_posix = _to_posix(str(domain_root)).rstrip("/")
    try:
        path_obj = PurePosixPath(posix_path)
        root_obj = PurePosixPath(root_posix)
        rel = path_obj.relative_to(root_obj)
    except ValueError:
        try:
            win_path = PureWindowsPath(str(raw_path))
            win_root = PureWindowsPath(str(domain_root))
            rel = PurePosixPath(_to_posix(str(win_path.relative_to(win_root))))
        except ValueError as exc:
            raise ValueError(
                f"path is outside domain_root: path={raw_path!r}, root={domain_root!r}"
            ) from exc
    return str(rel)


def file2id(
    raw_path: str,
    domain_root: str | Path,
    domain: str,
    *,
    is_dir: bool = False,
) -> str:
    """Convert a file or directory path to a canonical Group ID.

    Rules:
      1. Absolute paths are made relative to domain_root
      2. Backslashes are replaced with forward slashes
      3. Entire path is lowercased
      4. Trailing '/' is appended when is_dir=True
      5. Prefix "{domain.lower()}::path/" is prepended
    """
    rel = _relative_to_root(raw_path, domain_root)
    rel = _to_posix(rel).lower().lstrip("/")
    while "//" in rel:
        rel = rel.replace("//", "/")
    if is_dir and not rel.endswith("/"):
        rel = rel + "/"
    return f"{domain.lower()}::path/{rel}"


def ns2id(
    fqn_or_ns: str,
    file_path: str | None,
    domain_root: str | Path,
    domain: str,
) -> str:
    """Convert a Doxygen FQN or namespace to a canonical Group ID.

    Class/function FQNs: delegates to file2id via file_path.
    Pure namespaces (file_path=None): not yet implemented as it
    requires a per-language mapping table.
    """
    if file_path is not None:
        return file2id(file_path, domain_root, domain)

    raise NotImplementedError(
        "Pure namespace to folder-path conversion requires a "
        f"per-language mapping table. Input: {fqn_or_ns!r}. "
        "Provide file_path or define an ns_to_path mapping."
    )


def vcs2id(
    changed_file_path: str,
    domain_root: str | Path,
    domain: str,
) -> str:
    """Convert a VCS changed-file path to a canonical Group ID (same rules as file2id)."""
    return file2id(changed_file_path, domain_root, domain)


def vcs_branch2id(branch_name: str, domain: str) -> str:
    """Convert a VCS branch name to a canonical Group ID."""
    return f"{domain.lower()}::vcs/branch/{branch_name.lower()}"


def db2id(db_name: str, table_name: str, domain: str) -> str:
    """Convert a DB table to a canonical Group ID."""
    return f"{domain.lower()}::db/{db_name.lower()}/{table_name.lower()}"


def expand_path_ancestors(leaf_canonical: str) -> list[tuple[str, bool]]:
    """Expand a leaf canonical path ID into a root-to-leaf ancestor chain.

    Example (file leaf)::

        "k2g::path/src/k2g/pipeline/ingestion.py" ->
        [
            ("k2g::path/src/",                              True),
            ("k2g::path/src/k2g/",                          True),
            ("k2g::path/src/k2g/pipeline/",                 True),
            ("k2g::path/src/k2g/pipeline/ingestion.py",     False),
        ]

    Example (directory leaf)::

        "k2g::path/src/k2g/pipeline/" ->
        [
            ("k2g::path/src/",                              True),
            ("k2g::path/src/k2g/",                          True),
            ("k2g::path/src/k2g/pipeline/",                 True),
        ]

    Rules:
      1. Split at ``{domain}::path/`` prefix, then decompose segments
      2. Intermediate directories always keep trailing ``/``
      3. A leaf ending with ``/`` is treated as a directory, else file
      4. Every root segment is included (e.g. ``k2g::path/src/``)
    """
    if "::path/" not in leaf_canonical:
        raise ValueError(
            f"expand_path_ancestors: not a path canonical id: {leaf_canonical!r}"
        )
    domain_part, rel = leaf_canonical.split("::path/", 1)
    prefix = f"{domain_part}::path/"
    is_dir_leaf = rel.endswith("/")
    rel_clean = rel.rstrip("/")
    if not rel_clean:
        return []
    segments = rel_clean.split("/")
    chain: list[tuple[str, bool]] = []
    last_idx = len(segments) - 1
    for i in range(len(segments)):
        sub_rel = "/".join(segments[: i + 1])
        if i == last_idx and not is_dir_leaf:
            chain.append((f"{prefix}{sub_rel}", False))
        else:
            chain.append((f"{prefix}{sub_rel}/", True))
    return chain
