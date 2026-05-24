"""Render a deposited transcript as a self-contained HTML viewer.

Used by ``scripts/paper_bundle.py`` to produce a ``view.html`` next to each
session's ``transcript.jsonl`` + ``manifest.json``, plus an ``index.html`` at
the bundle root. The intent is reviewer-friendly inspection of exactly what
will be uploaded to Zenodo before the deposit goes public.

Design constraints (in priority order):

- **Forensic completeness.** The renderer must not silently drop any record.
  Records the per-source normalizer doesn't recognize fall through to a
  ``<details>`` block containing the verbatim JSON.
- **Self-contained.** No JS framework, no CDN, no external font/CSS load.
  Open the file by double-click on any machine, no network, no installer.
- **Safe to open.** All deposited text is HTML-escaped. No ``<script>`` is
  emitted anywhere in the output, and a strict CSP meta-tag forbids one.
  Tool results may contain arbitrary bytes; we treat them as data, not code.
- **Print-friendly.** ``<details>`` opens by default on print; long blocks
  use ``white-space: pre-wrap`` so paper review still works.
- **Stdlib only.**
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def _escape(s: Any) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    return html.escape(s, quote=False)


def _pretty_json(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError):
        return repr(obj)


def _fmt_ts(ts: Any) -> str:
    if not ts:
        return ""
    s = str(ts)
    return s.replace("T", " ").replace("Z", "")


# ----- Per-source normalizers ----------------------------------------------
#
# Each yields zero-or-more "blocks" per JSONL line. A block is a dict with:
#   kind:       one of user_message | assistant_message | thinking |
#               tool_call | tool_result | system | meta | unknown
#   timestamp:  str | None
#   role:       "user" | "assistant" | "system" | "tool" | None
#   text:       str | None  (rendered as pre-wrap)
#   tool_name:  str | None
#   tool_input: any | None  (rendered as JSON)
#   tool_output: str | None (rendered as pre-wrap)
#   model:      str | None
#   raw:        original JSONL record (always present)


_CLAUDE_CODE_META_TYPES = {
    "file-history-snapshot": "file-history snapshot",
    "queue-operation": "queue operation",
    "progress": "progress event",
    "custom-title": "custom title",
    "ai-title": "AI-generated title",
    "agent-name": "agent name",
    "last-prompt": "last-prompt marker",
    "compact-summary": "compact summary",
    "permission-mode": "permission mode change",
}


def _normalize_claude_code(record: dict) -> Iterator[dict]:
    ts = record.get("timestamp")
    rtype = record.get("type")

    # Operational record types Claude Code emits alongside conversational
    # records — known shapes, not "unknown." Surface as meta so they stay
    # visible (and the raw JSON toggle still shows the full record) without
    # being styled like a parser failure.
    if rtype in _CLAUDE_CODE_META_TYPES:
        label = _CLAUDE_CODE_META_TYPES[rtype]
        sub = record.get("subtype")
        if sub:
            label = f"{label}: {sub}"
        yield {"kind": "meta", "timestamp": ts, "text": f"({label})", "raw": record}
        return

    if rtype == "attachment":
        att = record.get("attachment") or {}
        att_type = att.get("type") if isinstance(att, dict) else None
        yield {"kind": "meta", "timestamp": ts,
               "text": f"(attachment: {att_type or 'unknown'})", "raw": record}
        return

    if rtype == "system" and "message" not in record:
        sub = record.get("subtype") or "system event"
        yield {"kind": "meta", "timestamp": ts, "text": f"(system: {sub})", "raw": record}
        return

    msg = record.get("message") or {}
    role = msg.get("role") or rtype
    model = msg.get("model")
    content = msg.get("content")

    if not isinstance(content, list):
        if isinstance(content, str) and content:
            # Role wins. A real user prompt arriving as a plain string
            # (Claude Code does this sometimes) should still look like a
            # user message in the UI, not a dim system block.
            if role == "user":
                kind = "user_message"
            elif role == "assistant":
                kind = "assistant_message"
            else:
                kind = "system"
            yield {"kind": kind, "timestamp": ts, "role": role, "text": content,
                   "model": model, "raw": record}
            return
        yield {"kind": "unknown", "timestamp": ts, "role": role, "model": model, "raw": record}
        return

    if not content:
        yield {"kind": "meta", "timestamp": ts, "role": role, "model": model,
               "text": "(empty message)", "raw": record}
        return

    for block in content:
        if not isinstance(block, dict):
            yield {"kind": "unknown", "timestamp": ts, "role": role, "model": model, "raw": block}
            continue
        bt = block.get("type")
        if bt == "text":
            yield {"kind": "user_message" if role == "user" else "assistant_message",
                   "timestamp": ts, "role": role, "text": block.get("text"),
                   "model": model, "raw": block}
        elif bt == "thinking":
            yield {"kind": "thinking", "timestamp": ts, "role": "assistant",
                   "text": block.get("thinking") or block.get("text"),
                   "model": model, "raw": block}
        elif bt == "tool_use":
            yield {"kind": "tool_call", "timestamp": ts, "role": "assistant",
                   "tool_name": block.get("name"), "tool_input": block.get("input"),
                   "model": model, "raw": block}
        elif bt == "image":
            src = block.get("source") if isinstance(block.get("source"), dict) else {}
            if src.get("type") == "base64" and src.get("data"):
                yield {"kind": "image", "timestamp": ts, "role": role,
                       "image_media_type": src.get("media_type") or "application/octet-stream",
                       "image_data_b64": src.get("data"),
                       "raw": block}
            else:
                yield {"kind": "image", "timestamp": ts, "role": role,
                       "image_media_type": None, "image_data_b64": None,
                       "raw": block}
        elif bt == "tool_result":
            tc = block.get("content")
            if isinstance(tc, list):
                parts = []
                for sub in tc:
                    if isinstance(sub, dict) and "text" in sub:
                        parts.append(str(sub.get("text") or ""))
                    else:
                        parts.append(_pretty_json(sub))
                output = "\n".join(parts)
            elif isinstance(tc, str):
                output = tc
            else:
                output = _pretty_json(tc)
            yield {"kind": "tool_result", "timestamp": ts, "role": "tool",
                   "tool_output": output, "tool_name": block.get("tool_use_id"),
                   "raw": block}
        else:
            yield {"kind": "unknown", "timestamp": ts, "role": role,
                   "model": model, "raw": block}


def _normalize_codex(record: dict) -> Iterator[dict]:
    ts = record.get("timestamp")
    rtype = record.get("type")
    payload = record.get("payload")

    if not isinstance(payload, dict):
        yield {"kind": "unknown", "timestamp": ts, "raw": record}
        return

    ptype = payload.get("type")

    if rtype == "session_meta":
        yield {"kind": "meta", "timestamp": ts, "text": "session start",
               "model": payload.get("model_provider"), "raw": record}
        return
    if rtype == "turn_context":
        yield {"kind": "meta", "timestamp": ts,
               "text": f"turn context — model={payload.get('model','?')} cwd={payload.get('cwd','?')}",
               "model": payload.get("model"), "raw": record}
        return
    if rtype == "event_msg":
        yield {"kind": "meta", "timestamp": ts, "text": f"event: {ptype or '?'}", "raw": record}
        return

    if rtype != "response_item":
        yield {"kind": "unknown", "timestamp": ts, "raw": record}
        return

    role = payload.get("role")
    if ptype == "message":
        content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    yield {"kind": "unknown", "timestamp": ts, "role": role, "raw": block}
                    continue
                bt = block.get("type")
                text = block.get("text")
                if bt in {"input_text", "output_text", "text"}:
                    yield {"kind": "user_message" if role == "user" else "assistant_message",
                           "timestamp": ts, "role": role, "text": text, "raw": block}
                else:
                    yield {"kind": "unknown", "timestamp": ts, "role": role, "raw": block}
        else:
            yield {"kind": "unknown", "timestamp": ts, "role": role, "raw": record}
        return

    if ptype == "reasoning":
        summary = payload.get("summary") or []
        parts = []
        if isinstance(summary, list):
            for s in summary:
                if isinstance(s, dict) and "text" in s:
                    parts.append(str(s.get("text") or ""))
        text = "\n".join(parts) if parts else _pretty_json(summary)
        yield {"kind": "thinking", "timestamp": ts, "role": "assistant",
               "text": text, "raw": record}
        return

    if ptype == "function_call":
        name = payload.get("name")
        args = payload.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                pass
        yield {"kind": "tool_call", "timestamp": ts, "role": "assistant",
               "tool_name": name, "tool_input": args, "raw": record}
        return

    if ptype == "function_call_output":
        yield {"kind": "tool_result", "timestamp": ts, "role": "tool",
               "tool_name": payload.get("call_id"),
               "tool_output": payload.get("output"), "raw": record}
        return

    yield {"kind": "unknown", "timestamp": ts, "raw": record}


def _normalize_antigravity(record: dict) -> Iterator[dict]:
    ts = record.get("created_at")
    src = record.get("source")
    rtype = record.get("type")
    content = record.get("content")
    tool_calls = record.get("tool_calls")

    if rtype == "USER_INPUT":
        yield {"kind": "user_message", "timestamp": ts, "role": "user",
               "text": content or "", "raw": record}
        return

    if rtype == "CONVERSATION_HISTORY":
        yield {"kind": "meta", "timestamp": ts,
               "text": "(conversation history checkpoint)", "raw": record}
        return

    if rtype == "PLANNER_RESPONSE":
        if isinstance(content, str) and content:
            yield {"kind": "assistant_message", "timestamp": ts, "role": "assistant",
                   "text": content, "raw": record}
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    yield {"kind": "unknown", "timestamp": ts, "raw": call}
                    continue
                yield {"kind": "tool_call", "timestamp": ts, "role": "assistant",
                       "tool_name": call.get("name"),
                       "tool_input": call.get("args"), "raw": call}
        return

    # Anything else from MODEL with content is a tool result of some kind.
    if src == "MODEL" and isinstance(content, str):
        yield {"kind": "tool_result", "timestamp": ts, "role": "tool",
               "tool_name": rtype, "tool_output": content, "raw": record}
        return

    yield {"kind": "unknown", "timestamp": ts, "raw": record}


def _normalize_record(source: str, record: dict) -> Iterator[dict]:
    if source == "claude-code":
        yield from _normalize_claude_code(record)
    elif source == "codex":
        yield from _normalize_codex(record)
    elif source == "antigravity":
        yield from _normalize_antigravity(record)
    else:
        yield {"kind": "unknown", "raw": record}


# ----- CSS / HTML scaffolding ----------------------------------------------

_CSS = """\
:root {
  color-scheme: light dark;
  --fg: #1a1a1a;
  --bg: #fbfbf9;
  --muted: #6a6a6a;
  --border: #d8d6d2;
  --code-bg: #f1efe9;
  --user-bg: #eef3f9;
  --user-border: #b5c8e0;
  --assistant-bg: #fdfdfb;
  --assistant-border: #cfcbc1;
  --thinking-bg: #f7f3ee;
  --thinking-border: #d6cdb9;
  --tool-bg: #f4f0e4;
  --tool-border: #c9c0a4;
  --tool-result-bg: #ebe9e1;
  --tool-result-border: #bdb9a8;
  --meta-bg: #f6f6f3;
  --meta-border: #d8d6cf;
  --unknown-bg: #fbeded;
  --unknown-border: #d4a0a0;
  --accent: #2c4a7a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --fg: #ececec;
    --bg: #16161a;
    --muted: #9b9b9b;
    --border: #2a2a30;
    --code-bg: #1d1d22;
    --user-bg: #1b2436;
    --user-border: #3a4d6f;
    --assistant-bg: #1a1a1f;
    --assistant-border: #2e2e36;
    --thinking-bg: #221f1a;
    --thinking-border: #45402f;
    --tool-bg: #221f12;
    --tool-border: #4d4622;
    --tool-result-bg: #1d1c14;
    --tool-result-border: #3e3a26;
    --meta-bg: #18181c;
    --meta-border: #2a2a30;
    --unknown-bg: #2a1414;
    --unknown-border: #5f2828;
    --accent: #8aa9e0;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.5rem 1rem 4rem;
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  color: var(--fg);
  background: var(--bg);
}
main { max-width: 880px; margin: 0 auto; }
header.session {
  border-bottom: 1px solid var(--border);
  padding-bottom: 1rem;
  margin-bottom: 1.25rem;
}
header.session h1 {
  font-size: 1.15rem;
  margin: 0 0 .3rem 0;
  font-weight: 600;
}
header.session .meta {
  color: var(--muted);
  font-size: .85rem;
  display: grid;
  grid-template-columns: max-content 1fr;
  column-gap: 1rem;
  row-gap: .15rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}
