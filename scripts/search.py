#!/usr/bin/env python3
"""Search the holotype archive via the FTS5-backed SQLite index.

Read-only. Does not modify the archive or the index. If the index is
missing, run scripts/reindex.py first.

Usage:
    python scripts/search.py "41 minute equilibration"
    python scripts/search.py "Mahdad PID" --limit 50
    python scripts/search.py "vdP F76" --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.index import open_index, search as fts_search


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
    p = argparse.ArgumentParser(description="Search the holotype archive.")
    p.add_argument("query", help="FTS5 query string. Quote phrases with double quotes for literal match.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of formatted text.")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    index_path = archive / ".holotype" / "index.sqlite"
    if not index_path.exists():
        sys.stderr.write(
            f"holotype: no index at {index_path}\n"
            f"  run: python scripts/reindex.py --archive {archive}\n"
        )
        return 2

    with open_index(index_path) as conn:
        hits = fts_search(conn, args.query, limit=args.limit)

    if args.json:
        print(json.dumps(hits, indent=2))
        return 0 if hits else 1

    if not hits:
        print(f"no matches for: {args.query}")
        return 1

    print(f"{len(hits)} match{'es' if len(hits) != 1 else ''} for: {args.query}\n")
    for h in hits:
        sid = h["session_id"][:8]
        ts = h.get("first_ts") or "?"
        date = ts.split("T")[0] if "T" in ts else ts
        project = h.get("project_dir") or "?"
        snippet = (h.get("snippet") or "").replace("\n", " ").strip()
        print(f"  {sid}  {date}  [{h.get('role','?'):>10s}]  {project}")
        print(f"      …{snippet}…")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
