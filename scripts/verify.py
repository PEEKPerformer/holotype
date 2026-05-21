#!/usr/bin/env python3
"""Verify the hash chain of the holotype archive.

Walks every session manifest, recomputes the SHA-256 of the corresponding
transcript.jsonl, and compares it to the recorded hash. Any mismatch is
reported as TAMPER.

Read-only. Never modifies the archive or the index. Exit codes:
  0 = all sessions pass
  1 = at least one session mismatched
  2 = config / setup error

Usage:
    python scripts/verify.py                    # verify all sessions
    python scripts/verify.py 3f1c4cf7           # verify one session
    python scripts/verify.py --json             # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.hashing import sha256_file


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


def iter_sessions(archive: Path, prefix: str | None):
    """Yield (session_id, transcript_path, manifest_path) for each session."""
    sessions_root = archive / "sessions"
    if not sessions_root.exists():
        return
    for proj in sorted(sessions_root.iterdir()):
        if not proj.is_dir():
            continue
        for sess in sorted(proj.iterdir()):
            if not sess.is_dir():
                continue
            if prefix and not sess.name.startswith(prefix):
                continue
            t = sess / "transcript.jsonl"
            m = sess / "manifest.json"
            if t.exists() and m.exists():
                yield sess.name, t, m


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify the hash chain of the holotype archive.")
    p.add_argument("session_id", nargs="?", default=None,
                   help="Optional UUID prefix to verify a single session.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        return 2

    results: list[dict] = []
    n_pass = 0
    n_fail = 0
    n_missing_hash = 0

    for sid, transcript, manifest_path in iter_sessions(archive, args.session_id):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            results.append({"session_id": sid, "status": "ERROR", "detail": f"manifest unreadable: {e}"})
            n_fail += 1
            continue

        recorded = manifest.get("sha256")
        if not recorded:
            results.append({"session_id": sid, "status": "MISSING_HASH",
                            "detail": "manifest has no sha256 field"})
            n_missing_hash += 1
            continue

        recomputed = sha256_file(transcript)
        if recomputed == recorded:
            results.append({"session_id": sid, "status": "OK", "sha256": recomputed})
            n_pass += 1
        else:
            results.append({"session_id": sid, "status": "TAMPER",
                            "recorded": recorded, "recomputed": recomputed})
            n_fail += 1

    if args.json:
        print(json.dumps({"results": results,
                          "summary": {"pass": n_pass, "fail": n_fail,
                                      "missing_hash": n_missing_hash}}, indent=2))
    else:
        for r in results:
            status = r["status"]
            sid = r["session_id"][:8]
            if status == "OK":
                print(f"  OK      {sid}  {r['sha256'][:12]}…")
            elif status == "TAMPER":
                print(f"  TAMPER  {sid}")
                print(f"          recorded:   {r['recorded']}")
                print(f"          recomputed: {r['recomputed']}")
            else:
                print(f"  {status:<8s}{sid}  {r.get('detail','')}")
        print()
        total = n_pass + n_fail + n_missing_hash
        print(f"  {n_pass}/{total} sessions verified clean"
              f"{f', {n_fail} tampered' if n_fail else ''}"
              f"{f', {n_missing_hash} with missing hash field' if n_missing_hash else ''}")

    if n_fail > 0:
        return 1
    if n_pass == 0 and not args.session_id:
        sys.stderr.write("holotype: no sessions to verify\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
