"""Build per-session manifests from raw agent-CLI transcripts.

A manifest records the minimum metadata needed to interpret a transcript
later: SHA-256 of the raw file (chain of custody), models that appeared,
timestamp bookends, message count, the original cwd / project recorded
by the CLI, the source platform, and an environment snapshot. The
transcript itself is the source of truth; the manifest is a derived
index card you can cite from.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from holotype.compression import iter_transcript_lines, read_transcript_bytes
from holotype.hashing import sha256_bytes, sha256_file
from holotype.sources.base import Source


MANIFEST_VERSION = 3


@dataclass
class SessionManifest:
    """Per-session manifest. Serialized to manifest.json next to the transcript."""

    manifest_version: int
    source: str
    session_id: str
    project_dir_encoded: str
    project_dir_decoded: str | None
    transcript_filename: str
    sha256: str
    hash_algorithm: str
    byte_count: int
    message_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    models: list[str] = field(default_factory=list)
    has_tool_use: bool = False
    has_thinking: bool = False
    has_compaction: bool = False
    parent_session_id: str | None = None
    deposited_at: str = ""
    holotype_version: str = ""
    source_path: str = ""
    env: dict = field(default_factory=dict)
    # Compression fields (manifest_version >= 3). `compression` is None
    # (or absent on older manifests) when the transcript is stored as
    # plain .jsonl. When "zstd", the on-disk file is transcript.jsonl.zst
    # and `sha256_compressed` is its SHA-256 — letting a reviewer without
    # zstd verify the deposit by hashing the file as-is.
    compression: str | None = None
    sha256_compressed: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def project_dir_decoded(encoded: str) -> str | None:
    """Best-effort decode of Claude Code's project-dir naming convention.

    Claude Code encodes the cwd by replacing slashes with dashes, e.g.
    `/Users/x/Git/foo` -> `-Users-x-Git-foo`. This reverses that. Not
    guaranteed exact when the original path itself contained dashes.
    """
    if not encoded.startswith("-"):
        return None
    return encoded.replace("-", "/")


def scan_jsonl_lines(lines, source_cls: type[Source]) -> dict:
    """Single-pass scan of JSONL bytes-lines collecting all metadata.

    Returns a dict with: message_count, first_timestamp, last_timestamp,
    models (set), has_tool_use, has_thinking, has_compaction, project_dir.

    Operates on any iterable of bytes lines so the caller can pass an
    open file, a list, or the result of ``compression.iter_transcript_lines``.
    Lines that the Source's parser rejects (None) are not counted as
    messages — this keeps Codex's "record_type: state" filler from
    inflating message counts.
    """
    state = {
        "message_count": 0,
        "first_timestamp": None,
        "last_timestamp": None,
        "models": set(),
        "has_tool_use": False,
        "has_thinking": False,
        "has_compaction": False,
        "project_dir_cwd": None,
    }

    for line in lines:
        info = source_cls.parse_line(line)
        if info is None:
            continue
        # Skip the session-header pseudo-record (Codex line-1).
        if "header" in info.flags:
            if info.timestamp and state["first_timestamp"] is None:
                state["first_timestamp"] = info.timestamp
                state["last_timestamp"] = info.timestamp
            continue

        state["message_count"] += 1
        if info.timestamp:
            if state["first_timestamp"] is None:
                state["first_timestamp"] = info.timestamp
            state["last_timestamp"] = info.timestamp
        if info.model:
            state["models"].add(info.model)
        if info.has_tool_use:
            state["has_tool_use"] = True
        if info.has_thinking:
            state["has_thinking"] = True
        if "compaction" in info.flags:
            state["has_compaction"] = True

        # Source-specific: Claude Code carries cwd on user-message
        # turns. Codex carries repository_url in the line-1 header
        # (already captured above as timestamp); cwd-equivalent for
        # Codex would be parsing the <environment_context> blob,
        # which we skip for now to keep this layer source-agnostic.
        try:
            import json as _json
            obj = _json.loads(line)
            cwd = obj.get("cwd") if isinstance(obj, dict) else None
            if isinstance(cwd, str) and state["project_dir_cwd"] is None:
                state["project_dir_cwd"] = cwd
        except Exception:
            pass

    return state


def scan_jsonl(path: Path, source_cls: type[Source]) -> dict:
    """Scan a plain JSONL file on disk (backwards-compat wrapper)."""
    with path.open("rb") as f:
        return scan_jsonl_lines(f, source_cls)


def build_manifest(
    raw_jsonl: bytes,
    source_cls: type[Source],
    project_dir_encoded: str,
    *,
    session_id: str,
    on_disk_filename: str,
    holotype_version: str,
    source_path: str,
    env: dict | None = None,
    parent_session_id: str | None = None,
    compression: str | None = None,
    sha256_compressed: str | None = None,
) -> SessionManifest:
    """Build a SessionManifest from already-read uncompressed JSONL bytes.

    The caller has the raw bytes in hand (read from the source file
    under live-file safety in ingest.py). We hash and scan those bytes
    directly so the manifest's ``sha256`` is always the canonical
    uncompressed hash, regardless of whether the on-disk deposit is
    compressed.

    ``session_id`` is passed in because at deposit time the source
    filename has already been collapsed to ``transcript.jsonl`` and the
    Source-resolved session_id is the authority. ``on_disk_filename``
    is the actual name written under the session dir (``transcript.jsonl``
    or ``transcript.jsonl.zst``) — recorded so a reviewer can locate
    the file. When ``compression`` is set, ``sha256_compressed`` is the
    SHA-256 of the file as it sits on disk, supporting the
    no-zstd-installed verification track.
    """
    scan = scan_jsonl_lines(raw_jsonl.splitlines(keepends=True), source_cls)
    decoded_cwd = scan.get("project_dir_cwd")

    return SessionManifest(
        manifest_version=MANIFEST_VERSION,
        source=source_cls.name,
        session_id=session_id,
        project_dir_encoded=project_dir_encoded,
        project_dir_decoded=decoded_cwd or project_dir_decoded(project_dir_encoded),
        transcript_filename=on_disk_filename,
        sha256=sha256_bytes(raw_jsonl),
        hash_algorithm="sha256",
        byte_count=len(raw_jsonl),
        message_count=scan["message_count"],
        first_timestamp=scan["first_timestamp"],
        last_timestamp=scan["last_timestamp"],
        models=sorted(scan["models"]),
        has_tool_use=scan["has_tool_use"],
        has_thinking=scan["has_thinking"],
        has_compaction=scan["has_compaction"],
        parent_session_id=parent_session_id,
        deposited_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        holotype_version=holotype_version,
        source_path=source_path,
        env=env or {},
        compression=compression,
        sha256_compressed=sha256_compressed,
    )
