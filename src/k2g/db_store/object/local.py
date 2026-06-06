"""Object Storage -- local filesystem implementation.

- Stores raw binary/text data on disk
- URI format: file:///abs/path/domain/uuid.ext
- Append-only: existing files are never overwritten
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

logger = logging.getLogger(__name__)

# MIME type -> file extension mapping
_MIME_TO_EXT: dict[str, str] = {
    "text/plain": ".txt",
    "text/x-code": ".code",
    "text/html": ".html",
    "text/markdown": ".md",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "application/json": ".json",
    "application/pdf": ".pdf",
}


def _ext_for(content_type: str) -> str:
    """Return the file extension matching the given MIME type."""
    # Check custom mapping first
    if content_type in _MIME_TO_EXT:
        return _MIME_TO_EXT[content_type]
    # Fall back to mimetypes module
    ext = mimetypes.guess_extension(content_type)
    if ext:
        return ext
    return ".bin"


def _local_path_to_uri(path: Path) -> str:
    """Convert an absolute path to a file:// URI (cross-platform)."""
    return path.as_uri()


def _uri_to_local_path(storage_uri: str) -> Path:
    """Convert a file:// URI to a local path (cross-platform)."""
    if not storage_uri.startswith("file://"):
        raise ValueError(f"Local storage only supports file:// URIs: {storage_uri!r}")
    parsed = urlparse(storage_uri)
    path_str = url2pathname(parsed.path)
    return Path(path_str)


class LocalObjectStorage:
    """Local filesystem-based object storage.

    Directory layout per domain: ``{base_dir}/{domain}/{uuid}{ext}``
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Local object storage initialized: %s", self._base_dir)

    def put(
        self,
        data: bytes,
        content_type: str,
        domain: str,
        hint: str = "",
    ) -> str:
        """Store data and return its URI.

        Args:
            data: Binary data to store.
            content_type: MIME type.
            domain: Domain name (used for directory separation).
            hint: Optional filename hint (for debugging).

        Returns:
            URI in ``file:///abs/path/domain/uuid.ext`` format.
        """
        # Create domain directory
        domain_dir = self._base_dir / self._sanitize_dir_name(domain)
        domain_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique filename
        ext = _ext_for(content_type)
        file_id = uuid.uuid4().hex
        if hint:
            # Include hint in filename (max 32 chars, safe characters only)
            safe_hint = "".join(c if c.isalnum() or c in "-_" else "_" for c in hint[:32])
            filename = f"{file_id}_{safe_hint}{ext}"
        else:
            filename = f"{file_id}{ext}"

        file_path = domain_dir / filename

        # File should not already exist (UUID-based, so collision is extremely rare)
        if file_path.exists():
            logger.warning("File collision (very rare): %s", file_path)
            filename = f"{uuid.uuid4().hex}{ext}"
            file_path = domain_dir / filename

        file_path.write_bytes(data)

        uri = _local_path_to_uri(file_path)
        logger.debug("Object stored: uri=%s, size=%d bytes", uri, len(data))
        return uri

    def put_text(
        self,
        text: str,
        content_type: str,
        domain: str,
        hint: str = "",
        encoding: str = "utf-8",
    ) -> str:
        """Store text and return its URI. Convenience wrapper around put()."""
        return self.put(
            data=text.encode(encoding),
            content_type=content_type,
            domain=domain,
            hint=hint,
        )

    def get(self, storage_uri: str) -> bytes:
        """Read stored data from a URI.

        Args:
            storage_uri: file:// URI.

        Returns:
            Stored binary data.

        Raises:
            ValueError: Unsupported URI scheme.
            FileNotFoundError: File does not exist.
        """
        path = _uri_to_local_path(storage_uri)
        if not path.exists():
            # URI may have been created on a different host (e.g. Colab); try filename lookup
            path = self._remap_path(storage_uri)
            if path is None or not path.exists():
                raise FileNotFoundError(f"Object not found: {storage_uri}")
        return path.read_bytes()

    def _remap_path(self, storage_uri: str) -> Path | None:
        """Remap the relative path after objects/ to the current base_dir."""
        parsed_path = urlparse(storage_uri).path
        # Extract relative path after "objects/" (e.g. domain/filename.txt)
        marker = "/objects/"
        idx = parsed_path.find(marker)
        if idx >= 0:
            rel = parsed_path[idx + len(marker):]
            candidate = self._base_dir / url2pathname(rel)
            if candidate.exists():
                return candidate
        # Retry with filename only
        filename = Path(url2pathname(parsed_path)).name
        for p in self._base_dir.rglob(filename):
            return p
        return None

    def get_text(self, storage_uri: str, encoding: str = "utf-8") -> str:
        """Read and return text data from a URI."""
        return self.get(storage_uri).decode(encoding)

    def exists(self, storage_uri: str) -> bool:
        """Check whether the file at the given URI exists."""
        try:
            path = _uri_to_local_path(storage_uri)
            return path.exists()
        except ValueError:
            return False

    def get_size(self, storage_uri: str) -> int:
        """Return the file size in bytes."""
        path = _uri_to_local_path(storage_uri)
        return path.stat().st_size

    def get_age(self, storage_uri: str) -> float:
        """Return elapsed time since file creation in seconds."""
        import time
        path = _uri_to_local_path(storage_uri)
        return time.time() - path.stat().st_ctime

    def move(self, storage_uri: str, target_dir: str | Path) -> str:
        """Move a file to another directory and return the new URI.

        The original file is removed.
        """
        import shutil
        source_path = _uri_to_local_path(storage_uri)
        target = Path(target_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        dest = target / source_path.name
        shutil.move(str(source_path), str(dest))
        new_uri = _local_path_to_uri(dest)
        logger.info("Object moved: %s -> %s", storage_uri[:50], new_uri[:50])
        return new_uri

    @staticmethod
    def _sanitize_dir_name(name: str) -> str:
        """Remove/replace characters not safe for directory names."""
        return "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in name
        ).strip("_") or "default"

    def ping(self) -> bool:
        """Check whether the storage directory is accessible."""
        try:
            return self._base_dir.exists() and self._base_dir.is_dir()
        except Exception as e:
            logger.error("Object storage access failed: %s", e)
            return False

    def close(self) -> None:
        """No cleanup needed for filesystem-based storage."""
        logger.debug("Local object storage closed")

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
