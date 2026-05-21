#!/usr/bin/env python3
"""Rebuild the SQLite + FTS5 index from the canonical archive.

The index is derived data — the JSONLs and manifests in git are the
source of truth. This script drops the existing index file (if any)
and rebuilds it from scratch by walking every deposited session and
dispatching FTS extraction to the appropriate Source parser per
session's recorded `source` field.

Usage:
    python scripts/reindex.py
    python scripts/reindex.py --archive ~/Documents/holotype-archive
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.archive import iter_all_sessions
from holotype.index import open_index, reindex_session, upsert_session
from holotype.sources import ALL_SOURCES, source_by_name


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

    index_path = archive / ".holotype" / "index.sqlite"
    if index_path.exists():
        index_path.unlink()
        print(f"  removed {index_path}")

    n_sessions = 0
    n_messages = 0
    n_unknown_source = 0

    with open_index(index_path) as conn:
        for sess_dir, manifest in iter_all_sessions(archive):
            transcript = sess_dir / "transcript.jsonl"
            source_name = manifest.get("source", "claude-code")
            try:
                source_cls = source_by_name(source_name)
            except KeyError:
                n_unknown_source += 1
                source_cls = source_by_name("claude-code")

            upsert_session(
                conn,
                session_id=sess_dir.name,
                source=source_name,
                parent_session_id=manifest.get("parent_session_id"),
                project_dir=manifest.get("project_dir_encoded", ""),
                first_ts=manifest.get("first_timestamp"),
                last_ts=manifest.get("last_timestamp"),
                message_count=manifest.get("message_count", 0),
                sha256=manifest.get("sha256", ""),
                deposited_at=manifest.get("deposited_at", ""),
                git_commit=None,
            )
            n_messages += reindex_session(conn, sess_dir.name, transcript, source_cls=source_cls)
            n_sessions += 1
        conn.commit()

    print(f"  indexed {n_sessions} sessions ({n_messages} messages)")
    if n_unknown_source:
        print(f"  warning: {n_unknown_source} sessions had unknown source field "
              f"and were indexed with the claude-code parser as a fallback")
    print(f"  sources: {', '.join(s.name for s in ALL_SOURCES)}")
    print(f"  index:   {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
