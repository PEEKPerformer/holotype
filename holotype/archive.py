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
import subprocess
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


# git runs `git maintenance run --auto --detach` after every commit. Once
# the loose-object count crosses gc.auto, each commit starts its own
# detached full repack (`pack-objects --all`), and nothing stops a second
# one from starting while the first is still running. An archive's pack
# is mostly incompressible blobs (zstd or git-crypt), so each repack holds
# gigabytes and runs for a long time. One ingest tick makes one commit
# per changed session, which is enough to stack repacks until the machine
# runs out of memory. Holotype turns automatic maintenance off in the
# archive; repack by hand with `git repack -d` when loose objects pile up.
AUTO_MAINTENANCE_OFF = (
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
)


def disable_auto_maintenance(archive: Path) -> None:
    """Turn off git's automatic background maintenance for ``archive``.

    Writes to the archive's local ``.git/config`` only, and only when a
    value differs, so calling this every ingest tick is cheap. Failures
    are ignored: a missing setting costs memory, not correctness.
    """
    for key, value in AUTO_MAINTENANCE_OFF:
        try:
            current = subprocess.run(
                ["git", "-C", str(archive), "config", "--local", "--get", key],
                capture_output=True, text=True, timeout=10,
            )
            if current.stdout.strip() == value:
                continue
            subprocess.run(
                ["git", "-C", str(archive), "config", "--local", key, value],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            pass