.banner {
  background: #fff4cf;
  border: 1px solid #d6c47a;
  padding: .55rem .75rem;
  font-size: .85rem;
  margin: 0 0 1rem 0;
  border-radius: 4px;
}
.block {
  border: 1px solid var(--border);
  padding: .55rem .75rem;
  margin: .55rem 0;
  border-radius: 4px;
  background: var(--bg);
}
.block .label {
  font-size: .72rem;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--muted);
  margin-bottom: .3rem;
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  font-weight: 600;
}
.block .label .ts { font-weight: 400; opacity: .8; }
.block.user { background: var(--user-bg); border-color: var(--user-border); }
.block.assistant { background: var(--assistant-bg); border-color: var(--assistant-border); }
.block.thinking { background: var(--thinking-bg); border-color: var(--thinking-border); font-style: italic; }
.block.tool-call { background: var(--tool-bg); border-color: var(--tool-border); }
.block.tool-result { background: var(--tool-result-bg); border-color: var(--tool-result-border); }
.block.meta { background: var(--meta-bg); border-color: var(--meta-border); color: var(--muted); font-size: .85rem; }
.block.unknown { background: var(--unknown-bg); border-color: var(--unknown-border); }
.block.image { background: var(--meta-bg); border-color: var(--meta-border); }
.block.image img {
  max-width: 100%;
  height: auto;
  display: block;
  margin-top: .35rem;
  border: 1px solid var(--border);
}
.block .text {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  font-size: .92rem;
}
.block pre, .block code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .82rem;
}
.block pre {
  background: var(--code-bg);
  padding: .55rem .75rem;
  margin: .35rem 0 0;
  border-radius: 3px;
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}
.block details { margin-top: .35rem; }
.block details summary {
  cursor: pointer;
  color: var(--muted);
  font-size: .8rem;
}
.block details[open] summary { margin-bottom: .25rem; }
.block .tool-meta {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .82rem;
  color: var(--muted);
  margin-bottom: .25rem;
}
footer {
  margin-top: 2rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: .8rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
footer a { color: var(--muted); }

/* Index page */
.session-card {
  display: block;
  border: 1px solid var(--border);
  padding: .85rem 1rem;
  margin: .5rem 0;
  border-radius: 4px;
  background: var(--bg);
  text-decoration: none;
  color: var(--fg);
}
.session-card:hover { border-color: var(--user-border); }
.session-card h2 {
  margin: 0 0 .3rem 0;
  font-size: .95rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}
.session-card .info {
  color: var(--muted);
  font-size: .82rem;
}
.session-card .excerpt {
  color: var(--fg);
  font-size: .88rem;
  margin: .25rem 0;
  font-style: italic;
  opacity: .85;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.session-card .cardmeta {
  color: var(--muted);
  font-size: .72rem;
  margin-top: .2rem;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  opacity: .8;
}
.session-card.subagent {
  margin-left: 1.5rem;
  background: var(--meta-bg);
}
details.subagents {
  margin: -.25rem 0 .35rem 0;
}
details.subagents > summary {
  cursor: pointer;
  color: var(--muted);
  font-size: .8rem;
  margin: .25rem 0 .35rem 1.5rem;
}
details.leading-meta {
  margin: .5rem 0;
}
details.leading-meta > summary {
  cursor: pointer;
  color: var(--muted);
  font-size: .85rem;
  padding: .35rem .6rem;
  border: 1px dashed var(--border);
  border-radius: 4px;
  background: var(--meta-bg);
}
form.search {
  display: flex;
  gap: .5rem;
  margin: 0 0 1rem 0;
}
form.search input[type=search] {
  flex: 1;
  padding: .5rem .7rem;
  font: inherit;
  font-size: .95rem;
  background: var(--bg);
  color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 4px;
}
form.search input[type=search]:focus {
  outline: 2px solid var(--accent);
  outline-offset: -1px;
}
form.search button {
  padding: .5rem 1rem;
  font: inherit;
  font-size: .9rem;
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}
.search-result {
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: .55rem .75rem;
  margin: .35rem 0;
  background: var(--bg);
  font-size: .9rem;
}
.search-result a { color: var(--accent); text-decoration: none; }
.search-result a:hover { text-decoration: underline; }
.search-result .snippet { color: var(--fg); margin-top: .2rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .82rem; white-space: pre-wrap; }
.search-result .snippet mark { background: rgba(255, 222, 113, 0.45); color: inherit; padding: 0 1px; }
.no-results { color: var(--muted); padding: 1rem 0; }

@media print {
  body { background: white; padding: 0; }
  details { display: block; }
  details > summary { display: none; }
  details > *:not(summary) { display: block !important; }
}
"""

_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "script-src 'none'; "
    "connect-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def _doc_open(title: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">\n'
        f"<title>{_escape(title)}</title>\n"
        f"<style>\n{_CSS}\n</style>\n"
        "</head>\n"
        "<body>\n<main>\n"
    )


_DOC_CLOSE = "</main>\n</body>\n</html>\n"


# ----- Block rendering -----------------------------------------------------


_LONG_OUTPUT_CHARS = 600


def _render_block(block: dict, include_raw: bool = True,
                  collapse_long_tool_results: bool = True) -> str:
    kind = block.get("kind", "unknown")
    role = block.get("role")
    ts = _fmt_ts(block.get("timestamp"))
    model = block.get("model")

    css_cls = {
        "user_message": "user",
        "assistant_message": "assistant",
        "thinking": "thinking",
        "tool_call": "tool-call",
        "tool_result": "tool-result",
        "image": "image",
        "meta": "meta",
        "system": "meta",
        "unknown": "unknown",
    }.get(kind, "unknown")
    label = {
        "user_message": "user",
        "assistant_message": "assistant",
        "thinking": "thinking",
        "tool_call": "tool call",
        "tool_result": "tool result",
        "image": "image attachment",
        "meta": "meta",
        "system": "system",
        "unknown": "unrecognized record",
    }.get(kind, "unknown")
    if model and kind == "assistant_message":
        label = f"assistant • {model}"

    label_html = (
        f'<div class="label"><span>{_escape(label)}</span>'
        f'<span class="ts">{_escape(ts)}</span></div>'
    )

    parts: list[str] = [f'<div class="block {css_cls}">', label_html]

    if kind in {"user_message", "assistant_message", "thinking", "system", "meta"}:
        text = block.get("text") or ""
        if text:
            parts.append(f'<p class="text">{_escape(text)}</p>')
    elif kind == "tool_call":
        parts.append(
            f'<div class="tool-meta">name: {_escape(block.get("tool_name"))}</div>'
        )
        tin = block.get("tool_input")
        if tin is not None:
            if isinstance(tin, str):
                parts.append(f'<pre>{_escape(tin)}</pre>')
            else:
                parts.append(f'<pre>{_escape(_pretty_json(tin))}</pre>')
    elif kind == "tool_result":
        tn = block.get("tool_name")
        if tn:
            parts.append(f'<div class="tool-meta">for: {_escape(tn)}</div>')
        out = block.get("tool_output") or ""
        if out:
            if collapse_long_tool_results and len(out) > _LONG_OUTPUT_CHARS:
                preview = out[:_LONG_OUTPUT_CHARS].rstrip()
                parts.append(
                    f'<pre>{_escape(preview)}…</pre>'
                    f'<details><summary>show all ({len(out):,} chars)</summary>'
                    f'<pre>{_escape(out)}</pre></details>'
                )
            else:
                parts.append(f'<pre>{_escape(out)}</pre>')
    elif kind == "image":
        mt = block.get("image_media_type")
        data = block.get("image_data_b64")
        if mt and data:
            kb = (len(data) * 3 // 4) // 1024
            parts.append(
                f'<div class="tool-meta">{_escape(mt)} • ~{kb} KB (decoded)</div>'
            )
            parts.append(
                f'<img alt="image attachment ({_escape(mt)})" '
                f'src="data:{_escape(mt)};base64,{_escape(data)}">'
            )
        else:
            parts.append('<p class="text">(image attachment — source not base64-decodable)</p>')
    elif kind == "unknown":
        msg = ("(record shape not recognized — see raw JSON below)"
               if include_raw else
               "(record shape not recognized)")
        parts.append(f'<p class="text">{msg}</p>')

    if include_raw:
        raw = block.get("raw")
        if raw is not None:
            parts.append(
                "<details><summary>raw JSON</summary>"
                f'<pre>{_escape(_pretty_json(raw))}</pre></details>'
            )

    parts.append("</div>")
    return "".join(parts)


# ----- Public renderers ----------------------------------------------------


def _read_transcript_records(transcript_path: Path) -> Iterator[dict]:
    with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, TypeError):
                yield {"_holotype_unparseable": line}


def render_session(manifest: dict, transcript_path: Path, out_path: Path,
                   include_raw: bool = True,
                   collapse_long_tool_results: bool = True,
                   collapse_leading_meta: bool = True) -> None:
    """Write ``view.html`` rendering ``transcript_path`` for one session.

    ``include_raw`` — embed each record's raw JSON in a ``<details>`` toggle.
    Default True for paper bundles (reviewers verify rendering faithfulness).
    Browse mode passes False (canonical JSONL sits next to the HTML).

    ``collapse_long_tool_results`` — wrap tool results > _LONG_OUTPUT_CHARS
    in a closed ``<details>`` so long shell output doesn't dominate scroll.

    ``collapse_leading_meta`` — collect the run of meta/system records at
    the top of the transcript (file-history snapshots, queue ops, attachment
    deltas, system reminders) into a single closed ``<details>``. Real
    conversation usually starts 5–15 records deep; this hides operational
    noise without dropping it.
    """
    source = manifest.get("source") or "unknown"
    session_id = manifest.get("session_id") or "?"
    sha = manifest.get("sha256") or "?"
    models = [m for m in (manifest.get("models") or []) if m != "<synthetic>"]
    deposited = manifest.get("deposited_at") or "?"
    first_ts = manifest.get("first_timestamp") or "?"
    last_ts = manifest.get("last_timestamp") or "?"
    msg_count = manifest.get("message_count")
    tok_in = manifest.get("total_input_tokens")
    tok_out = manifest.get("total_output_tokens")
    wall = manifest.get("wall_clock_seconds")
    # Canonical field on manifest v4+ is project_dir_decoded; older keys
    # (project_path, project) are read as fallbacks so historical manifests
    # don't show "?" if they ever existed.
    project = (manifest.get("project_dir_decoded") or manifest.get("project_path")
               or manifest.get("project") or "")
    gs = manifest.get("project_git_state") or {}

    project_basename = _project_basename(project) if project else ""
    short_id = session_id[:8] if len(session_id) >= 8 else session_id
    if project_basename:
        page_title = f"holotype: {project_basename} · {short_id}"
        heading = f"{project_basename}"
        sub_heading = f'<div class="cardmeta">id {_escape(session_id)}</div>'
    else:
        page_title = f"holotype: {session_id}"
        heading = session_id
        sub_heading = ""
    parts = [_doc_open(page_title)]
    parts.append('<header class="session">')
    parts.append(f"<h1>{_escape(heading)}</h1>")
    if sub_heading:
        parts.append(sub_heading)
    meta_rows = [
        ("source", source),
        ("models", ", ".join(models) if models else "?"),
        ("sha256", sha),
        ("deposited", deposited),
        ("first turn", first_ts),
        ("last turn", last_ts),
        ("messages", msg_count if msg_count is not None else "?"),
        ("tokens in / out", f"{tok_in} / {tok_out}" if tok_in is not None else "?"),
        ("wall clock (s)", wall if wall is not None else "?"),
        ("project", project or "?"),
    ]
    if gs:
        meta_rows.append(("project git", f'{gs.get("remote") or "?"} @ '
                          f'{gs.get("commit_short") or gs.get("commit") or "?"}'))
    parts.append('<div class="meta">')
    for k, v in meta_rows:
        parts.append(f"<span>{_escape(k)}</span><span>{_escape(v)}</span>")
    parts.append("</div></header>")

    banner_text = (
        "This page is a rendering of <code>transcript.jsonl</code>. The JSONL is "
        "the canonical artifact; this HTML is regenerated from it and is not part "
        "of the hash chain."
        + (" Each block carries a raw-JSON toggle so you can verify the rendering "
           "is faithful." if include_raw else "")
    )
    parts.append(f'<p class="banner">{banner_text}</p>')

    # Stream-classify blocks. Default-collapse the leading run of meta/system
    # records (file-history snapshots, queue ops, attachment deltas, system
    # reminders) into one collapsed <details> so the user lands on the first
    # real conversation turn. Once any non-meta block appears the rest renders
    # inline as before.
    leading_meta: list[str] = []
    in_leading = bool(collapse_leading_meta)
    seen_any = False

    def flush_leading():
        if leading_meta:
            n = len(leading_meta)
            parts.append(
                f'<details class="leading-meta"><summary>{n} pre-conversation '
                f"system event{'s' if n != 1 else ''} (file-history, attachments, "
                "reminders) — click to expand</summary>"
                + "".join(leading_meta)
                + "</details>"
            )
            leading_meta.clear()

    for record in _read_transcript_records(transcript_path):
        if "_holotype_unparseable" in record:
            seen_any = True
            in_leading = False
            flush_leading()
            parts.append(
                '<div class="block unknown"><div class="label">'
                "<span>unparseable line</span><span></span></div>"
                f'<pre>{_escape(record["_holotype_unparseable"])}</pre></div>'
            )
            continue
        for block in _normalize_record(source, record):
            seen_any = True
            block_html = _render_block(
                block, include_raw=include_raw,
                collapse_long_tool_results=collapse_long_tool_results,
            )
            if in_leading and block.get("kind") in {"meta", "system", "unknown"}:
                leading_meta.append(block_html)
            else:
                if in_leading:
                    flush_leading()
                    in_leading = False
                parts.append(block_html)

    # If the whole transcript was meta (unusual), flush so we render something.
    flush_leading()

    if not seen_any:
        parts.append('<p class="banner">(transcript was empty)</p>')

    parts.append(
        '<footer>raw: <a href="transcript.jsonl">transcript.jsonl</a> &middot; '
        '<a href="manifest.json">manifest.json</a></footer>'
    )
    parts.append(_DOC_CLOSE)

    out_path.write_text("".join(parts), encoding="utf-8")


def render_search_page(
    query: str,
    hits: list[dict],
    out_path: Path,
    *,
    href_pattern: str = "/session/{sid}",
) -> None:
    """Render a search-results page mirroring index.html's framing."""
    title = f"holotype — search: {query}" if query else "holotype — search"
    parts = [_doc_open(title)]
    parts.append('<header class="session">')
    parts.append(f"<h1>{_escape(title)}</h1>")
    parts.append(
        '<div class="meta">'
        f"<span>matches</span><span>{len(hits)}</span>"
        f"<span>query</span><span>{_escape(query) or '(empty)'}</span>"
        "</div></header>"
    )
    parts.append(
        '<form class="search" action="/search" method="get" role="search">'
        '<input type="search" name="q" placeholder="search transcripts (FTS5)" '
        f'value="{_escape(query)}" autofocus>'
        '<button type="submit">search</button>'
        '</form>'
    )
    if not hits:
        parts.append('<p class="no-results">No matches. FTS5 syntax: bare terms (porter stemming, '
                     'AND default), <code>"exact phrase"</code>, '
                     '<code>foo OR bar</code>, <code>NEAR(a b, 5)</code>.</p>')
    else:
        for h in hits:
            sid = h.get("session_id") or "?"
            ts = (h.get("first_ts") or "").split("T")[0]
            project = _project_basename(h.get("project_dir")) or h.get("project_dir") or ""
            role = h.get("role") or "?"
            snippet = h.get("snippet") or ""
            # FTS5 snippet() markers « » → <mark>. We escape the body first
            # then unescape just the markers we control.
            esc = _escape(snippet).replace("«", "<mark>").replace("»", "</mark>")
            href = href_pattern.format(sid=sid)
            head = f"<strong>{_escape(project)}</strong> · {_escape(ts)} · {_escape(role)}" if project else f"{_escape(ts)} · {_escape(role)}"
            parts.append(
                f'<div class="search-result">'
                f'<a href="{_escape(href)}">{head} → open</a>'
                f'<div class="snippet">{esc}</div>'
                "</div>"
            )
    parts.append(_DOC_CLOSE)
    out_path.write_text("".join(parts), encoding="utf-8")


def _human_date(iso: str | None) -> str:
    """ISO-8601 -> 'May 22, 2026 · 11:49' (UTC). Empty string on failure."""
    if not iso:
        return ""
    try:
        from datetime import datetime
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%b %-d, %Y · %H:%M")
    except (ValueError, TypeError):
        return str(iso)


def _project_basename(project_dir: str | None) -> str:
    """Last path segment, handling both POSIX paths and Claude Code's
    dash-encoded form (``-Users-bf-Git-foo`` ↔ ``/Users/bf/Git/foo``)."""
    if not project_dir:
        return ""
    s = project_dir.strip()
    if "/" in s:
        return s.rstrip("/").split("/")[-1] or s
    if s.startswith("-"):
        # Dash-encoded; the last segment is the trailing dash-separated chunk.
        parts = [p for p in s.split("-") if p]
        if parts:
            return parts[-1]
    return s


def _card_info_line(sess: dict) -> str:
    """One info line per session card: human date • messages • badges • output tokens."""
    parts: list[str] = []
    first = _human_date(sess.get("first_timestamp"))
    if first:
        parts.append(first)
    msgs = sess.get("message_count")
    if msgs is not None:
        parts.append(f"{msgs} msg")
    badges: list[str] = []
    if sess.get("has_tool_use"):
        badges.append("tools")
    if sess.get("has_thinking"):
        badges.append("thinking")
    if badges:
        parts.append(" + ".join(badges))
    tok_out = sess.get("total_output_tokens")
    if isinstance(tok_out, int) and tok_out > 0:
        if tok_out >= 1000:
            parts.append(f"{tok_out//1000}k out")
        else:
            parts.append(f"{tok_out} out")
    models = [m for m in (sess.get("models") or []) if m and m != "<synthetic>"]
    if models:
        parts.append(", ".join(models))
    return " • ".join(parts)


_DEFAULT_BUNDLE_BANNER = (
    "Open any session below to read the rendered transcript. Each session "
    "directory contains the canonical <code>transcript.jsonl</code>, the "
    "<code>manifest.json</code>, and a <code>view.html</code> regenerated from the JSONL."
)


def render_index(
    bundle_dir: Path,
    bundle_manifest: dict,
    out_path: Path,
    *,
    title: str = "holotype paper bundle",
    banner: str | None = _DEFAULT_BUNDLE_BANNER,
    href_pattern: str = "{sid}/view.html",
    show_search: bool = False,
    search_query: str = "",
    children_by_parent: dict[str, list[dict]] | None = None,
) -> None:
    """Write ``index.html`` listing all sessions.

    ``title`` and ``banner`` let the browse-server path use distinct prose
    from the paper-bundle path. ``href_pattern`` is a format string for
    each card's link; the browse server passes ``"/session/{sid}"`` to
    route through its handler. ``show_search`` adds a top-of-page search
    form. ``children_by_parent`` groups subagent sessions under their
    parent's card; pass None to render flat.
    """
    sessions = bundle_manifest.get("sessions") or []
    paper_title = bundle_manifest.get("paper_title")
    paper_doi = bundle_manifest.get("paper_doi")
    produced_at = bundle_manifest.get("produced_at") or ""
    archive_commit = bundle_manifest.get("archive_commit") or ""
    children_by_parent = children_by_parent or {}

    parts = [_doc_open(title)]
    parts.append('<header class="session">')
    parts.append(f"<h1>{_escape(title)}</h1>")
    meta_rows: list[tuple[str, Any]] = [
        ("produced", _human_date(produced_at) or produced_at),
        ("sessions", len(sessions)),
    ]
    if archive_commit:
        meta_rows.append(("archive HEAD", archive_commit))
    if paper_title:
        meta_rows.insert(0, ("paper", paper_title))
    if paper_doi:
        meta_rows.insert(1 if paper_title else 0, ("doi", paper_doi))
    parts.append('<div class="meta">')
    for k, v in meta_rows:
        parts.append(f"<span>{_escape(k)}</span><span>{_escape(v)}</span>")
    parts.append("</div></header>")

    if show_search:
        parts.append(
            '<form class="search" action="/search" method="get" role="search">'
            '<input type="search" name="q" placeholder="search transcripts (FTS5)" '
            f'value="{_escape(search_query)}" autofocus>'
            '<button type="submit">search</button>'
            "</form>"
        )

    if banner:
        parts.append(f'<p class="banner">{banner}</p>')

    for sess in sessions:
        sid = sess.get("session_id") or "?"
        project = _project_basename(sess.get("project_dir_decoded"))
        heading = project or sid
        sid_short = sid[:8] if len(sid) >= 8 else sid
        sub_count = len(children_by_parent.get(sid, []))
        info = _card_info_line(sess)
        href = href_pattern.format(sid=sid)
        gs = sess.get("project_git_state") or {}
        repo = gs.get("remote") or ""
        bottom: list[str] = []
        bottom.append(f"id {sid_short}")
        if repo:
            bottom.append(_escape(repo))
        if sub_count:
            bottom.append(f"+{sub_count} subagent{'s' if sub_count != 1 else ''}")

        excerpt = sess.get("first_user_message_excerpt") or ""
        excerpt_html = (
            f'<div class="excerpt">{_escape(excerpt)}</div>' if excerpt else ""
        )
        parts.append(
            f'<a class="session-card" href="{_escape(href)}">'
            f"<h2>{_escape(heading)}</h2>"
            f'{excerpt_html}'
            f'<div class="info">{_escape(info)}</div>'
            f'<div class="cardmeta">{" · ".join(bottom)}</div>'
            "</a>"
        )

        if sub_count:
            parts.append('<details class="subagents"><summary>'
                         f"subagents ({sub_count})</summary>")
            for child in children_by_parent.get(sid, []):
                csid = child.get("session_id") or "?"
                cinfo = _card_info_line(child)
                chref = href_pattern.format(sid=csid)
                parts.append(
                    f'<a class="session-card subagent" href="{_escape(chref)}">'
                    f"<h2>{_escape(csid)}</h2>"
                    f'<div class="info">{_escape(cinfo)}</div>'
                    "</a>"
                )
            parts.append("</details>")

    parts.append(
        '<footer>structured manifest: <a href="BUNDLE_MANIFEST.json">BUNDLE_MANIFEST.json</a> '
        '&middot; <a href="VERIFY.md">VERIFY.md</a></footer>'
    )
    parts.append(_DOC_CLOSE)
    out_path.write_text("".join(parts), encoding="utf-8")
