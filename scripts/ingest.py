#!/usr/bin/env python3
"""Deposit Claude Code session JSONLs into the holotype archive.

Designed to be idempotent and safe to run concurrently with active
Claude Code sessions. Re-running with no new data is a no-op.

Discovery chain:
  1. --archive flag, if passed
  2. HOLOTYPE_ARCHIVE env var
  3. Pointer file at ~/.config/holotype/archive-path
  4. ~/Documents/holotype-archive (default)

Source discovery chain (only if --source not passed):
  1. Sources listed in <archive>/.holotype/config.json (sources.preferred,
     then sources.fallback)
  2. ~/Documents/Claude-Backups
  3. ~/.claude/projects

Live-file safety:
  We re-stat each JSONL after reading it. If mtime changed during the
  read, the file is being actively written; we skip it this round and
  pick it up next time. Sessions modified within the last 2 seconds are
  also skipped as a belt-and-suspenders against the read-stat race.

Concurrency:
  An exclusive lock at <archive>/.holotype/.lock prevents two ingests
  from corrupting the index. A second concurrent ingest exits 0
  silently (its target sessions will be picked up by the running one
  or by the next tick).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype import __version__
from holotype.env import claude_code_version, platform_info, git_head
from holotype.index import open_index, reindex_session, upsert_session
from holotype.manifest import build_manifest

LIVE_FILE_GRACE_SECONDS = 2


def find_archive(explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()

    env_archive = os.environ.get("HOLOTYPE_ARCHIVE")
    if env_archive:
        return Path(env_archive).expanduser().resolve()

    pointer = Path.home() / ".config" / "holotype" / "archive-path"
    if pointer.exists():
        path = pointer.read_text().strip()
        if path:
            return Path(path).expanduser().resolve()

    return (Path.home() / "Documents" / "holotype-archive").resolve()


def load_config(archive: Path) -> dict:
    cfg_path = archive / ".holotype" / "config.json"
    if not cfg_path.exists():
        raise SystemExit(
            f"holotype: no config at {cfg_path}\n"
            f"  run the setup wizard, or pass --archive to point at an existing archive."
        )
    return json.loads(cfg_path.read_text())


def candidate_sources(config: dict, explicit: Path | None) -> list[Path]:
    if explicit:
        return [explicit.expanduser().resolve()]

    out: list[Path] = []
    sources = config.get("sources", {}) or {}
    for key in ("preferred", "fallback"):
        for raw in sources.get(key, []) or []:
            out.append(Path(raw).expanduser().resolve())

    # Final stock fallbacks if config didn't list any
    for default in (Path.home() / "Documents" / "Claude-Backups",
                    Path.home() / ".claude" / "projects"):
        if default not in out:
            out.append(default)

    return [p for p in out if p.exists()]


def walk_jsonls(source: Path):
    """Yield (project_dir_encoded, jsonl_path, parent_session_id) for every
    deposit candidate in the source — top-level sessions AND the subagent
    JSONLs spawned from them.

    Filesystem shape from Claude Code:
        <source>/<project_dir>/<session-uuid>.jsonl                    (top-level)
        <source>/<project_dir>/<session-uuid>/subagents/<sub>.jsonl    (subagent)

    For top-level sessions we yield parent_session_id=None.
    For subagents we yield the parent UUID extracted from the path so the
    deposit can be routed under the parent in the archive layout.

    Subagent transcripts must be deposited because they contain Claude's
    actual reasoning during delegated work — parent transcripts only record
    "I spawned an agent with this prompt", not the agent's conversation.
    """
    if not source.exists():
        return
    for project_dir in sorted(source.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue

        for jsonl in sorted(project_dir.glob("*.jsonl")):
            if jsonl.is_file():
                yield project_dir.name, jsonl, None

        for parent_dir in sorted(p for p in project_dir.iterdir() if p.is_dir()):
            subagents_dir = parent_dir / "subagents"
            if not subagents_dir.is_dir():
                continue
            for sub_jsonl in sorted(subagents_dir.glob("*.jsonl")):
                if sub_jsonl.is_file():
                    yield project_dir.name, sub_jsonl, parent_dir.name


def is_live_file(path: Path) -> bool:
    """Heuristic: is this JSONL currently being written?"""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age < LIVE_FILE_GRACE_SECONDS


def read_with_stable_check(path: Path) -> bytes | None:
    """Read a file and verify mtime didn't change during the read.

    Returns the bytes if stable, None if the file was being written
    while we read it (caller should retry next ingest).
    """
    try:
        before = path.stat().st_mtime_ns
        data = path.read_bytes()
        after = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    if before != after:
        return None
    return data


def session_archive_dir(
    archive: Path,
    project_dir_encoded: str,
    session_id: str,
    parent_session_id: str | None = None,
) -> Path:
    """Resolve the on-disk location of a session inside the archive.

    Top-level sessions live at sessions/<project>/<session-id>/.
    Subagents live nested under their parent at
    sessions/<project>/<parent-id>/subagents/<sub-id>/ — this keeps the
    parent-subagent relationship visible in the filesystem and makes a
    citable bundle of a parent session naturally include its subagents.
    """
    base = archive / "sessions" / project_dir_encoded
    if parent_session_id is None:
        return base / session_id
    return base / parent_session_id / "subagents" / session_id


def existing_sha256(
    archive: Path,
    project_dir_encoded: str,
    session_id: str,
    parent_session_id: str | None = None,
) -> str | None:
    """If this session is already deposited, return its recorded SHA-256."""
    manifest_path = session_archive_dir(archive, project_dir_encoded, session_id, parent_session_id) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text()).get("sha256")
    except (OSError, json.JSONDecodeError):
        return None


def run_git(archive: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(archive), *args],
        capture_output=True,
        text=True,
    )


def git_head_short(archive: Path) -> str | None:
    out = run_git(archive, "rev-parse", "--short", "HEAD")
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def deposit_one(
    archive: Path,
    project_dir_encoded: str,
    jsonl_path: Path,
    parent_session_id: str | None,
    *,
    verbose: bool,
) -> tuple[str, str]:
    """Deposit a single JSONL. Returns (status, session_id) for reporting.

    Status is one of: 'new', 'updated', 'skipped-live', 'skipped-unchanged',
    'skipped-empty'.
    """
    session_id = jsonl_path.stem

    if is_live_file(jsonl_path):
        return ("skipped-live", session_id)

    data = read_with_stable_check(jsonl_path)
    if data is None:
        return ("skipped-live", session_id)
    if not data.strip():
        return ("skipped-empty", session_id)

    dest_dir = session_archive_dir(archive, project_dir_encoded, session_id, parent_session_id)
    dest_jsonl = dest_dir / "transcript.jsonl"
    dest_manifest = dest_dir / "manifest.json"

    prior_sha = existing_sha256(archive, project_dir_encoded, session_id, parent_session_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    tmp_jsonl = dest_jsonl.with_suffix(".jsonl.partial")
    tmp_jsonl.write_bytes(data)
    os.replace(tmp_jsonl, dest_jsonl)

    env = {
        "holotype_version": __version__,
        "claude_code_version": claude_code_version(),
        "platform": platform_info(),
    }
    manifest = build_manifest(
        dest_jsonl,
        project_dir_encoded=project_dir_encoded,
        holotype_version=__version__,
        source_path=str(jsonl_path),
        env=env,
        parent_session_id=parent_session_id,
    )

    if prior_sha == manifest.sha256:
        return ("skipped-unchanged", session_id)

    tmp_manifest = dest_manifest.with_suffix(".json.partial")
    tmp_manifest.write_text(manifest.to_json())
    os.replace(tmp_manifest, dest_manifest)

    return ("updated" if prior_sha else "new", session_id)


def commit_deposit(
    archive: Path,
    project_dir_encoded: str,
    session_id: str,
    status: str,
    parent_session_id: str | None,
) -> str | None:
    """Stage and commit one deposit. Returns the new commit's short SHA, or
    None if there was nothing to commit (which should be rare given the
    caller filters)."""
    if parent_session_id is None:
        rel = f"sessions/{project_dir_encoded}/{session_id}"
    else:
        rel = f"sessions/{project_dir_encoded}/{parent_session_id}/subagents/{session_id}"
    run_git(archive, "add", rel)

    diff = run_git(archive, "diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return None

    verb = "deposit" if status == "new" else "update"
    msg = f"{verb}: {rel}"
    out = run_git(archive, "commit", "-m", msg)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return None
    return git_head_short(archive)


def update_index(
    archive: Path,
    project_dir_encoded: str,
    session_id: str,
    parent_session_id: str | None,
    git_commit: str | None,
) -> None:
    index_path = archive / ".holotype" / "index.sqlite"
    sess_dir = session_archive_dir(archive, project_dir_encoded, session_id, parent_session_id)
    transcript = sess_dir / "transcript.jsonl"
    manifest_path = sess_dir / "manifest.json"
    if not (transcript.exists() and manifest_path.exists()):
        return

    manifest = json.loads(manifest_path.read_text())

    with open_index(index_path) as conn:
        upsert_session(
            conn,
            session_id=session_id,
            parent_session_id=parent_session_id,
            project_dir=project_dir_encoded,
            first_ts=manifest.get("first_timestamp"),
            last_ts=manifest.get("last_timestamp"),
            message_count=manifest.get("message_count", 0),
            sha256=manifest.get("sha256", ""),
            deposited_at=manifest.get("deposited_at", ""),
            git_commit=git_commit,
        )
        reindex_session(conn, session_id, transcript)
        conn.commit()


def acquire_lock(archive: Path):
    """Take an exclusive non-blocking flock on the archive. Returns a file
    handle that the caller must keep alive (file is unlocked on close)."""
    lock_path = archive / ".holotype" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Deposit Claude Code sessions into the archive.")
    p.add_argument("--archive", type=Path, default=None, help="Archive path (overrides discovery).")
    p.add_argument("--source", type=Path, default=None, help="Where to read JSONLs from.")
    p.add_argument("--quiet", action="store_true", help="No output unless something deposited.")
    p.add_argument("--dry-run", action="store_true", help="Report what would change, do not write.")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    config = load_config(archive)
    sources = candidate_sources(config, args.source)

    if not sources:
        sys.stderr.write("holotype: no source directories exist\n")
        return 2

    lock = acquire_lock(archive)
    if lock is None:
        if not args.quiet:
            print("holotype: another ingest is running, exiting")
        return 0

    try:
        counts = {"new": 0, "updated": 0, "skipped-live": 0,
                  "skipped-unchanged": 0, "skipped-empty": 0}
        committed: list[str] = []

        for source in sources:
            for project_dir_encoded, jsonl_path, parent_session_id in walk_jsonls(source):
                if args.dry_run:
                    counts["new"] += 1  # rough proxy in dry-run mode
                    continue

                status, session_id = deposit_one(
                    archive, project_dir_encoded, jsonl_path,
                    parent_session_id, verbose=not args.quiet,
                )
                counts[status] = counts.get(status, 0) + 1

                if status in ("new", "updated"):
                    commit = commit_deposit(archive, project_dir_encoded,
                                            session_id, status, parent_session_id)
                    update_index(archive, project_dir_encoded, session_id,
                                 parent_session_id, commit)
                    tag = f"{parent_session_id[:8]}/subagents/{session_id[:8]}" if parent_session_id else session_id[:8]
                    committed.append(f"  {status:>8s}  {project_dir_encoded}/{tag}")

        any_changes = counts["new"] + counts["updated"] > 0
        if not args.quiet or any_changes:
            print(
                f"holotype ingest: new={counts['new']} updated={counts['updated']}"
                f" skipped-live={counts['skipped-live']}"
                f" unchanged={counts['skipped-unchanged']}"
                f" empty={counts['skipped-empty']}"
            )
            if committed and not args.quiet:
                print("\n".join(committed))

        return 0 if any_changes else 1
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
