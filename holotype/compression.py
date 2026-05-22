"""Transcript compression for the archive (optional).

Compression is opt-in at ``init.py`` time and locked for the life of
the archive — it's a per-archive setting in ``.holotype/config.json``,
not a per-deposit choice. The reason is the hash chain: a session's
manifest records the SHA-256 of the uncompressed bytes (and, when
compression is on, ALSO the SHA-256 of the compressed file on disk),
so flipping the mode mid-archive would orphan one set of those
hashes.

Two-track verification:
  - Reviewer with ``zstd`` installed: decompress, hash, compare to
    ``manifest.sha256`` (the canonical uncompressed hash).
  - Reviewer without ``zstd``: hash the ``.jsonl.zst`` file as-is,
    compare to ``manifest.sha256_compressed``.

We shell out to the ``zstd`` binary instead of pulling in a Python
package. Rationale: holotype is otherwise stdlib-only, and ``zstd``
is ubiquitous on macOS (brew) and Linux (apt) — the same trust
surface as ``git`` / ``jq`` / ``shasum`` that ``VERIFY.md`` already
relies on.

Compression level is locked at ``-19 --long=27``. The choice is
boring on purpose: -19 is the highest level that still finishes in
reasonable wall time on JSONL, and ``--long=27`` enables a 128 MiB
window which helps on long sessions where the same tool-result
preamble repeats hundreds of times. Real-world ratio on Claude Code
JSONL is typically 1.5-3x depending on content (text-heavy turns
compress well; sessions dominated by base64 or random tool output
compress less).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

COMPRESSED_SUFFIX = ".zst"
COMPRESSED_TRANSCRIPT = "transcript.jsonl.zst"
PLAIN_TRANSCRIPT = "transcript.jsonl"

# Archival compression flags: -19 --long=27. Optimizes ratio over speed,
# right for the long-term storage case. The "fast" preset uses -3 instead
# and is ~3-5x faster on the compress step at a modest ratio cost
# (typically <10% larger files). Both write to .jsonl.zst; the file format
# is identical, only the encoder effort differs. sha256_compressed will
# vary per level, but sha256 (uncompressed canonical) is invariant — so
# verification still works the same way regardless of which level was used.
_ZSTD_FLAGS_ARCHIVAL = ["-19", "--long=27", "-q"]
_ZSTD_FLAGS_FAST = ["-3", "-q"]


def _flags_for_level(level: str) -> list[str]:
    if level == "fast":
        return _ZSTD_FLAGS_FAST
    if level == "archival":
        return _ZSTD_FLAGS_ARCHIVAL
    raise ValueError(f"unknown compression level: {level!r} (expected 'archival' or 'fast')")


class ZstdMissingError(RuntimeError):
    """Raised when zstd is required but not on PATH."""


def zstd_available() -> bool:
    return shutil.which("zstd") is not None


def _require_zstd() -> None:
    if not zstd_available():
        raise ZstdMissingError(
            "zstd binary not found on PATH. Install with `brew install zstd` "
            "or `apt install zstd`, or initialize the archive without "
            "compression (--compression none)."
        )


def compress_bytes(data: bytes, level: str = "archival") -> bytes:
    """Compress raw bytes with the chosen level preset.

    ``level="archival"`` (default) uses ``-19 --long=27`` — best ratio,
    slow. ``level="fast"`` uses ``-3`` — ~3-5× faster encode at modest
    ratio cost. Both produce valid zstd-framed bytes; decompression is
    identical for either level. ``ingest.py --fast-compress`` selects
    "fast" for first-time bulk backfill where wall time matters more
    than the last few percent of ratio.
    """
    _require_zstd()
    proc = subprocess.run(
        ["zstd", *_flags_for_level(level)],
        input=data,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def decompress_bytes(data: bytes) -> bytes:
    """Decompress zstd-framed bytes."""
    _require_zstd()
    proc = subprocess.run(
        ["zstd", "-d", "-q"],
        input=data,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def transcript_filename(compression: str | None) -> str:
    """The on-disk filename for a deposited transcript under this archive's mode."""
    if compression == "zstd":
        return COMPRESSED_TRANSCRIPT
    return PLAIN_TRANSCRIPT


def find_transcript(sess_dir: Path) -> tuple[Path, bool] | None:
    """Locate the transcript in a session dir regardless of compression mode.

    Returns ``(path, is_compressed)`` or ``None`` if neither variant
    exists. Prefers plain ``.jsonl`` over ``.jsonl.zst`` if both happen
    to be present (which shouldn't occur — archives are
    compression-uniform — but we'd rather pick the directly-verifiable
    one in that edge case).
    """
    plain = sess_dir / PLAIN_TRANSCRIPT
    if plain.exists():
        return (plain, False)
    compressed = sess_dir / COMPRESSED_TRANSCRIPT
    if compressed.exists():
        return (compressed, True)
    return None


def read_transcript_bytes(sess_dir: Path) -> bytes | None:
    """Return uncompressed transcript bytes from a session dir.

    Decompresses transparently if the deposit is ``.jsonl.zst``.
    Returns None if no transcript file is present. Raises
    ``ZstdMissingError`` if the deposit is compressed and ``zstd`` is
    not installed.
    """
    found = find_transcript(sess_dir)
    if found is None:
        return None
    path, is_compressed = found
    raw = path.read_bytes()
    if is_compressed:
        return decompress_bytes(raw)
    return raw


def iter_transcript_lines(sess_dir: Path):
    """Yield raw bytes-per-line from the transcript, decompressing if needed.

    Used by manifest scanning and FTS reindex so they don't care
    whether the deposit is compressed.
    """
    data = read_transcript_bytes(sess_dir)
    if data is None:
        return
    # Preserve trailing newlines; split keeping no empty final element when
    # the file ends with \n (the common case).
    for line in data.splitlines(keepends=True):
        yield line
