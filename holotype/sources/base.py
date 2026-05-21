"""Abstract base for holotype Sources.

A Source represents one agent-CLI's session-transcript convention.
Adding a new agent CLI means subclassing Source and registering it in
holotype/sources/__init__.py — nothing in scripts/ingest.py changes.

Three things a Source must answer:

1. **Where to look on disk** for transcripts (and what's a sensitive
   peer to NEVER walk into — e.g. Codex's auth.json sits next to its
   sessions/).

2. **What identifies a session** — the on-disk session_id, where in
   the archive it goes, and what relationship (if any) it has to a
   parent session.

3. **How to parse a transcript line** — extract role / timestamp /
   model / has_tool_use / has_thinking flags for the manifest, and
   extract a flat searchable string for the FTS5 index. Forensic
   completeness means including tool calls and thinking — never
   filtering for "noise".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class DepositCandidate:
    """One transcript file the Source thinks should be ingested.

    `archive_subpath` is the relative path under the archive's
    `sessions/` directory where this deposit should land. Sources own
    their layout — `claude_code` uses `<project>/<session>/`, `codex`
    uses `codex/<YYYY-MM>/<session>/`, etc. Keeping platforms in
    different subtrees prevents session_id collisions and makes
    'cite all my codex work' / 'verify just the claude-code subtree'
    natural.
    """

    source_name: str
    jsonl_path: Path
    session_id: str
    archive_subpath: str
    parent_session_id: str | None = None
    project_dir_encoded: str = ""


@dataclass
class MessageInfo:
    """Per-line metadata extracted by a Source."""

    role: str | None
    timestamp: str | None
    model: str | None
    has_tool_use: bool
    has_thinking: bool
    fts_content: str
    # For Source-specific signals (compaction markers, etc.) that the
    # manifest scanner aggregates over the full file.
    flags: set[str]


class Source(ABC):
    """Interface contract for an agent-CLI source."""

    # Used in `manifest.json` and the archive subpath. Must be a stable,
    # lowercase, hyphen-separated identifier (e.g. "claude-code", "codex").
    name: str = ""

    @staticmethod
    @abstractmethod
    def default_source_paths() -> list[Path]:
        """Where, by default on macOS, do this CLI's transcripts live?

        Returned paths are NOT walked above. Each one is the deepest dir
        the Source has been promised never to escape (e.g. Codex's
        `~/.codex/sessions/`, not `~/.codex/` — auth.json sits at the
        parent level).
        """

    @staticmethod
    @abstractmethod
    def discover(source_root: Path) -> Iterator[DepositCandidate]:
        """Yield every deposit candidate under `source_root`.

        Sources are responsible for their own subagent / sub-conversation
        traversal. They MUST refuse to traverse outside `source_root`.
        """

    @staticmethod
    @abstractmethod
    def parse_line(line: bytes) -> MessageInfo | None:
        """Parse one JSONL line; return None for unparseable or filler lines.

        For lines that are "filler" (e.g. Codex's `{"record_type":"state"}`),
        return None so the manifest scanner doesn't count them as messages.
        """

    @staticmethod
    @abstractmethod
    def session_id_from_filename(jsonl_path: Path) -> str:
        """Extract the session_id from the on-disk filename.

        Claude Code uses `<uuid>.jsonl`. Codex uses `rollout-<ts>-<uuid>.jsonl`
        and we want just the trailing UUID.
        """
