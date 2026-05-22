"""Google Antigravity CLI source.

Filesystem layout (macOS, empirically observed 2026-05-22):
    ~/.gemini/antigravity-cli/brain/<conversation-uuid>/
        .system_generated/logs/
            transcript.jsonl       — turn-by-turn record (tool args re-JSON-stringified)
            transcript_full.jsonl  — same turns with native-JSON tool args (canonical)

Antigravity also stores an *encrypted* `.pb` file at
``~/.gemini/antigravity-cli/conversations/<uuid>.pb`` — we ignore that
(opaque blob, no schema published) and use the plaintext JSONL the CLI
writes alongside for its own observability. We pick the ``_full``
variant because tool-call ``args`` are stored as native JSON values
there, not re-stringified — closer to the canonical record.

Schema per line (object):
    {
      "step_index": int,
      "source":     "USER_EXPLICIT" | "SYSTEM" | "MODEL",
      "type":       "USER_INPUT" | "CONVERSATION_HISTORY" | "PLANNER_RESPONSE"
                  | "LIST_DIRECTORY" | <other tool-result type>,
      "status":     "DONE" | ...,
      "created_at": "ISO-8601",
      "content":    "string" (optional — present for messages and tool results),
      "tool_calls": [{"name": "...", "args": {...}}] (optional — present on
                    PLANNER_RESPONSE turns that invoke tools),
      "tool_results": [...] (optional — observed seldom; preserved verbatim
                    via fts inclusion for searchability)
    }

There is no per-line ``model`` field. The model identifier is mentioned
in line 0's ``content`` inside a ``<USER_SETTINGS_CHANGE>`` blob —
parsed best-effort here. If Antigravity ever introduces a structured
field, swap in that path. The manifest's ``models`` list is a forensic
nicety; absence won't break verification.

CRITICAL — sensitivity boundary:
    ``~/.gemini/`` contains ``oauth_creds.json``, ``google_accounts.json``,
    and tokens. The Source's ``default_source_paths`` deliberately
    returns ONLY ``~/.gemini/antigravity-cli/brain/`` and ``discover``
    refuses to traverse outside that root. ingest.py enforces this by
    treating the declared root as a hard ceiling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

from holotype.sources.base import Source, DepositCandidate, MessageInfo

# Maps Antigravity's `source` field to a Claude/Codex-style role string.
# Anything unknown falls through as the raw source value so we don't
# silently lose information.
_ROLE_MAP = {
    "USER_EXPLICIT": "user",
    "USER": "user",
    "MODEL": "assistant",
    "SYSTEM": "system",
}

# Best-effort model extraction from line-0 USER_SETTINGS_CHANGE content.
# Format observed: "The user changed setting `Model Selection` from None
# to Gemini 3.5 Flash (High). No need to comment..."
# The capture must accept periods inside the model name (e.g. "3.5") and
# stop at the next sentence end — a period followed by whitespace and a
# capital letter or newline.
_MODEL_RE = re.compile(
    r"`?Model Selection`?[^`\n]*?\bto\s+(.+?)\.\s+[A-Z\n]",
    re.IGNORECASE,
)

# Tool-result-like `type` values. Anything that starts with a known
# "tool name in screaming snake case" pattern is treated as a tool
# result — keeps fts_content full while flagging has_tool_use upstream.
# Conservative list; extend as new types appear in real transcripts.
_TOOL_RESULT_TYPES = {
    "LIST_DIRECTORY", "READ_FILE", "WRITE_FILE", "EDIT_FILE",
    "RUN_COMMAND", "RUN_SHELL", "TOOL_RESULT", "FILE_VIEW",
    "GREP_SEARCH", "CODEBASE_SEARCH", "SEARCH_WEB", "FETCH_URL",
}


class AntigravitySource(Source):
    name = "antigravity"

    @staticmethod
    def default_source_paths() -> list[Path]:
        # ONLY the brain dir. Never the antigravity-cli parent (history,
        # cache, conversations, log all sit there), never the .gemini
        # parent (oauth_creds.json, google_accounts.json live there).
        return [Path.home() / ".gemini" / "antigravity-cli" / "brain"]

    @staticmethod
    def session_id_from_filename(jsonl_path: Path) -> str:
        # Antigravity puts the session id in the parent-of-parent-of-parent
        # directory name (the conversation uuid), not the filename
        # itself (which is always "transcript_full.jsonl"). Walk up.
        # brain/<uuid>/.system_generated/logs/transcript_full.jsonl
        try:
            return jsonl_path.parents[2].name
        except IndexError:
            return jsonl_path.stem

    @staticmethod
    def discover(source_root: Path) -> Iterator[DepositCandidate]:
        if not source_root.exists():
            return
        try:
            source_root = source_root.resolve(strict=True)
        except OSError:
            return

        for conv_dir in sorted(source_root.iterdir()):
            if not conv_dir.is_dir() or conv_dir.name.startswith("."):
                continue
            transcript = conv_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
            if not transcript.is_file():
                continue
            # Refuse symlinks that escape source_root.
            try:
                transcript.resolve(strict=True).relative_to(source_root)
            except ValueError:
                continue
            sid = conv_dir.name
            yield DepositCandidate(
                source_name=AntigravitySource.name,
                jsonl_path=transcript,
                session_id=sid,
                archive_subpath=f"antigravity/{sid}",
                parent_session_id=None,
                project_dir_encoded="antigravity",
            )

    @staticmethod
    def parse_line(line: bytes) -> MessageInfo | None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None

        raw_source = obj.get("source") or ""
        raw_type = obj.get("type") or ""
        role = _ROLE_MAP.get(raw_source, raw_source.lower() or raw_type.lower() or None)
        timestamp = obj.get("created_at")

        flags: set[str] = set()
        parts: list[str] = []
        has_tool_use = False
        has_thinking = False
        model = None

        content = obj.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
            # Best-effort model parse from line-0 settings blob.
            if "Model Selection" in content:
                m = _MODEL_RE.search(content)
                if m:
                    model = m.group(1).strip()

        tool_calls = obj.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            has_tool_use = True
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tname = tc.get("name", "?")
                parts.append(f"[tool_use:{tname}]")
                args = tc.get("args")
                if isinstance(args, (dict, list)):
                    parts.append(json.dumps(args, ensure_ascii=False))
                elif isinstance(args, str):
                    parts.append(args)

        # Tool-result-shaped turns may carry the result body in `content`
        # (already captured) or in an explicit `tool_results` list.
        tool_results = obj.get("tool_results")
        if isinstance(tool_results, list):
            for tr in tool_results:
                if isinstance(tr, (dict, list)):
                    parts.append(json.dumps(tr, ensure_ascii=False))
                elif isinstance(tr, str):
                    parts.append(tr)

        if raw_type in _TOOL_RESULT_TYPES:
            has_tool_use = True

        return MessageInfo(
            role=role,
            timestamp=timestamp,
            model=model,
            has_tool_use=has_tool_use,
            has_thinking=has_thinking,
            fts_content="\n".join(p for p in parts if p),
            flags=flags,
        )
