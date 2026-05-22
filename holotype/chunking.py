"""Bin-pack session-directory paths into ~equal-size chunks for git push.

Used by:
  - ``scripts/ingest.py`` (since v1.2) — auto-chunk a bulk-initial commit
    when the projected pack would exceed GitHub's 2 GiB single-push
    ceiling.
  - ``scripts/repush_chunked.py`` (since v1.1.6) — recover archives whose
    initial bulk-push already failed against the same limit.

Algorithm: first-fit decreasing bin-packing. Sort the input dirs
largest-first; place each in the first chunk whose remaining headroom
fits it; open a new chunk if none fits. Projects larger than the
target get their own chunk (slightly over target) — better than
fracturing one project across multiple commits, because per-project
`git log` history stays coherent.
"""

from __future__ import annotations

import os
from pathlib import Path


def dir_size_bytes(path: Path) -> int:
    """Total byte size of a directory subtree (recursive). Returns 0 on errors."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return total


def bin_pack_paths(
    project_dirs: list[Path],
    archive: Path,
    target_bytes: int,
) -> list[list[str]]:
    """Group project subtrees into chunks under ``target_bytes`` each.

    ``project_dirs`` are paths like ``sessions/<project-dir-encoded>``
    relative to ``archive``. ``target_bytes`` is the per-chunk soft
    ceiling (default callers use ~1.5 GiB to stay well under GitHub's
    2 GiB push limit). Returns a list of chunks; each chunk is a list
    of relative-path strings suitable for ``git add``.
    """
    sized = [(p, dir_size_bytes(archive / p)) for p in project_dirs]
    sized.sort(key=lambda t: -t[1])
    chunks: list[tuple[list[str], int]] = []
    for rel, size in sized:
        placed = False
        for i, (chunk, chunk_size) in enumerate(chunks):
            if chunk_size + size <= target_bytes:
                chunk.append(str(rel))
                chunks[i] = (chunk, chunk_size + size)
                placed = True
                break
        if not placed:
            chunks.append(([str(rel)], size))
    return [c for c, _ in chunks]
