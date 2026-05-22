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
from holotype.index import (
    open_index,
    reindex_session,
    session_indexed,
    upsert_session,
)
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
) -> tuple[str, str, bool]:
    """Deposit one candidate. Returns (status, session_id, transcript_changed).

    ``transcript_changed`` is True only when the on-disk transcript bytes
    differ from the prior deposit (i.e. a real "new" or content-update).
    For pure manifest-version migrations (the bytes are unchanged but
    the manifest schema bumped), it's False — the caller can skip
    expensive FTS rebuilds since the indexed content didn't change.

    When ``compression == "zstd"``, the transcript is stored as
    ``transcript.jsonl.zst`` and the manifest records both ``sha256``
    (of the uncompressed canonical bytes — what's cited) and
    ``sha256_compressed`` (of the file as it sits on disk — for
    no-zstd reviewers).
    """
    jsonl_path = candidate.jsonl_path
    session_id = candidate.session_id

    if is_live_file(jsonl_path):
        return ("skipped-live", session_id, False)

    data = read_with_stable_check(jsonl_path)
    if data is None:
        return ("skipped-live", session_id, False)
    if not data.strip():
        return ("skipped-empty", session_id, False)

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
        return ("skipped-unchanged", session_id, False)

    transcript_changed = (prior_sha != new_sha)

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

    return ("updated" if prior_sha else "new", session_id, transcript_changed)


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


