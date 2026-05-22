"""Helpers for walking the on-disk archive structure.

The archive layout is owned by each Source — Claude Code uses
`sessions/<project>/<session>/` with subagents at
`sessions/<project>/<parent>/subagents/<sub>/`; Codex uses
`sessions/codex/<YYYY>/<MM>/<DD>/<session>/`. The read-side scripts
(cite, verify, context, reindex) shouldn't care about per-Source
layout — they just walk anything under `sessions/` that has both a
`transcript.jsonl` and a `manifest.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def iter_all_sessions(archive: Path) -> Iterator[tuple[Path, dict]]:
    """Yield (session_dir, manifest_dict) for every deposit in the archive.

    Walks `archive/sessions/` to arbitrary depth, recognizing a session
    by the presence of both a transcript file (``transcript.jsonl`` or
    ``transcript.jsonl.zst``) and ``manifest.json`` in a directory.
    Order is stable (sorted by directory path).
    """
    sessions_root = archive / "sessions"
    if not sessions_root.exists():
        return

    for manifest_path in sorted(sessions_root.rglob("manifest.json")):
        sess_dir = manifest_path.parent
        has_transcript = (
            (sess_dir / "transcript.jsonl").exists()
            or (sess_dir / "transcript.jsonl.zst").exists()
        )
        if not has_transcript:
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        yield sess_dir, manifest


def resolve_session_by_prefix(archive: Path, prefix: str) -> Path | None:
    """Find the unique session whose id starts with `prefix`.

    Returns the session directory or None. Writes ambiguity diagnostics
    to stderr — callers should check `None` and exit non-zero.
    """
    import sys
    matches: list[Path] = []
    for sess_dir, _ in iter_all_sessions(archive):
        if sess_dir.name.startswith(prefix):
            matches.append(sess_dir)
    if not matches:
        return None
    if len(matches) > 1:
        sys.stderr.write(f"ambiguous prefix '{prefix}', matches:\n")
        for m in matches:
            sys.stderr.write(f"  {m.name}\n")
        return None
    return matches[0]
