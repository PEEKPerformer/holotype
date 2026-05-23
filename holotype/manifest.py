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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from holotype.compression import iter_transcript_lines, read_transcript_bytes
from holotype.hashing import sha256_bytes, sha256_file
from holotype.sources.base import Source


MANIFEST_VERSION = 5

# v5 added first/last_user_message_excerpt — see _clean_user_excerpt for
# how source-specific wrappings (Codex <environment_context>, Antigravity
# <USER_REQUEST>, Claude Code <system-reminder>) are stripped so the
# excerpt reads as a real user message in the viewer's session cards.
USER_EXCERPT_MAX_CHARS = 160


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
    # Reproducibility fields (manifest_version >= 4). All nullable — for
    # older manifests, sources without the relevant data, or when the
    # signal couldn't be captured.
    #
    # project_git_state: dict with {commit, commit_short, branch, dirty,
    #   remote, captured_at}. captured_at is "session-start" for Codex
    #   (sourced from the rollout header — accurate to the moment the
    #   LLM ran) or "deposit" (probed by ingest.py from the recorded
    #   cwd — accurate only if the repo hasn't moved past the session
    #   since). A reviewer should prefer session-start when it's there.
    # wall_clock_seconds: last_timestamp - first_timestamp in seconds.
    #   Methods-section nicety; not a substitute for actually citing
    #   per-turn timing from the transcript.
    # total_input_tokens / total_output_tokens / total_cache_*_tokens:
    #   aggregated from the per-turn usage block when the host CLI
    #   records it (Claude Code does; Codex partially; Antigravity
    #   doesn't currently expose it). None means "not recorded by this
    #   source," not "zero."
    project_git_state: dict | None = None
    wall_clock_seconds: float | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_cache_creation_tokens: int | None = None
    total_cache_read_tokens: int | None = None
    # Reader fields (manifest_version >= 5). Used by the in-browser viewer
    # to show session-identifying excerpts in cards. Cleaned of source-
    # specific wrappings (Codex <environment_context>, Antigravity
    # <USER_REQUEST>, Claude Code <system-reminder>) so they read as plain
    # user prose. The canonical text is in transcript.jsonl — this is a
    # convenience field for the index UI.
    first_user_message_excerpt: str | None = None
    last_user_message_excerpt: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


_WRAP_PATTERNS = [
    # Antigravity: <USER_REQUEST>actual prose</USER_REQUEST>; keep inner
    (re.compile(r"<USER_REQUEST>\s*(.+?)\s*</USER_REQUEST>", re.DOTALL), r"\1"),
    # Antigravity + Claude Code: framing metadata blocks → drop entirely
    (re.compile(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", re.DOTALL), ""),
    (re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.DOTALL), ""),
    (re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL), ""),
    # Codex: <environment_context>...</environment_context> auto-prepended to
    # the first user turn. Strip the whole block.
    (re.compile(r"<environment_context>.*?</environment_context>", re.DOTALL), ""),
    (re.compile(r"<user_instructions>.*?</user_instructions>", re.DOTALL), ""),
]


