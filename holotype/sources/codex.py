"""OpenAI Codex CLI source.

Filesystem layout:
    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO-TS>-<UUID>.jsonl

Each file is the full transcript of one Codex session. There is no
sub-conversation file analog of Claude Code's `subagents/` directory.

Schema observed on real rollouts:

Line 1 — session header:
    {
      "id": "<uuid>", "timestamp": "ISO-8601",
      "instructions": null | "...",
      "git": {"commit_hash": "...", "branch": "...", "repository_url": "..."}
    }

Subsequent lines fall into a few shapes:

  Filler / state markers (skip from message_count):
    {"record_type": "state"}

  Messages:
    {"type": "message", "id": "msg_..."|null, "role": "user"|"assistant",
     "content": [{"type": "input_text"|"output_text", "text": "..."}]}

  Reasoning blocks:
    {"type": "reasoning", "id": "rs_...",
     "summary": [{"type": "summary_text", "text": "..."}]}

  Tool calls (varies; observed `function_call` / `function_call_output`,
  plus shell-call patterns linked by call_id):
    {"type": "function_call", "name": "...", "arguments": "json-string", ...}
    {"type": "function_call_output", "call_id": "...", "output": "..."}

We treat any line with type "function_call" / "function_call_output" /
"local_shell_call" / "local_shell_call_output" as tool use.

CRITICAL — sensitivity boundary:
    `~/.codex/` contains auth.json with credentials. The Source's
    `default_source_paths` deliberately returns ONLY `~/.codex/sessions/`
    and the discover() walk refuses to traverse upward. ingest.py
    enforces this by treating the source root as a hard ceiling.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator

from holotype.sources.base import Source, DepositCandidate, MessageInfo

# Match `rollout-<ISO>-<UUID>` and capture the UUID. The UUID is the
# session identifier Codex itself uses (line-1 "id" field matches it).
_ROLLOUT_RE = re.compile(
    r"^rollout-.+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

# Types of records that are tool invocations / their results.
_TOOL_TYPES = {"function_call", "function_call_output",
               "local_shell_call", "local_shell_call_output",
               "tool_use", "tool_result"}


class CodexSource(Source):
    name = "codex"

    @staticmethod
    def default_source_paths() -> list[Path]:
        # ONLY the sessions subdir. Never the parent (auth.json lives there).
        return [Path.home() / ".codex" / "sessions"]

    @staticmethod
    def session_id_from_filename(jsonl_path: Path) -> str:
        m = _ROLLOUT_RE.match(jsonl_path.stem)
        if m:
            return m.group(1)
        # Fall back to the full stem if the filename doesn't match the
        # rollout pattern — preserves uniqueness for non-standard files.
        return jsonl_path.stem

    @staticmethod
    def discover(source_root: Path) -> Iterator[DepositCandidate]:
        if not source_root.exists():
            return

        # source_root is expected to be `.../codex/sessions/`. We walk
        # YYYY/MM/DD/rollout-*.jsonl and refuse to escape source_root.
        try:
            source_root = source_root.resolve(strict=True)
        except OSError:
            return

        for jsonl in sorted(source_root.rglob("rollout-*.jsonl")):
            if not jsonl.is_file():
                continue
            try:
                jsonl.resolve(strict=True).relative_to(source_root)
            except ValueError:
                # Symlink that resolves outside source_root — refuse.
                continue
            sid = CodexSource.session_id_from_filename(jsonl)
            # Date-partition the archive to mirror Codex's own layout,
            # so a future "cite my codex April 2026 work" is one ls away.
            try:
                rel = jsonl.parent.relative_to(source_root)
                date_part = str(rel)  # "2026/04/24"
            except ValueError:
                date_part = "undated"
            yield DepositCandidate(
                source_name=CodexSource.name,
                jsonl_path=jsonl,
                session_id=sid,
                archive_subpath=f"codex/{date_part}/{sid}",
                parent_session_id=None,
                project_dir_encoded="codex",
            )

    @staticmethod
    def parse_line(line: bytes) -> MessageInfo | None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None

        # State markers and the line-1 session header carry no message.
        if obj.get("record_type") == "state":
            return None
        if "id" in obj and "timestamp" in obj and "git" in obj and "type" not in obj:
            # Session header line — record timestamp but no message body.
            return MessageInfo(
                role="session_header",
                timestamp=obj.get("timestamp"),
                model=None,
                has_tool_use=False,
                has_thinking=False,
                fts_content="",
                flags={"header"},
            )

        rtype = obj.get("type")
        role = obj.get("role")
        timestamp = obj.get("timestamp")
        model = obj.get("model")  # may be absent in observed rollouts; tolerant.
        flags: set[str] = set()
        parts: list[str] = []
        has_tool_use = False
        has_thinking = False

        if rtype == "message":
            content = obj.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    t = item.get("type")
                    if t in ("input_text", "output_text", "text"):
                        parts.append(item.get("text") or "")
            elif isinstance(content, str):
                parts.append(content)

        elif rtype == "reasoning":
            has_thinking = True
            summary = obj.get("summary")
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, dict) and isinstance(s.get("text"), str):
                        parts.append(s["text"])

        elif rtype in _TOOL_TYPES:
            has_tool_use = True
            parts.append(f"[{rtype}:{obj.get('name','?')}]")
            args = obj.get("arguments")
            if isinstance(args, str):
                parts.append(args)
            elif isinstance(args, dict):
                parts.append(json.dumps(args, ensure_ascii=False))
            out = obj.get("output")
            if isinstance(out, str):
                parts.append(out)

        else:
            # Unknown record type — include verbatim so search still finds it.
            parts.append(json.dumps(obj, ensure_ascii=False))

        return MessageInfo(
            role=role or rtype,
            timestamp=timestamp,
            model=model,
            has_tool_use=has_tool_use,
            has_thinking=has_thinking,
            fts_content="\n".join(p for p in parts if p),
            flags=flags,
        )
