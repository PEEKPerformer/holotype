#!/usr/bin/env python3
"""Drop old git history from the local archive clone. The remote keeps all of it.

Every update to a session stores the whole transcript again, and encrypted,
compressed blobs get no delta against earlier versions. Over time most of
``.git`` is superseded versions that only matter for history, and history
is already on the remote. This script makes the local clone shallow: it
keeps the commits from the last ``--keep-days`` days (at least the current
commit) and deletes older objects from disk.

What does NOT change:
  - The working tree. Every session's current transcript and manifest stay
    on disk, so verify, cite, search, and browse work as before.
  - The remote. Nothing is pushed or rewritten there.
  - Future deposits and pushes. A shallow clone commits and pushes normally.

What does change: ``git log`` and ``git show`` locally reach back only
``--keep-days``. For older history, clone the remote.

Safety checks, all before anything is deleted:
  - the archive has an ``origin`` remote with the current branch on it;
  - after a fetch, every local commit is already on the remote;
  - no other local branch or stash holds commits the remote lacks;
  - no ingest is running (the ingest lock is held for the whole run).

Without ``--yes`` the script only prints the plan.

Usage:
    python scripts/prune_local.py                  # show the plan
    python scripts/prune_local.py --yes            # keep the last 7 days
    python scripts/prune_local.py --keep-days 3 --yes

Exit codes:
    0  pruned, or plan printed
    1  a safety check failed; nothing was deleted
    2  an ingest is running, or a git step failed
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A fetch or repack of a multi-GB archive can take a while, but a network
# stall must not hold the ingest lock forever.
GIT_TIMEOUT_SECONDS = 3600


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


def git(archive: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(archive), *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
    )


def objects_size(archive: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(archive / ".git" / "objects"):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def gib(n: int) -> str:
    return f"{n / 2**30:.2f} GiB"


def fail(msg: str, code: int = 1) -> int:
    print(f"prune_local: {msg}", file=sys.stderr)
    return code


def unpushed_refs(archive: Path, branch: str) -> list[str]:
    """Local refs (other than the current branch) with commits the remote lacks."""
    problems = []
    if git(archive, "rev-parse", "--verify", "--quiet", "refs/stash").returncode == 0:
        problems.append("refs/stash")
    refs = git(archive, "for-each-ref", "--format=%(refname)", "refs/heads").stdout.split()
    for ref in refs:
        if ref == f"refs/heads/{branch}":
            continue
        # Commits reachable from this ref but from no remote-tracking ref.
        out = git(archive, "rev-list", "--count", ref, "--not", "--remotes=origin")
        if out.returncode != 0 or out.stdout.strip() != "0":
            problems.append(ref)
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--keep-days", type=float, default=7,
                   help="Keep local history from the last N days (default 7).")
    p.add_argument("--yes", action="store_true",
                   help="Prune. Without it, only print the plan.")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".git").is_dir():
        return fail(f"{archive} is not a git repository")

    lock_path = archive / ".holotype" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return fail("an ingest is running; try again when it finishes", 2)

    try:
        if git(archive, "remote", "get-url", "origin").returncode != 0:
            return fail("no `origin` remote. Local history is the only copy; not pruning.")
        branch = git(archive, "symbolic-ref", "--short", "HEAD").stdout.strip()
        if not branch:
            return fail("HEAD is detached; check out the archive's branch first")

        print(f"prune_local: fetching origin/{branch} ...")
        fetch = git(archive, "fetch", "origin", branch)
        if fetch.returncode != 0:
            return fail(f"fetch failed: {fetch.stderr.strip()}", 2)
        remote_ref = f"refs/remotes/origin/{branch}"
        if git(archive, "rev-parse", "--verify", "--quiet", remote_ref).returncode != 0:
            return fail(f"origin has no branch {branch}; push first")

        ahead = git(archive, "rev-list", "--count", f"{remote_ref}..HEAD").stdout.strip()
        if ahead != "0":
            return fail(f"{ahead} local commit(s) are not on origin/{branch}. "
                        f"Push first (`git -C {archive} push`).")
        others = unpushed_refs(archive, branch)
        if others:
            return fail("these refs hold commits the remote lacks: " + ", ".join(others))

        since = datetime.now(timezone.utc) - timedelta(days=args.keep_days)
        since_arg = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        total = git(archive, "rev-list", "--count", "HEAD").stdout.strip()
        keep = git(archive, "rev-list", "--count", f"--since={since_arg}", "HEAD").stdout.strip()
        before = objects_size(archive)
        print(f"prune_local: {archive}")
        print(f"  local objects:  {gib(before)}")
        print(f"  local commits:  {total}; keeping {max(int(keep or 0), 1)} "
              f"from the last {args.keep_days:g} day(s)")
        if not args.yes:
            print("  (plan only; re-run with --yes to prune)")
            return 0

        # Move the shallow boundary. With no commit in the window, keep
        # just the current one.
        out = git(archive, "fetch", f"--shallow-since={since_arg}", "origin", branch)
        if out.returncode != 0:
            out = git(archive, "fetch", "--depth=1", "origin", branch)
            if out.returncode != 0:
                return fail(f"shallow fetch failed: {out.stderr.strip()}", 2)

        # Reflogs still point at the dropped commits; expire them, then
        # rewrite the packs without unreachable objects. pack.window=0
        # skips the delta search: transcripts are encrypted or compressed,
        # so it would find nothing and only cost memory.
        steps = [
            ("reflog", ["reflog", "expire", "--expire=now", "--all"]),
            ("repack", ["-c", "pack.window=0", "-c", "pack.threads=1",
                        "repack", "-a", "-d", "-q"]),
            ("prune", ["prune", "--expire=now"]),
            ("check", ["fsck", "--connectivity-only", "--no-progress"]),
        ]
        for name, cmd in steps:
            print(f"prune_local: {name} ...")
            out = git(archive, *cmd)
            if out.returncode != 0:
                return fail(f"{name} failed: {out.stderr.strip()}", 2)

        after = objects_size(archive)
        print(f"prune_local: local objects {gib(before)} -> {gib(after)} "
              f"(freed {gib(before - after)})")
        return 0
    except subprocess.TimeoutExpired as e:
        return fail(f"git timed out after {GIT_TIMEOUT_SECONDS}s: {' '.join(e.cmd)}", 2)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