def _clean_user_excerpt(text: str | None) -> str | None:
    """Strip source-specific wrapping tags and collapse whitespace.

    The viewer's session cards display this. Wrapping tags are uninformative
    in a one-line preview; the canonical text is in transcript.jsonl. Returns
    None if nothing useful is left after stripping.
    """
    if not text:
        return None
    s = str(text)
    for pat, repl in _WRAP_PATTERNS:
        s = pat.sub(repl, s)
    s = " ".join(s.split())
    s = s.strip()
    if not s:
        return None
    if len(s) > USER_EXCERPT_MAX_CHARS:
        s = s[: USER_EXCERPT_MAX_CHARS - 1].rstrip() + "…"
    return s


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
        # Reproducibility aggregates (manifest v4):
        "session_metadata": None,           # from header line(s)
        "usage_cumulative": None,           # last "cumulative" reading wins
        "usage_delta_sum": {                # summed over "delta" readings
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "reasoning_output_tokens": 0,
        },
        "any_usage_seen": False,
        # First and last text we saw on a role=user message (cleaned in
        # build_manifest, before storage on the manifest dict).
        "first_user_text": None,
        "last_user_text": None,
    }

    for line in lines:
        info = source_cls.parse_line(line)
        if info is None:
            continue
        # Header-flagged lines aren't user-visible messages (don't count
        # toward message_count), but they DO carry timestamps we want to
        # propagate. Codex emits many header-flagged lines mid-session
        # (turn_context, token_count) — the last one's timestamp is a
        # better last_timestamp than just line-0's. We still preserve
        # the session_metadata from the first header that supplied it.
        if "header" in info.flags:
            if info.timestamp:
                if state["first_timestamp"] is None:
                    state["first_timestamp"] = info.timestamp
                state["last_timestamp"] = info.timestamp
            if isinstance(info.session_metadata, dict) and state["session_metadata"] is None:
                state["session_metadata"] = info.session_metadata
            if info.model:
                state["models"].add(info.model)
            if isinstance(info.usage, dict):
                state["any_usage_seen"] = True
                if info.token_count_kind == "cumulative":
                    state["usage_cumulative"] = info.usage
            continue

        # Token usage aggregation. Sources mark each usage payload as
        # "cumulative" (Codex's token_count event) or "delta" (Claude
        # Code's per-turn message.usage). We trust the last cumulative
        # reading when present; otherwise sum the deltas.
        if isinstance(info.usage, dict):
            state["any_usage_seen"] = True
            if info.token_count_kind == "cumulative":
                state["usage_cumulative"] = info.usage
            else:
                for k in state["usage_delta_sum"]:
                    v = info.usage.get(k)
                    if isinstance(v, int):
                        state["usage_delta_sum"][k] += v

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

        # Capture first/last user message text from the Source-provided
        # fts_content. Cleaning happens later (in build_manifest) so this
        # branch stays cheap on the hot scan loop.
        if info.role == "user" and isinstance(info.fts_content, str):
            txt = info.fts_content
            if txt:
                if state["first_user_text"] is None:
                    state["first_user_text"] = txt
                state["last_user_text"] = txt

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


def _resolve_token_totals(state: dict) -> dict:
    """Pick the most trustworthy view of token usage and normalize keys.

    Prefers a final cumulative reading (e.g. Codex's last token_count
    event) over summed deltas. Returns {input, output, cache_creation,
    cache_read} or all-None if nothing was recorded.
    """
    out = {
        "total_input_tokens": None,
        "total_output_tokens": None,
        "total_cache_creation_tokens": None,
        "total_cache_read_tokens": None,
    }
    if not state.get("any_usage_seen"):
        return out
    cum = state.get("usage_cumulative")
    delta = state.get("usage_delta_sum") or {}
    src = cum if isinstance(cum, dict) else delta
    def _pick(*keys):
        for k in keys:
            v = src.get(k) if isinstance(src, dict) else None
            if isinstance(v, int):
                return v
        return None
    out["total_input_tokens"] = _pick("input_tokens", "prompt_tokens")
    out["total_output_tokens"] = _pick("output_tokens", "completion_tokens")
    out["total_cache_creation_tokens"] = _pick("cache_creation_input_tokens", "cache_creation_tokens")
    out["total_cache_read_tokens"] = _pick("cache_read_input_tokens", "cached_input_tokens")
    return out


def _wall_clock_seconds(first: str | None, last: str | None) -> float | None:
    if not first or not last:
        return None
    try:
        from datetime import datetime
        # Allow "Z" suffix (Codex / Antigravity) by normalizing to +00:00.
        f = datetime.fromisoformat(first.replace("Z", "+00:00"))
        l = datetime.fromisoformat(last.replace("Z", "+00:00"))
        seconds = (l - f).total_seconds()
        return round(seconds, 3) if seconds >= 0 else None
    except (ValueError, TypeError):
        return None


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
    project_git_state: dict | None = None,
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
    token_totals = _resolve_token_totals(scan)

    # Prefer the header-derived git_state (more accurate — captured at
    # session-start) over the deposit-time probe ingest passed in. If
    # only one is present, use it. If both, header wins.
    header_meta = scan.get("session_metadata") or {}
    header_git = header_meta.get("git_state") if isinstance(header_meta, dict) else None
    resolved_git_state = header_git or project_git_state

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
        project_git_state=resolved_git_state,
        wall_clock_seconds=_wall_clock_seconds(scan["first_timestamp"], scan["last_timestamp"]),
        total_input_tokens=token_totals["total_input_tokens"],
        total_output_tokens=token_totals["total_output_tokens"],
        total_cache_creation_tokens=token_totals["total_cache_creation_tokens"],
        total_cache_read_tokens=token_totals["total_cache_read_tokens"],
        first_user_message_excerpt=_clean_user_excerpt(scan.get("first_user_text")),
        last_user_message_excerpt=_clean_user_excerpt(scan.get("last_user_text")),
    )
