#!/usr/bin/env python3
"""Print the filesystem path of a deposited session's transcript.

This is the load-context primitive: print the absolute path of the
archived transcript.jsonl so a Claude Code session can then `Read` it
and pull the past conversation into the current context. The skill
itself does not stuff bytes into the model context — it just resolves
the location.

Read-only. Prints exactly one absolute path on stdout (and the manifest
path if --with-manifest), so it is safe to chain with shell tools.

Usage:
    python scripts/context.py 3f1c4cf7
    python scripts/context.py 3f1c4cf7 --with-manifest
    python scripts/context.py 3f1c4cf7 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


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


def resolve_session(archive: Path, prefix: str) -> tuple[str, Path] | None:
    sessions_root = archive / "sessions"
    if not sessions_root.exists():
        return None
    matches: list[Path] = []
    for proj in sessions_root.iterdir():
        if not proj.is_dir():
            continue
        for sess in proj.iterdir():
            if not sess.is_dir():
                continue
            if sess.name.startswith(prefix) and (sess / "manifest.json").exists():
                matches.append(sess)
            sub_root = sess / "subagents"
            if sub_root.is_dir():
                for sub in sub_root.iterdir():
                    if sub.is_dir() and sub.name.startswith(prefix) and (sub / "manifest.json").exists():
                        matches.append(sub)
    if not matches:
        return None
    if len(matches) > 1:
        sys.stderr.write(f"ambiguous prefix '{prefix}', matches:\n")
        for m in matches:
            sys.stderr.write(f"  {m.name}\n")
        return None
    return (matches[0].name, matches[0])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Print the archive path of a session's transcript.")
    p.add_argument("session_id", help="UUID or prefix.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--with-manifest", action="store_true",
                   help="Also print the manifest path on a second line.")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    resolved = resolve_session(archive, args.session_id)
    if not resolved:
        sys.stderr.write(f"holotype: no session matching '{args.session_id}' in {archive}\n")
        return 1
    session_id, session_dir = resolved
    transcript = session_dir / "transcript.jsonl"
    manifest = session_dir / "manifest.json"

    if args.json:
        print(json.dumps({
            "session_id": session_id,
            "transcript": str(transcript),
            "manifest": str(manifest),
        }, indent=2))
        return 0

    print(str(transcript))
    if args.with_manifest:
        print(str(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
