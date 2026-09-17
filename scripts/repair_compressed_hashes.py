#!/usr/bin/env python3
"""Rewrite stale ``sha256_compressed`` values in deposit manifests.

A compressed deposit records two hashes: ``sha256`` of the canonical
(uncompressed) transcript, which is what gets cited and chained, and
``sha256_compressed`` of the ``.zst`` file as it sits on disk, which lets a
reviewer without zstd check the file directly.

Before v2.4.1, a pure manifest-version migration copied
``sha256_compressed`` from the prior manifest instead of hashing the file.
When the prior value was already stale, the migration carried it forward.
The content is intact (the canonical hash still matches), but the ingest
sweep and the no-zstd track of ``verify.py`` both report the deposit as an
integrity failure.

This script finds every compressed deposit whose file hash differs from
``sha256_compressed``, decompresses it, and checks the result against
``sha256``. Only when the canonical hash matches does it rewrite
``sha256_compressed``. A deposit whose canonical hash does NOT match is real
damage: it is reported and left alone.

The rewrite changes one hex string in each manifest and nothing else, so the
hash-chain ledger (which links canonical ``sha256`` values only) is
unaffected. All repaired manifests go into one ``repair:`` commit.

Usage:
    python scripts/repair_compressed_hashes.py --dry-run   # report only
    python scripts/repair_compressed_hashes.py             # repair + commit

Exit codes:
    0  nothing stale, or all stale hashes repaired
    1  at least one deposit has real content damage (never rewritten)
    2  another ingest holds the archive lock, or zstd is missing
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.archive import iter_all_sessions  # noqa: E402
from holotype.compression import (  # noqa: E402
    COMPRESSED_TRANSCRIPT,
    decompress_bytes,
    zstd_available,
)
from holotype.hashing import sha256_bytes  # noqa: E402


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


def run_git(archive: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(archive), *args], capture_output=True, text=True
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="Report stale hashes without rewriting anything.")
    args = p.parse_args(argv)

    if not zstd_available():
        print("repair: zstd not found on PATH; cannot check canonical hashes.",
              file=sys.stderr)
        return 2

    archive = find_archive(args.archive)
    lock_path = archive / ".holotype" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("repair: an ingest is running; try again when it finishes.",
              file=sys.stderr)
        return 2

    try:
        stale: list[tuple[Path, str, str]] = []
        damaged: list[Path] = []
        checked = 0
        for sess_dir, manifest in iter_all_sessions(archive):
            zst = sess_dir / COMPRESSED_TRANSCRIPT
            recorded = manifest.get("sha256_compressed")
            if not zst.exists() or not recorded:
                continue
            checked += 1
            raw = zst.read_bytes()
            actual = sha256_bytes(raw)
            if actual == recorded:
                continue
            try:
                canonical = sha256_bytes(decompress_bytes(raw))
            except Exception:
                canonical = None
            if canonical and canonical == manifest.get("sha256"):
                stale.append((sess_dir, recorded, actual))
            else:
                damaged.append(sess_dir)

        rel = lambda d: d.relative_to(archive)  # noqa: E731
        print(f"repair: checked {checked} compressed deposit(s): "
              f"{len(stale)} stale hash(es), {len(damaged)} damaged")
        for d in damaged:
            print(f"  DAMAGED (not rewritten): {rel(d)}", file=sys.stderr)

        if args.dry_run or not stale:
            return 1 if damaged else 0

        for sess_dir, old, new in stale:
            path = sess_dir / "manifest.json"
            text = path.read_text()
            if text.count(old) != 1:
                print(f"  skipped (hash not found exactly once): {rel(sess_dir)}",
                      file=sys.stderr)
                continue
            tmp = path.with_suffix(".json.partial")
            tmp.write_text(text.replace(old, new))
            os.replace(tmp, path)

        paths = [f"{rel(d)}/manifest.json" for d, _, _ in stale]
        for i in range(0, len(paths), 500):
            run_git(archive, "add", *paths[i:i + 500])
        if run_git(archive, "diff", "--cached", "--quiet").returncode != 0:
            config_path = archive / ".holotype" / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (OSError, json.JSONDecodeError):
                config = {}
            sign = bool((config.get("deposit") or {}).get("sign_commits"))
            msg = (
                f"repair: rewrite {len(stale)} stale sha256_compressed value(s)\n\n"
                "Each manifest's sha256_compressed no longer matched its .zst\n"
                "file. The decompressed content still matches the canonical\n"
                "sha256, so only the compressed hash was rewritten. Transcripts\n"
                "and the hash-chain ledger are unchanged."
            )
            commit_args = ["commit", "-m", msg]
            if sign:
                commit_args.insert(1, "-S")
            out = run_git(archive, *commit_args)
            if out.returncode != 0:
                print(f"repair: commit failed: {out.stderr.strip()}", file=sys.stderr)
                return 2
            print(f"repair: committed {len(stale)} manifest(s)")
        return 1 if damaged else 0
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
