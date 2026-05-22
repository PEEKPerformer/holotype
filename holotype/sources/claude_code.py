"""Claude Code (Anthropic) source.

Filesystem layout:
    <source>/<project-dir>/<session-uuid>.jsonl                 (top-level)
    <source>/<project-dir>/<session-uuid>/subagents/<id>.jsonl  (subagent)

Schema per line (object):
    {
      "uuid": "...", "parentUuid": "...",
      "type": "user"|"assistant"|...,
      "isSidechain": bool,
      "isCompactSummary": bool (optional),
      "timestamp": "ISO-8601",
      "cwd": "/abs/path" (optional),
      "message": {
        "role": "user"|"assistant",
        "model": "claude-...-x-y" (assistant only),
        "content": [
          {"type": "text", "text": "..."},
          {"type": "thinking", "thinking": "..."},
          {"type": "tool_use", "name": "...", "input": {...}},
          {"type": "tool_result", "content": ...}
        ]
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from holotype.sources.base import Source, DepositCandidate, MessageInfo


class ClaudeCodeSource(Source):
    name = "claude-code"

    @staticmethod
    def default_source_paths() -> list[Path]:
        return [
            Path.home() / "Documents" / "Claude-Backups",
            Path.home() / ".claude" / "projects",
        ]

    @staticmethod
    def session_id_from_filename(jsonl_path: Path) -> str:
        return jsonl_path.stem

    @staticmethod
    def discover(source_root: Path) -> Iterator[DepositCandidate]:
        if not source_root.exists():
            return
        for project_dir in sorted(source_root.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            project_dir_encoded = project_dir.name

            for jsonl in sorted(project_dir.glob("*.jsonl")):
                if not jsonl.is_file():
                    continue
                sid = ClaudeCodeSource.session_id_from_filename(jsonl)
                yield DepositCandidate(
                    source_name=ClaudeCodeSource.name,
                    jsonl_path=jsonl,
                    session_id=sid,
                    archive_subpath=f"{project_dir_encoded}/{sid}",
                    parent_session_id=None,
                    project_dir_encoded=project_dir_encoded,
                )

            for parent_dir in sorted(p for p in project_dir.iterdir() if p.is_dir()):
                subagents_dir = parent_dir / "subagents"
                if not subagents_dir.is_dir():
                    continue
                for sub_jsonl in sorted(subagents_dir.glob("*.jsonl")):
                    if not sub_jsonl.is_file():
                        continue
                    sid = ClaudeCodeSource.session_id_from_filename(sub_jsonl)
                    yield DepositCandidate(
                        source_name=ClaudeCodeSource.name,
                        jsonl_path=sub_jsonl,
                        session_id=sid,
                        archive_subpath=f"{project_dir_encoded}/{parent_dir.name}/subagents/{sid}",
                        parent_session_id=parent_dir.name,
                        project_dir_encoded=project_dir_encoded,
                    )

    @staticmethod
    def parse_line(line: bytes) -> MessageInfo | None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None

        flags: set[str] = set()
        if obj.get("isCompactSummary"):
            flags.add("compaction")

        msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
        role = (msg.get("role") if msg else None) or obj.get("type")
        model = msg.get("model") if msg else None
        timestamp = obj.get("timestamp")
        usage = msg.get("usage") if msg else None
        if not isinstance(usage, dict):
            usage = None

        has_tool_use = False
        has_thinking = False
        parts: list[str] = []

        if msg and isinstance(msg.get("content"), list):
            for item in msg["content"]:
                if not isinstance(item, dict):
                    continue
                t = item.get("type")
                if t == "text":
                    parts.append(item.get("text") or "")
                elif t == "thinking":
                    parts.append(item.get("thinking") or "")
                    has_thinking = True
                elif t == "tool_use":
                    has_tool_use = True
                    parts.append(f"[tool_use:{item.get('name','?')}]")
                    inp = item.get("input")
                    if isinstance(inp, dict):
                        parts.append(json.dumps(inp, ensure_ascii=False))
                elif t == "tool_result":
                    inner = item.get("content")
                    if isinstance(inner, str):
                        parts.append(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                                parts.append(sub["text"])
        elif msg and isinstance(msg.get("content"), str):
            parts.append(msg["content"])

        return MessageInfo(
            role=role,
            timestamp=timestamp,
            model=model,
            has_tool_use=has_tool_use,
            has_thinking=has_thinking,
            fts_content="\n".join(parts),
            flags=flags,
            usage=usage,
        )
