"""OpenAI Codex CLI source.

Filesystem layout:
    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO-TS>-<UUID>.jsonl

Each file is the full transcript of one Codex session. There is no
sub-conversation file analog of Claude Code's `subagents/` directory.

Real-world Codex schema (observed 2026-04 on Brenden's machine):

Every line is wrapped in an envelope:
    {"timestamp": "ISO-8601",
     "type": "session_meta" | "event_msg" | "response_item" | "turn_context",
     "payload": {...}}

Outer types and their payload shapes:

  session_meta (line 0 — session header):
    payload = {id, timestamp, cwd, originator, cli_version, source,
               model_provider, base_instructions, git: {commit_hash,
               branch, repository_url}}

  turn_context:
    payload = {turn_id, cwd, current_date, timezone, approval_policy,
               sandbox_policy, permission_profile, model}
    The `model` field is per-turn; aggregated into the manifest.

  response_item — the actual transcript content. payload.type is the
  inner record type:
    - message: {role, content: [{type: input_text|output_text|text, text}]}
    - reasoning: {summary: [{type: summary_text, text}]}
    - function_call: {name, arguments, call_id}
    - function_call_output: {call_id, output}

  event_msg — control/telemetry events. payload.type subtypes:
    - task_started: {turn_id, started_at, ...}
    - token_count: {info: {total_token_usage: {input_tokens,
                    cached_input_tokens, output_tokens,
                    reasoning_output_tokens, total_tokens},
                    last_token_usage: {...}, model_context_window},
                    rate_limits: {...}}
    - user_message / agent_message: convenience duplicates of the
      response_item message text
    - exec_command_end: shell tool result telemetry

LEGACY flat schema (synthetic-codex-rollout.jsonl fixture only, was
never observed in real Codex data — kept supported for backwards
compat with any older fixtures):
    {"type": "message"|"reasoning"|"function_call"|..., ...}

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

        # Legacy "filler" marker from the original fixture format.
        if obj.get("record_type") == "state":
            return None

        outer_type = obj.get("type")
        outer_ts = obj.get("timestamp")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else None

        # Wrapped format (real Codex): {timestamp, type, payload}
        if payload is not None and outer_type in (
            "session_meta", "event_msg", "response_item", "turn_context"
        ):
            return CodexSource._parse_wrapped(outer_type, outer_ts, payload, obj)

        # Legacy flat fixture format — fall through to the older parser
        # so old fixtures keep working.
        return CodexSource._parse_flat(obj)

    @staticmethod
    def _parse_wrapped(
        outer_type: str,
        outer_ts: str | None,
        payload: dict,
        full_obj: dict,
    ) -> MessageInfo | None:
        inner_type = payload.get("type")

        # Session header — pull cwd, model_provider, and git into
        # session_metadata for the manifest aggregator.
        if outer_type == "session_meta":
            git_block = payload.get("git") if isinstance(payload.get("git"), dict) else None
            git_state = None
            if git_block:
                git_state = {
                    "commit": git_block.get("commit_hash"),
                    "commit_short": (git_block.get("commit_hash") or "")[:7] or None,
                    "branch": git_block.get("branch"),
                    "remote": git_block.get("repository_url"),
                    "dirty": None,  # not reported at session-start
                    "captured_at": "session-start",
                }
            return MessageInfo(
                role="session_header",
                timestamp=outer_ts or payload.get("timestamp"),
                # model_provider is "openai", not a model id — don't
                # pollute manifest.models. The real model arrives later
                # via turn_context.model.
                model=None,
                has_tool_use=False,
                has_thinking=False,
                fts_content="",
                flags={"header"},
                session_metadata={
                    "cwd": payload.get("cwd"),
                    "cli_version": payload.get("cli_version"),
                    "originator": payload.get("originator"),
                    "model_provider": payload.get("model_provider"),
                    "git_state": git_state,
                },
            )

        # turn_context carries the per-turn model identifier. Treat it
        # like a tiny header — counted as 0 messages but model picked
        # up by the aggregator.
        if outer_type == "turn_context":
            return MessageInfo(
                role="turn_context",
                timestamp=outer_ts,
                model=payload.get("model"),
                has_tool_use=False,
                has_thinking=False,
                fts_content="",
                flags={"header"},
            )

        # event_msg → telemetry. Token counts get pulled out specifically;
        # other events are skipped (transcript bytes still preserve them).
        if outer_type == "event_msg":
            if inner_type == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else None
                if not total:
                    return None
                # token_count events are filler (not messages), but they
                # carry cumulative usage we want to capture.
                return MessageInfo(
                    role="telemetry",
                    timestamp=outer_ts,
                    model=None,
                    has_tool_use=False,
                    has_thinking=False,
                    fts_content="",
                    flags={"header"},  # don't count as a message
                    usage=total,
                    token_count_kind="cumulative",
                )
            # Other event_msg subtypes — surface the text payload to FTS
            # but don't count them as messages (avoids double-counting
            # user/agent message bodies that already appear as
            # response_item.message).
            return None

        # response_item — the actual transcript content.
        if outer_type == "response_item":
            return CodexSource._parse_response_item(inner_type, outer_ts, payload)

        return None

    @staticmethod
    def _parse_response_item(
        inner_type: str | None,
        timestamp: str | None,
        payload: dict,
    ) -> MessageInfo | None:
        parts: list[str] = []
        has_tool_use = False
        has_thinking = False
        role = payload.get("role")

        if inner_type == "message":
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    t = item.get("type")
                    if t in ("input_text", "output_text", "text"):
                        parts.append(item.get("text") or "")
            elif isinstance(content, str):
                parts.append(content)

        elif inner_type == "reasoning":
            has_thinking = True
            summary = payload.get("summary")
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, dict) and isinstance(s.get("text"), str):
                        parts.append(s["text"])
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(item["text"])

        elif inner_type in _TOOL_TYPES or (inner_type or "").endswith("_call") or (inner_type or "").endswith("_call_output"):
            has_tool_use = True
            parts.append(f"[{inner_type}:{payload.get('name','?')}]")
            args = payload.get("arguments")
            if isinstance(args, str):
                parts.append(args)
            elif isinstance(args, dict):
                parts.append(json.dumps(args, ensure_ascii=False))
            out = payload.get("output")
            if isinstance(out, str):
                parts.append(out)

        else:
            # Unknown response_item subtype — include verbatim so
            # search still finds it.
            parts.append(json.dumps(payload, ensure_ascii=False))

        return MessageInfo(
            role=role or inner_type,
            timestamp=timestamp,
            model=None,
            has_tool_use=has_tool_use,
            has_thinking=has_thinking,
            fts_content="\n".join(p for p in parts if p),
            flags=set(),
        )

    @staticmethod
    def _parse_flat(obj: dict) -> MessageInfo | None:
        """Legacy flat-shape parser. Only used by the old synthetic fixture.

        Real Codex data always arrives in the wrapped format
        (``{timestamp, type, payload}``). Kept so we don't break tests
        that intentionally exercise legacy fixtures or any rare
        non-wrapped data we encounter in the wild.
        """
        if "id" in obj and "timestamp" in obj and "git" in obj and "type" not in obj:
            git_block = obj.get("git") if isinstance(obj.get("git"), dict) else None
            git_state = None
            if git_block:
                git_state = {
                    "commit": git_block.get("commit_hash"),
                    "commit_short": (git_block.get("commit_hash") or "")[:7] or None,
                    "branch": git_block.get("branch"),
                    "remote": git_block.get("repository_url"),
                    "dirty": None,
                    "captured_at": "session-start",
                }
            return MessageInfo(
                role="session_header",
                timestamp=obj.get("timestamp"),
                model=None,
                has_tool_use=False,
                has_thinking=False,
                fts_content="",
                flags={"header"},
                session_metadata={"git_state": git_state} if git_state else None,
            )

        rtype = obj.get("type")
        role = obj.get("role")
        timestamp = obj.get("timestamp")
        model = obj.get("model")
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
            parts.append(json.dumps(obj, ensure_ascii=False))

        return MessageInfo(
            role=role or rtype,
            timestamp=timestamp,
            model=model,
            has_tool_use=has_tool_use,
            has_thinking=has_thinking,
            fts_content="\n".join(p for p in parts if p),
            flags=set(),
        )
