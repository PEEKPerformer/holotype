"""Build per-session manifests from raw Claude Code JSONLs.

A manifest records the minimum metadata needed to interpret a transcript
later: SHA-256 of the raw file (chain of custody), models that appeared,
timestamp bookends, message count, the original cwd Claude Code recorded,
and an environment snapshot. The transcript itself is the source of truth;
the manifest is a derived index card you can cite from.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from holotype.hashing import sha256_file


MANIFEST_VERSION = 1


@dataclass
class SessionManifest:
    """Per-session manifest. Serialized to manifest.json next to the transcript."""

    manifest_version: int
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


def session_id_from_filename(filename: str) -> str:
    """Extract the session UUID from a JSONL filename."""
    return Path(filename).stem


def scan_jsonl(path: Path) -> dict:
    """Single-pass scan of a JSONL collecting all metadata in one read.

    Returns a dict with: message_count, first_timestamp, last_timestamp,
    models (set), has_tool_use, has_thinking, has_compaction, project_dir.

    Tolerates malformed lines (skipped, not raised) because the JSONL is
    the source of truth and we never want metadata extraction to refuse
    to deposit a real transcript.
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

    with path.open("rb") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            state["message_count"] += 1

            ts = obj.get("timestamp")
            if isinstance(ts, str):
                if state["first_timestamp"] is None:
                    state["first_timestamp"] = ts
                state["last_timestamp"] = ts

            cwd = obj.get("cwd")
            if isinstance(cwd, str) and state["project_dir_cwd"] is None:
                state["project_dir_cwd"] = cwd

            if obj.get("isCompactSummary"):
                state["has_compaction"] = True

            msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
            if msg:
                model = msg.get("model")
                if isinstance(model, str):
                    state["models"].add(model)

                content = msg.get("content")
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        t = item.get("type")
                        if t == "tool_use":
                            state["has_tool_use"] = True
                        elif t == "thinking":
                            state["has_thinking"] = True

    return state


def build_manifest(
    jsonl_path: Path,
    project_dir_encoded: str,
    *,
    holotype_version: str,
    source_path: str,
    env: dict | None = None,
    parent_session_id: str | None = None,
) -> SessionManifest:
    """Build a SessionManifest from a JSONL file on disk.

    `project_dir_encoded` is the project-dir name as Claude Code writes it
    (e.g. `-Users-brendenferland-Git-ResistaMet-GUI`). `source_path` is
    where the JSONL was read from (for provenance — recorded in the
    manifest but never trusted).
    """
    scan = scan_jsonl(jsonl_path)
    decoded_cwd = scan.get("project_dir_cwd")

    return SessionManifest(
        manifest_version=MANIFEST_VERSION,
        session_id=session_id_from_filename(jsonl_path.name),
        project_dir_encoded=project_dir_encoded,
        project_dir_decoded=decoded_cwd or project_dir_decoded(project_dir_encoded),
        transcript_filename=jsonl_path.name,
        sha256=sha256_file(jsonl_path),
        hash_algorithm="sha256",
        byte_count=jsonl_path.stat().st_size,
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
    )
