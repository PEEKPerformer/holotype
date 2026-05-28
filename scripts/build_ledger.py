#!/usr/bin/env python3
"""Build (or rebuild) the hash-chain ledger for an existing archive.

Most archives never need this directly — ``ingest.py`` bootstraps a missing
ledger automatically on its next run. This script is the explicit,
inspectable entry point: a one-time seal for a pre-ledger archive, and a
rebuild path if a ledger is ever lost.

Order is deterministic — ``(deposited_at, session_id)`` — so the resulting
head is reproducible. The true historical deposit order already lives in
git's commit DAG; this ledger is a forward-looking tamper-evidence seal, not
a reconstruction of original event order. One link per current on-disk
session.

Idempotency: if a ledger already exists, verifies clean, AND covers every
on-disk session, this is a no-op (re-running never rewrites a healthy live
ledger, which would discard historical update links it can't reconstruct).
Pass ``--force`` to rebuild from scratch regardless.

Exit codes:
  0 = ledger is present and verifies clean (built, rebuilt, or already good)
  1 = build produced a ledger that does not verify (should not happen)
  2 = config / setup error

Usage:
    python scripts/build_ledger.py                 # default archive
    python scripts/build_ledger.py --archive PATH
    python scripts/build_ledger.py --force         # rebuild even if healthy
    python scripts/build_ledger.py --no-commit     # write file, don't commit
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype import ledger


def find_archive(explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    env = os.environ.get("HOLOTYPE_ARCHIVE")
    if env:
        return Path(env).expanduser().resolve()
    pointer = Path.home() / ".config" / "holotype" / "archive-path"
    if pointer.exists():
        return Path(pointer.read_text().strip()).expanduser().resolve()
    return (Path.home() / "Documents" / "holotype-archive").resolve()


def _commit_ledger(archive: Path, sign: bool) -> None:
    subprocess.run(["git", "-C", str(archive), "add", ledger.LEDGER_RELPATH],
                   check=False)
    staged = subprocess.run(
        ["git", "-C", str(archive), "diff", "--cached", "--quiet"]
    )
    if staged.returncode == 0:
        return  # nothing staged (ledger byte-identical to what's committed)
    args = ["git", "-C", str(archive), "commit", "-m",
            "ledger: (re)build hash chain from on-disk deposits"]
    if sign:
        args.insert(3, "-S")
    subprocess.run(args, check=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the holotype hash-chain ledger.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--force", action="store_true",
                   help="Rebuild from scratch even if a healthy ledger exists.")
    p.add_argument("--no-commit", action="store_true",
                   help="Write ledger.jsonl but do not git-commit it.")
    p.add_argument("--sign", action="store_true", help="Sign the commit (-S).")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        return 2

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg)

    if not args.force and ledger.ledger_path(archive).exists():
        result = ledger.verify_chain(archive)
        if result["ok"]:
            say(f"  ledger already healthy: {result['length']} link(s), "
                f"head {result['head'][:12]}…")
            say("  (pass --force to rebuild from scratch)")
            return 0
        say(f"  existing ledger does NOT verify "
            f"(broken_at={result['broken_at']}, "
            f"orphans={len(result['orphans'])}); rebuilding.")

    entries = ledger.backfill_entries(archive)
    ledger.write_ledger(archive, entries)
    say(f"  wrote {len(entries)} link(s) → {ledger.LEDGER_RELPATH}")

    check = ledger.verify_chain(archive)
    if not check["ok"]:
        sys.stderr.write(
            f"holotype: rebuilt ledger does not verify "
            f"(broken_at={check['broken_at']}, orphans={check['orphans'][:5]})\n"
        )
        return 1

    if not args.no_commit:
        _commit_ledger(archive, sign=args.sign)

    say(f"  CHAIN OK — {check['length']} link(s), head {check['head'][:12]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
