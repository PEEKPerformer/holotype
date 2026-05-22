#!/usr/bin/env python3
"""Deposit agent-CLI session JSONLs into the holotype archive.

Designed to be idempotent and safe to run concurrently with active
agent-CLI sessions. Re-running with no new data is a no-op.

Discovery chains:
  Archive: --archive flag → HOLOTYPE_ARCHIVE env → ~/.config/holotype/
           archive-path → ~/Documents/holotype-archive (default)

  Sources: --source PATH and --source-name (single source override)
           or auto-discover from each registered Source's
           default_source_paths(), filtered to existing dirs.

Live-file safety: skip JSONLs modified in the last 2 seconds and
re-check mtime after reading. Atomic writes (write-then-rename).

Concurrency: exclusive flock at <archive>/.holotype/.lock prevents two
ingests from corrupting the index. A second concurrent ingest exits 0
silently.

Sensitivity: each Source declares its own root paths and is responsible
for refusing to traverse above them. ingest.py does NOT walk above the
declared source roots — e.g. Codex's `~/.codex/auth.json` is never
read because CodexSource.default_source_paths() returns only
`~/.codex/sessions/`.
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
from holotype.compression import compress_bytes, transcript_filename
from holotype.env import claude_code_version, git_state_for_path, platform_info
from holotype.hashing import sha256_bytes
from holotype.index import open_index, reindex_session, upsert_session
from holotype.manifest import MANIFEST_VERSION, build_manifest
from holotype.sources import ALL_SOURCES, source_by_name
from holotype.sources.base import DepositCandidate, Source

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


def discover_candidates(
    config: dict,
    explicit_source: Path | None,
    explicit_source_name: str | None,
) -> list[tuple[type[Source], DepositCandidate]]:
    """Build the deduplicated deposit-candidate list across all Sources.

    With --source and --source-name, only the named Source is consulted
    and only at the given path. Otherwise we walk each Source's
    default_source_paths() filtered to ones that actually exist.

    Many Sources list multiple default paths that mirror each other
    (Claude Code: rsync backup + live ~/.claude/projects). Without
    dedup, every session is yielded once per matching path, doubling
    ingest work. We dedupe by (source_name, session_id), keeping the
    FIRST occurrence — Sources are expected to list their most durable
    path first (e.g. an immutable rsync mirror before a live source
    Claude Code itself prunes).
    """
    candidates: list[tuple[type[Source], DepositCandidate]] = []
    seen: set[tuple[str, str]] = set()

    def _add(cls: type[Source], c: DepositCandidate) -> None:
        key = (cls.name, c.session_id)
        if key in seen:
            return
        seen.add(key)
        candidates.append((cls, c))

    if explicit_source is not None:
        explicit_source = explicit_source.expanduser().resolve()
        if explicit_source_name is None:
            explicit_source_name = "claude-code"
        try:
            cls = source_by_name(explicit_source_name)
        except KeyError as e:
            raise SystemExit(f"holotype: {e}")
        for c in cls.discover(explicit_source):
            _add(cls, c)
        return candidates

    for cls in ALL_SOURCES:
        for root in cls.default_source_paths():
            root = root.expanduser()
            if not root.exists():
                continue
            try:
                root_resolved = root.resolve(strict=True)
            except OSError:
                continue
            for c in cls.discover(root_resolved):
                _add(cls, c)

    return candidates


def is_live_file(path: Path) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age < LIVE_FILE_GRACE_SECONDS


def read_with_stable_check(path: Path) -> bytes | None:
    try:
        before = path.stat().st_mtime_ns
        data = path.read_bytes()
        after = path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    if before != after:
        return None
    return data


def session_archive_dir(archive: Path, candidate: DepositCandidate) -> Path:
    return archive / "sessions" / candidate.archive_subpath


def existing_manifest(archive: Path, candidate: DepositCandidate) -> dict | None:
    manifest_path = session_archive_dir(archive, candidate) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def existing_sha256(archive: Path, candidate: DepositCandidate) -> str | None:
    m = existing_manifest(archive, candidate)
    return m.get("sha256") if m else None


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
    source_cls: type[Source],
    candidate: DepositCandidate,
    *,
    compression: str | None,
) -> tuple[str, str]:
    """Deposit one candidate. Returns (status, session_id).

    When ``compression == "zstd"``, the transcript is stored as
    ``transcript.jsonl.zst`` and the manifest records both ``sha256``
    (of the uncompressed canonical bytes — what's cited) and
    ``sha256_compressed`` (of the file as it sits on disk — for
    no-zstd reviewers).
    """
    jsonl_path = candidate.jsonl_path
    session_id = candidate.session_id

    if is_live_file(jsonl_path):
        return ("skipped-live", session_id)

    data = read_with_stable_check(jsonl_path)
    if data is None:
        return ("skipped-live", session_id)
    if not data.strip():
        return ("skipped-empty", session_id)

    dest_dir = session_archive_dir(archive, candidate)
    on_disk_name = transcript_filename(compression)
    dest_transcript = dest_dir / on_disk_name
    dest_manifest = dest_dir / "manifest.json"

    prior_manifest = existing_manifest(archive, candidate)
    prior_sha = prior_manifest.get("sha256") if prior_manifest else None
    prior_version = prior_manifest.get("manifest_version") if prior_manifest else None

    # Idempotency: short-circuit before writing anything if the new bytes
    # match what's already deposited AND the manifest is the current
    # schema version. Older-version manifests get re-processed so they
    # pick up new fields (project_git_state, token totals, etc.) — this
    # is critical when a Source parser bug fix would otherwise be hidden
    # behind the "unchanged sha" short-circuit.
    new_sha = sha256_bytes(data)
    if prior_sha == new_sha and prior_version == MANIFEST_VERSION:
        return ("skipped-unchanged", session_id)

    dest_dir.mkdir(parents=True, exist_ok=True)

    if compression == "zstd":
        payload = compress_bytes(data)
        sha_compressed = sha256_bytes(payload)
    else:
        payload = data
        sha_compressed = None

    tmp_path = dest_transcript.with_suffix(dest_transcript.suffix + ".partial")
    tmp_path.write_bytes(payload)
    os.replace(tmp_path, dest_transcript)

    # If switching modes (e.g. a prior plain deposit, now compressed),
    # remove the stale alternate-form file so the session dir stays
    # compression-uniform. Cheap and only triggers on the rare migration.
    for stale_name in ("transcript.jsonl", "transcript.jsonl.zst"):
        if stale_name == on_disk_name:
            continue
        stale = dest_dir / stale_name
        if stale.exists():
            stale.unlink()

    env = {
        "holotype_version": __version__,
        "claude_code_version": claude_code_version(),
        "platform": platform_info(),
    }

    # Probe the project's git state at deposit time, using the cwd the
    # host CLI recorded (decoded from the project_dir_encoded string).
    # If the source itself recorded a session-start git snapshot (e.g.
    # Codex's session_meta), build_manifest will prefer that — the
    # deposit-time probe is the fallback.
    deposit_time_git = None
    from holotype.manifest import project_dir_decoded as _decode
    decoded = _decode(candidate.project_dir_encoded)
    if decoded:
        try:
            deposit_time_git = git_state_for_path(Path(decoded))
        except Exception:
            deposit_time_git = None

    manifest = build_manifest(
        data,
        source_cls,
        project_dir_encoded=candidate.project_dir_encoded,
        session_id=candidate.session_id,
        on_disk_filename=on_disk_name,
        holotype_version=__version__,
        source_path=str(jsonl_path),
        env=env,
        parent_session_id=candidate.parent_session_id,
        compression=compression,
        sha256_compressed=sha_compressed,
        project_git_state=deposit_time_git,
    )

    tmp_manifest = dest_manifest.with_suffix(".json.partial")
    tmp_manifest.write_text(manifest.to_json())
    os.replace(tmp_manifest, dest_manifest)

    return ("updated" if prior_sha else "new", session_id)


def commit_deposit(
    archive: Path,
    candidate: DepositCandidate,
    status: str,
    sign_commits: bool = False,
) -> str | None:
    rel = f"sessions/{candidate.archive_subpath}"
    run_git(archive, "add", rel)
    diff = run_git(archive, "diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return None
    verb = "deposit" if status == "new" else "update"
    msg = f"{verb}: [{candidate.source_name}] {rel}"
    commit_args = ["commit", "-m", msg]
    if sign_commits:
        commit_args.insert(1, "-S")
    out = run_git(archive, *commit_args)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return None
    return git_head_short(archive)


def update_index(
    archive: Path,
    source_cls: type[Source],
    candidate: DepositCandidate,
    git_commit: str | None,
) -> None:
    from holotype.compression import find_transcript

    index_path = archive / ".holotype" / "index.sqlite"
    sess_dir = session_archive_dir(archive, candidate)
    found = find_transcript(sess_dir)
    manifest_path = sess_dir / "manifest.json"
    if found is None or not manifest_path.exists():
        return

    manifest = json.loads(manifest_path.read_text())

    with open_index(index_path) as conn:
        upsert_session(
            conn,
            session_id=candidate.session_id,
            source=source_cls.name,
            parent_session_id=candidate.parent_session_id,
            project_dir=candidate.project_dir_encoded,
            first_ts=manifest.get("first_timestamp"),
            last_ts=manifest.get("last_timestamp"),
            message_count=manifest.get("message_count", 0),
            sha256=manifest.get("sha256", ""),
            deposited_at=manifest.get("deposited_at", ""),
            git_commit=git_commit,
        )
        reindex_session(conn, candidate.session_id, sess_dir, source_cls=source_cls)
        conn.commit()


def acquire_lock(archive: Path):
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
    p = argparse.ArgumentParser(description="Deposit agent-CLI sessions into the archive.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--source", type=Path, default=None,
                   help="Override source path (single source). Pair with --source-name.")
    p.add_argument("--source-name", default=None,
                   choices=[s.name for s in ALL_SOURCES],
                   help="When --source is given, which Source's parser to use.")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    config = load_config(archive)
    deposit_cfg = config.get("deposit") or {}
    compression = deposit_cfg.get("compression") or None
    if compression == "none":
        compression = None
    sign_commits = bool(deposit_cfg.get("sign_commits"))
    candidates = discover_candidates(config, args.source, args.source_name)

    if not candidates and not args.dry_run:
        if not args.quiet:
            print("holotype ingest: nothing to deposit (no sources or no candidates)")
        return 1

    lock = acquire_lock(archive)
    if lock is None:
        if not args.quiet:
            print("holotype: another ingest is running, exiting")
        return 0

    try:
        counts: dict[str, int] = {"new": 0, "updated": 0, "skipped-live": 0,
                                  "skipped-unchanged": 0, "skipped-empty": 0}
        committed: list[str] = []

        for source_cls, candidate in candidates:
            if args.dry_run:
                counts["new"] += 1
                continue

            status, session_id = deposit_one(archive, source_cls, candidate, compression=compression)
            counts[status] = counts.get(status, 0) + 1

            if status in ("new", "updated"):
                commit = commit_deposit(archive, candidate, status, sign_commits=sign_commits)
                update_index(archive, source_cls, candidate, commit)
                committed.append(f"  {status:>8s}  [{source_cls.name}] {candidate.archive_subpath}")

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