def commit_bulk_initial(
    archive: Path,
    candidates: list[DepositCandidate],
    sign_commits: bool = False,
) -> str | None:
    """One combined commit for the first-time backfill of an archive.

    The user opted into ``--bulk-initial`` at first ingest. Instead of
    one ``deposit:`` commit per session — which signs N times and is
    the dominant first-time cost when ``sign_commits`` is on — we
    bundle every new deposit into a single ``bulk-initial`` commit.

    Loses per-session ordering INSIDE the initial backfill (all
    bundled sessions share one commit, so you can't see "A deposited
    before B" within the initial batch). Future per-session ingests
    are unaffected — they continue to emit one commit each via
    ``commit_deposit``.
    """
    if not candidates:
        return None
    rels = [f"sessions/{c.archive_subpath}" for c in candidates]
    CHUNK = 500
    for i in range(0, len(rels), CHUNK):
        run_git(archive, "add", *rels[i:i + CHUNK])
    diff = run_git(archive, "diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return None
    sources = sorted({c.source_name for c in candidates})
    src_tag = "+".join(sources) if sources else "?"
    msg = (
        f"bulk-initial: {len(candidates)} session(s) ingested [{src_tag}]\n\n"
        f"First-time backfill of historical sessions. Per-session "
        f"granular commits resume on subsequent ingests; this commit "
        f"represents the initial seeding of the archive."
    )
    commit_args = ["commit", "-m", msg]
    if sign_commits:
        commit_args.insert(1, "-S")
    out = run_git(archive, *commit_args)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return None
    return git_head_short(archive)


def commit_manifest_migration(
    archive: Path,
    candidates: list[DepositCandidate],
    sign_commits: bool = False,
) -> str | None:
    """One combined commit for manifest-only refreshes (transcript bytes
    unchanged — typically a manifest_version schema bump).

    For genuine deposits (new sessions or content updates) we still emit
    one commit per session via ``commit_deposit``, preserving the
    granular per-session history. But for schema migrations where the
    on-disk transcript hasn't changed, emitting N individual "update:"
    commits is a lie — there's one logical event (the schema bump), not
    N. Combining them into one commit is faster (avoids N×100ms git
    overhead) AND more honest about what changed.

    `git log sessions/<X>/<Y>/manifest.json` still finds this commit;
    the file's history just shares one commit with its siblings instead
    of having its own.
    """
    if not candidates:
        return None

    rels = [f"sessions/{c.archive_subpath}" for c in candidates]
    # Stage in one shot. `git add` accepts many paths; we chunk to stay
    # under argv limits for very large migrations (well above what we'd
    # ever hit but cheap insurance).
    CHUNK = 500
    for i in range(0, len(rels), CHUNK):
        run_git(archive, "add", *rels[i:i + CHUNK])

    diff = run_git(archive, "diff", "--cached", "--quiet")
    if diff.returncode == 0:
        return None

    sources = sorted({c.source_name for c in candidates})
    src_tag = "+".join(sources) if sources else "?"
    msg = (
        f"migrate: refresh {len(candidates)} manifest(s) to schema v"
        f"{MANIFEST_VERSION} [{src_tag}]\n\n"
        f"Manifest-only update: on-disk transcript bytes unchanged; new "
        f"manifest_version fields written. See CHANGELOG for what each "
        f"version bump added."
    )
    commit_args = ["commit", "-m", msg]
    if sign_commits:
        commit_args.insert(1, "-S")
    out = run_git(archive, *commit_args)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return None
    return git_head_short(archive)


def update_index(
    conn,
    archive: Path,
    source_cls: type[Source],
    candidate: DepositCandidate,
    git_commit: str | None,
    transcript_changed: bool,
) -> None:
    """Reflect a single deposit into the shared SQLite index.

    Caller owns the connection — one connection per ingest cycle. We
    don't commit here; the main loop batches commits.

    ``transcript_changed=False`` means the on-disk transcript bytes are
    unchanged from the prior deposit (e.g. a manifest-version migration).
    The FTS rows for this session are still correct under that condition,
    so we skip the expensive DELETE+INSERT rebuild and just refresh the
    sessions-table row. This is the single biggest perf win for bulk
    re-ingests triggered by a manifest schema bump.
    """
    from holotype.compression import find_transcript

    sess_dir = session_archive_dir(archive, candidate)
    found = find_transcript(sess_dir)
    manifest_path = sess_dir / "manifest.json"
    if found is None or not manifest_path.exists():
        return

    manifest = json.loads(manifest_path.read_text())

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

    # Skip FTS rebuild when the transcript bytes haven't changed AND we
    # already have the per-message rows indexed. Both conditions matter:
    # if the index was wiped (schema bump or `reindex.py`), we still
    # need to populate even unchanged transcripts.
    if not transcript_changed and session_indexed(conn, candidate.session_id):
        return

    reindex_session(conn, candidate.session_id, sess_dir, source_cls=source_cls)


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
    p.add_argument(
        "--bulk-initial",
        action="store_true",
        help=(
            "First-time-only mode: bundle ALL transcript-changed deposits "
            "into a single `bulk-initial:` commit instead of one commit "
            "per session. Dramatically faster when GPG signing is on "
            "(one signature vs. N). Loses per-session ordering inside "
            "the initial backfill; future per-session ingests are "
            "unaffected. Should only be passed once, on the first ingest "
            "of a new archive."
        ),
    )
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    config = load_config(archive)
    deposit_cfg = config.get("deposit") or {}
    compression = deposit_cfg.get("compression") or None
    if compression == "none":
        compression = None
    sign_commits = bool(deposit_cfg.get("sign_commits"))
    auto_push = bool(deposit_cfg.get("auto_push"))
    remote_url = ((config.get("remote") or {}).get("url") or "").strip()
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

        index_path = archive / ".holotype" / "index.sqlite"
        # One SQLite connection for the whole cycle (vs. open-per-session
        # in the old code path, which was the dominant per-session cost
        # at scale). Commit in batches of INDEX_COMMIT_BATCH sessions to
        # bound WAL size; the final commit happens in the `finally`
        # below regardless of how the loop exits.
        INDEX_COMMIT_BATCH = 50
        pending_since_commit = 0

        # Three buckets:
        # - migrations[]: manifest-only refreshes (transcript bytes
        #   unchanged) → one combined `migrate:` commit at end of cycle.
        # - bulk_initial[]: present only when --bulk-initial is passed.
        #   Genuine content changes go here too instead of per-session
        #   commits, so the first-time backfill signs/commits once.
        # - per-session committed: when --bulk-initial is OFF, genuine
        #   content changes get one commit each via commit_deposit().
        migrations: list[tuple[type[Source], DepositCandidate]] = []
        bulk_initial: list[tuple[type[Source], DepositCandidate]] = []

        with open_index(index_path) as conn:
            for source_cls, candidate in candidates:
                if args.dry_run:
                    counts["new"] += 1
                    continue

                status, session_id, transcript_changed = deposit_one(
                    archive, source_cls, candidate, compression=compression
                )
                counts[status] = counts.get(status, 0) + 1

                if status not in ("new", "updated"):
                    continue

                if transcript_changed:
                    if args.bulk_initial:
                        # Defer the git commit; just write index rows
                        # now (cheap — FTS gets the new content).
                        update_index(
                            conn, archive, source_cls, candidate, git_commit=None,
                            transcript_changed=True,
                        )
                        bulk_initial.append((source_cls, candidate))
                    else:
                        # Genuine deposit or content update — per-session commit.
                        commit = commit_deposit(archive, candidate, status, sign_commits=sign_commits)
                        update_index(
                            conn, archive, source_cls, candidate, commit,
                            transcript_changed=True,
                        )
                        committed.append(f"  {status:>8s}  [{source_cls.name}] {candidate.archive_subpath}")
                else:
                    # Manifest-only refresh — defer the git commit; do the
                    # index work now (cheap because we skip FTS rebuild).
                    update_index(
                        conn, archive, source_cls, candidate, git_commit=None,
                        transcript_changed=False,
                    )
                    migrations.append((source_cls, candidate))

                pending_since_commit += 1
                if pending_since_commit >= INDEX_COMMIT_BATCH:
                    conn.commit()
                    pending_since_commit = 0

            # Final batch flush.
            if pending_since_commit:
                conn.commit()

        # One combined commit for all bulk-initial deposits (when the
        # user passed --bulk-initial for first-time backfill). Runs
        # before the migration commit so the bulk-initial appears first
        # in `git log` — chronological order for the user.
        if bulk_initial:
            bi_candidates = [c for _, c in bulk_initial]
            bi_commit = commit_bulk_initial(archive, bi_candidates, sign_commits=sign_commits)
            if bi_commit:
                committed.append(
                    f"  bulk-initial  {len(bi_candidates)} session(s) ingested"
                )
                with open_index(index_path) as conn2:
                    conn2.executemany(
                        "UPDATE sessions SET git_commit = ? WHERE session_id = ?",
                        [(bi_commit, c.session_id) for _, c in bulk_initial],
                    )
                    conn2.commit()

        # One combined commit for all manifest-only refreshes. Runs OUTSIDE
        # the SQLite `with` block so the index is fully committed first —
        # the git commit is the user-visible record; the index is derived.
        if migrations:
            mig_candidates = [c for _, c in migrations]
            mig_commit = commit_manifest_migration(archive, mig_candidates, sign_commits=sign_commits)
            if mig_commit:
                committed.append(
                    f"  migrate  {len(mig_candidates)} manifest(s) → schema v{MANIFEST_VERSION}"
                )
            # Backfill the sessions.git_commit column for the migrated
            # rows so search/cite still know which commit they belong to.
            if mig_commit:
                with open_index(index_path) as conn2:
                    conn2.executemany(
                        "UPDATE sessions SET git_commit = ? WHERE session_id = ?",
                        [(mig_commit, c.session_id) for _, c in migrations],
                    )
                    conn2.commit()

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

        # Auto-push if configured. Runs at end of cycle (one push per
        # ingest, not per session) so a single network round-trip
        # covers everything. Push failure does NOT fail the ingest —
        # local deposits are already committed and a future ingest or
        # manual push will retry.
        if any_changes and auto_push and remote_url and not args.dry_run:
            if not args.quiet:
                print(f"  pushing to {remote_url}...")
            push = run_git(archive, "push", "origin", "HEAD")
            if push.returncode == 0:
                if not args.quiet:
                    print("  push OK")
            else:
                sys.stderr.write(
                    f"holotype: auto-push to {remote_url} failed (deposits are "
                    f"safe locally; retry manually with `git -C {archive} push`).\n"
                )
                if push.stderr:
                    sys.stderr.write(push.stderr)

        return 0 if any_changes else 1
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
