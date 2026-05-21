#!/usr/bin/env python3
"""Rebuild the SQLite + FTS5 index from the canonical archive.

The index is derived data — the JSONLs and manifests in git are the
source of truth. This script drops the existing index file (if any)
and rebuilds it from scratch by walking every deposited session.

Run this when:
  - The index file was deleted or corrupted
  - The index schema version has changed
  - You want to be sure the index is in sync with the archive
  - Search results look suspiciously stale

Read-only with respect to the archive itself — only the SQLite file at
<archive>/.holotype/index.sqlite is rewritten.

Usage:
    python scripts/reindex.py
    python scripts/reindex.py --archive ~/Documents/holotype-archive
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.index import open_index, reindex_session, upsert_session


def find_archive(explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    import os
    env = os.environ.get("HOLOTYPE_ARCHIVE")
    if env:
        return Path(env).expanduser().resolve()
    pointer = Path.home() / ".config" / "holotype" / "archive-path"
    if pointer.exists():
        return Path(pointer.read_text().strip()).expanduser().resolve()
    return (Path.home() / "Documents" / "holotype-archive").resolve()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rebuild the holotype SQLite index from the archive.")
    p.add_argument("--archive", type=Path, default=None)
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    cfg = archive / ".holotype" / "config.json"
    if not cfg.exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        return 2

    sessions_root = archive / "sessions"
    if not sessions_root.exists():
        print("holotype reindex: nothing to index (no sessions/ directory)")
        return 0

    index_path = archive / ".holotype" / "index.sqlite"
    if index_path.exists():
        index_path.unlink()
        print(f"  removed {index_path}")

    n_sessions = 0
    n_messages = 0
    with open_index(index_path) as conn:
        for proj in sorted(sessions_root.iterdir()):
            if not proj.is_dir():
                continue
            project_dir_encoded = proj.name
            for sess in sorted(proj.iterdir()):
                if not sess.is_dir():
                    continue
                transcript = sess / "transcript.jsonl"
                manifest_path = sess / "manifest.json"
                if not (transcript.exists() and manifest_path.exists()):
                    continue

                manifest = json.loads(manifest_path.read_text())
                upsert_session(
                    conn,
                    session_id=sess.name,
                    project_dir=project_dir_encoded,
                    first_ts=manifest.get("first_timestamp"),
                    last_ts=manifest.get("last_timestamp"),
                    message_count=manifest.get("message_count", 0),
                    sha256=manifest.get("sha256", ""),
                    deposited_at=manifest.get("deposited_at", ""),
                    git_commit=None,
                )
                n_messages += reindex_session(conn, sess.name, transcript)
                n_sessions += 1
        conn.commit()

    print(f"  indexed {n_sessions} sessions ({n_messages} messages)")
    print(f"  index:   {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
