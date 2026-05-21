"""SHA-256 hashing for archive integrity.

The hash of a deposited transcript is the SHA-256 of the raw JSONL bytes,
exactly as Claude Code wrote them. No normalization, no line-ending fixes,
no whitespace stripping. Whatever is on disk is the specimen.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MiB


def sha256_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 of the file at `path`."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of an in-memory bytes object."""
    return hashlib.sha256(data).hexdigest()
