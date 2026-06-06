"""BP-74 — Streaming tar.gz archive I/O for `.mweft.tar.gz` files.

The writer uses `SpooledTemporaryFile` so JSONL members of unbounded size do
not blow main-process memory.  The reader exposes a generator (`iter_jsonl`)
so importers can stream rows without ever holding the full table in RAM.

Determinism: `mtime` is fixed to 0 on every member so two exports of identical
content produce byte-identical archives.  Tests rely on this.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import shutil
import tarfile
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO, Callable

logger = logging.getLogger(__name__)

_SPOOL_THRESHOLD_BYTES = 8 * 1024 * 1024  # 8 MB before spilling to disk
_DEFAULT_COMPRESS_LEVEL = 6


def _default_json_dumps(obj: Any) -> str:
    """Default JSON serializer for dict rows.

    Caller (schema.py) injects a typed serializer that handles datetime/bytes.
    For tests and the manifest path, plain `json.dumps` is sufficient.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class ArchiveWriter:
    """Streaming writer for `.mweft.tar.gz`.

    Open in a `with` block so the underlying tarfile is closed even on error.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        compresslevel: int = _DEFAULT_COMPRESS_LEVEL,
    ) -> None:
        self.path = Path(path)
        self._compresslevel = compresslevel
        self._tar: tarfile.TarFile | None = None
        self._gz: gzip.GzipFile | None = None
        self._raw: BinaryIO | None = None

    def __enter__(self) -> ArchiveWriter:
        # Wrap gzip explicitly with mtime=0 + empty filename so the gzip
        # header is deterministic — two writes of identical content yield
        # byte-identical archives.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._raw = self.path.open("wb")
        self._gz = gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=self._compresslevel,
            fileobj=self._raw,
            mtime=0,
        )
        self._tar = tarfile.TarFile(mode="w", fileobj=self._gz)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None
        if self._gz is not None:
            self._gz.close()
            self._gz = None
        if self._raw is not None:
            self._raw.close()
            self._raw = None

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def add_text(self, name: str, content: str) -> None:
        """Embed a small UTF-8 text member directly (no spooling)."""
        data = content.encode("utf-8")
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mtime = 0
        self._tar_or_raise().addfile(info, fileobj=io.BytesIO(data))

    def add_jsonl(
        self,
        name: str,
        *,
        rows: Iterable[dict[str, Any]],
        header: dict[str, Any] | None = None,
        json_dumps: Callable[[Any], str] | None = None,
    ) -> int:
        """Stream rows as JSONL into a tar member.

        Returns the number of data rows written (excluding the optional
        `$header` line).
        """
        dumps = json_dumps or _default_json_dumps
        count = 0
        with tempfile.SpooledTemporaryFile(
            max_size=_SPOOL_THRESHOLD_BYTES,
            mode="w+b",
        ) as buf:
            if header is not None:
                buf.write(dumps({"$header": header}).encode("utf-8"))
                buf.write(b"\n")
            for row in rows:
                buf.write(dumps(row).encode("utf-8"))
                buf.write(b"\n")
                count += 1
            size = buf.tell()
            buf.seek(0)
            info = tarfile.TarInfo(name=name)
            info.size = size
            info.mtime = 0
            self._tar_or_raise().addfile(info, fileobj=buf)
        return count

    def add_file(self, name: str, source_path: str | Path) -> None:
        """Add an existing file from disk (e.g., segment dump)."""
        path = Path(source_path)
        info = self._tar_or_raise().gettarinfo(str(path), arcname=name)
        info.mtime = 0
        with path.open("rb") as fh:
            self._tar_or_raise().addfile(info, fileobj=fh)

    def add_bytes(self, name: str, content: bytes) -> None:
        """Embed raw bytes (e.g., binary blob)."""
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        info.mtime = 0
        self._tar_or_raise().addfile(info, fileobj=io.BytesIO(content))

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _tar_or_raise(self) -> tarfile.TarFile:
        if self._tar is None:
            raise RuntimeError(
                "ArchiveWriter must be used inside a `with` block."
            )
        return self._tar


class ArchiveReader:
    """Streaming reader for `.mweft.tar.gz`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._tar: tarfile.TarFile | None = None

    def __enter__(self) -> ArchiveReader:
        self._tar = tarfile.open(self.path, mode="r:gz")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def has_member(self, name: str) -> bool:
        try:
            self._tar_or_raise().getmember(name)
            return True
        except KeyError:
            return False

    def list_members(self) -> list[str]:
        return self._tar_or_raise().getnames()

    def read_text(self, name: str) -> str:
        f = self._tar_or_raise().extractfile(name)
        if f is None:
            raise FileNotFoundError(f"member not extractable: {name}")
        return f.read().decode("utf-8")

    def read_bytes(self, name: str) -> bytes:
        f = self._tar_or_raise().extractfile(name)
        if f is None:
            raise FileNotFoundError(f"member not extractable: {name}")
        return f.read()

    def iter_jsonl(
        self,
        name: str,
        *,
        with_header: bool = False,
    ) -> Iterator[Any]:
        """Stream JSONL rows from a tar member.

        If `with_header=False` (default), yields data rows only — the
        `$header` line is consumed silently.  If `with_header=True`,
        yields ``(header_dict, row_dict)`` tuples (header may be ``None``
        if the file has no header).
        """
        f = self._tar_or_raise().extractfile(name)
        if f is None:
            raise FileNotFoundError(f"member not extractable: {name}")
        text_stream = io.TextIOWrapper(f, encoding="utf-8", newline="")
        header: dict[str, Any] | None = None
        for raw_line in text_stream:
            line = raw_line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and "$header" in obj:
                header = obj["$header"]
                continue
            if with_header:
                yield header, obj
            else:
                yield obj

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _tar_or_raise(self) -> tarfile.TarFile:
        if self._tar is None:
            raise RuntimeError(
                "ArchiveReader must be used inside a `with` block."
            )
        return self._tar


class DirWriter:
    """Writer for an *unpacked* export folder (BP-74, folder format).

    Same public API as :class:`ArchiveWriter`, but writes plain files into a
    directory tree (``<root>/db/entities.jsonl`` …, ``<root>/manifest.json``)
    instead of tar members.  The result is a human-readable / diffable export
    folder — pick this over the single ``.mweft.tar.gz`` blob when the user
    wants to inspect or version-control the exported JSON directly.
    """

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root)

    def __enter__(self) -> DirWriter:
        self.path.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def _target(self, name: str) -> Path:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def add_text(self, name: str, content: str) -> None:
        self._target(name).write_text(content, encoding="utf-8")

    def add_jsonl(
        self,
        name: str,
        *,
        rows: Iterable[dict[str, Any]],
        header: dict[str, Any] | None = None,
        json_dumps: Callable[[Any], str] | None = None,
    ) -> int:
        """Write rows as a JSONL file. Returns data-row count (header excluded)."""
        dumps = json_dumps or _default_json_dumps
        count = 0
        with self._target(name).open("w", encoding="utf-8", newline="") as fh:
            if header is not None:
                fh.write(dumps({"$header": header}))
                fh.write("\n")
            for row in rows:
                fh.write(dumps(row))
                fh.write("\n")
                count += 1
        return count

    def add_file(self, name: str, source_path: str | Path) -> None:
        shutil.copyfile(Path(source_path), self._target(name))

    def add_bytes(self, name: str, content: bytes) -> None:
        self._target(name).write_bytes(content)


class DirReader:
    """Reader for an *unpacked* export folder (see :class:`DirWriter`)."""

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root)

    def __enter__(self) -> DirReader:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def has_member(self, name: str) -> bool:
        return (self.path / name).is_file()

    def list_members(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.path)).replace("\\", "/")
            for p in self.path.rglob("*")
            if p.is_file()
        )

    def read_text(self, name: str) -> str:
        target = self.path / name
        if not target.is_file():
            raise FileNotFoundError(f"member not found: {name}")
        return target.read_text(encoding="utf-8")

    def read_bytes(self, name: str) -> bytes:
        target = self.path / name
        if not target.is_file():
            raise FileNotFoundError(f"member not found: {name}")
        return target.read_bytes()

    def iter_jsonl(
        self,
        name: str,
        *,
        with_header: bool = False,
    ) -> Iterator[Any]:
        """Stream JSONL rows from a file member (lazy). Mirrors ArchiveReader."""
        target = self.path / name
        if not target.is_file():
            raise FileNotFoundError(f"member not found: {name}")
        header: dict[str, Any] | None = None
        with target.open("r", encoding="utf-8", newline="") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if isinstance(obj, dict) and "$header" in obj:
                    header = obj["$header"]
                    continue
                if with_header:
                    yield header, obj
                else:
                    yield obj
