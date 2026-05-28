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
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype import __version__
from holotype.chunking import bin_pack_paths, dir_size_bytes
from holotype.compression import compress_bytes, find_transcript, transcript_filename
from holotype.env import claude_code_version, git_state_for_path, platform_info
from holotype.hashing import sha256_bytes
from holotype.index import (
    bulk_insert_fts_rows,
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


# --- Per-tick change detection + rolling content re-verification ----------
#
# The expensive part of an idle tick used to be reading + SHA-256ing every
# source transcript just to discover nothing changed. needs_processing()
# replaces that with a stat: append-only logs only grow, so a matching
# (size, mtime_ns) against the stored manifest values means the bytes are
# unchanged and the read can be skipped.
#
# mtime is a heuristic, not a guarantee (a tool could rewrite content while
# preserving mtime). So we DON'T trust it as the sole oracle: every tick
# also force-re-hashes a deterministic 1/SWEEP_BUCKETS slice of the corpus
# (the "rolling sweep"), cycling through all buckets via a persisted cursor.
# At the default 30-min cadence that's full content re-verification of the
# whole archive every ~24h, independent of mtime — the soundness backstop.
# The same slice is fsck'd (verify_deposit_integrity) against its manifest,
# giving continuous archive bit-rot detection the tool otherwise lacked.
SWEEP_BUCKETS = 48
_SWEEP_CURSOR_REL = Path(".holotype") / "sweep-cursor"


def sweep_bucket_for(session_id: str) -> int:
    """Stable bucket assignment for a session, in [0, SWEEP_BUCKETS).

    Hash-based so the partition is balanced and independent of discovery
    order; deterministic so a session lands in the same bucket every tick.
    """
    h = int(hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8], 16)
    return h % SWEEP_BUCKETS


def read_sweep_bucket(archive: Path) -> int:
    """Current sweep bucket from the persisted cursor (0 if absent/corrupt)."""
    try:
        return int((archive / _SWEEP_CURSOR_REL).read_text().strip()) % SWEEP_BUCKETS
    except (OSError, ValueError):
        return 0


def advance_sweep_cursor(archive: Path, current: int) -> None:
    """Advance the cursor by one bucket (atomic write).

    Advancing by exactly one per tick guarantees every bucket is visited
    once per SWEEP_BUCKETS ticks regardless of tick cadence or downtime —
    no session is ever permanently skipped by the sweep.
    """
    p = archive / _SWEEP_CURSOR_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".partial")
    tmp.write_text(str((current + 1) % SWEEP_BUCKETS))
    os.replace(tmp, p)


def needs_processing(
    archive: Path, candidate: DepositCandidate, *, in_sweep: bool
) -> bool:
    """Cheap pre-dispatch gate: should this candidate be read + hashed now?

    True when the file is new, the manifest schema is stale (forces a
    re-deposit so new fields backfill), the session is in this tick's
    rolling-sweep slice, or the source's (size, mtime_ns) differ from the
    stored values. False only when the manifest is current-version, the
    stat matches, and it's not a sweep tick — i.e. provably unchanged for
    an append-only log. The content-hash short-circuit inside deposit_one
    remains as a backstop for anything that slips through.
    """
    prior = existing_manifest(archive, candidate)
    if prior is None:
        return True
    if prior.get("manifest_version") != MANIFEST_VERSION:
        return True
    if in_sweep:
        return True
    stored_size = prior.get("source_size")
    stored_mtime = prior.get("source_mtime_ns")
    if stored_size is None or stored_mtime is None:
        return True  # pre-v6 baseline not yet recorded — read to establish it
    try:
        st = candidate.jsonl_path.stat()
    except OSError:
        return False  # source vanished; the prior deposit stands untouched
    return st.st_size != stored_size or st.st_mtime_ns != stored_mtime


def verify_deposit_integrity(
    archive: Path, candidate: DepositCandidate
) -> str | None:
    """fsck one deposited transcript against its manifest hash.

    Hashes the on-disk deposit bytes as they sit (compressed bytes vs
    ``sha256_compressed`` when zstd, raw bytes vs ``sha256`` otherwise) —
    pure bit-rot detection that needs no zstd and never false-alarms.
    Returns an error string on mismatch/missing, else None. Run on the
    rolling-sweep slice so the whole archive is re-checked every ~24h.
    """
    m = existing_manifest(archive, candidate)
    if not m:
        return None
    sess_dir = session_archive_dir(archive, candidate)
    found = find_transcript(sess_dir)
    if found is None:
        return f"{candidate.archive_subpath}: deposited transcript missing"
    path, is_compressed = found
    expected = m.get("sha256_compressed") if is_compressed else m.get("sha256")
    if not expected:
        return None  # nothing stored to check against (e.g. legacy manifest)
    try:
        raw = path.read_bytes()
    except OSError as e:
        return f"{candidate.archive_subpath}: deposited transcript unreadable ({e})"
    if sha256_bytes(raw) != expected:
        return (
            f"{candidate.archive_subpath}: ARCHIVE INTEGRITY FAILURE — "
            f"stored bytes do not match manifest hash"
        )
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
    source_cls: type[Source],
    candidate: DepositCandidate,
    *,
    compression: str | None,
    compression_level: str = "archival",
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

    # Source size + mtime at the moment we read it, recorded in the
    # manifest so the next tick's pre-filter can skip the read+hash when
    # nothing changed. read_with_stable_check already guaranteed the file
    # didn't move during the read; this stat matches those bytes.
    try:
        _src_stat = jsonl_path.stat()
        src_size: int | None = _src_stat.st_size
        src_mtime_ns: int | None = _src_stat.st_mtime_ns
    except OSError:
        src_size = src_mtime_ns = None

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

    # Pure manifest-version migration: canonical transcript bytes are
    # unchanged, only the schema bumped. Do NOT re-write the transcript.
    # Re-compressing produces cosmetically-different bytes (zstd output is
    # not stable across compression levels — an archive bulk-ingested with
    # --fast-compress then migrated at archival level differs byte-for-byte
    # — and git-crypt re-encrypts on re-stage), which would turn a
    # manifest-only schema bump into a full-corpus re-commit + re-push
    # (~2 GiB on a real archive, over GitHub's 2 GiB pack ceiling). Reuse
    # the existing on-disk file and its recorded compressed hash. Falls
    # through to a real write only if the file is missing or the
    # compression mode changed (then dest_transcript wouldn't exist).
    is_pure_migration = (
        prior_manifest is not None
        and prior_sha == new_sha
        and prior_version != MANIFEST_VERSION
    )

    dest_dir.mkdir(parents=True, exist_ok=True)

    if is_pure_migration and dest_transcript.exists():
        if compression == "zstd":
            sha_compressed = prior_manifest.get("sha256_compressed") or sha256_bytes(
                dest_transcript.read_bytes()
            )
        else:
            sha_compressed = None
    else:
        if compression == "zstd":
            payload = compress_bytes(data, level=compression_level)
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

    # For a pure schema migration (is_pure_migration, computed above),
    # preserve fields captured at the original deposit time that CANNOT be
    # reconstructed by re-deriving from the bytes:
    #   - env (especially env.claude_code_version, which reads as null from
    #     a launchd-tick context but had a real value at the original
    #     deposit moment)
    #   - deposited_at (the manifest was deposited THEN, not now)
    # Real content updates (sha differs) re-capture env fresh, since the
    # new env corresponds to the moment of the update.
    if is_pure_migration and isinstance(prior_manifest.get("env"), dict) and prior_manifest["env"]:
        env = dict(prior_manifest["env"])
        original_deposited_at = prior_manifest.get("deposited_at")
    else:
        env = {
            "holotype_version": __version__,
            "claude_code_version": claude_code_version(),
            "platform": platform_info(),
        }
        original_deposited_at = None

    # Probe the project's git state at deposit time, using the cwd the
    # host CLI recorded (decoded from the project_dir_encoded string).
    # If the source itself recorded a session-start git snapshot (e.g.
    # Codex's session_meta), build_manifest will prefer that — the
    # deposit-time probe is the fallback. For pure migrations, prefer the
    # prior probe (more accurate to original deposit time) over re-probing.
    deposit_time_git = None
    if is_pure_migration and isinstance(prior_manifest.get("project_git_state"), dict):
        deposit_time_git = prior_manifest["project_git_state"]
    else:
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
        original_deposited_at=original_deposited_at,
        source_size=src_size,
        source_mtime_ns=src_mtime_ns,
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
    """Commit one session's deposit files.

    Adds the manifest + transcript by *explicit path*, not by directory.
    This matters under the v2.0 parallel-worker path: workers write
    files concurrently and a subagent's files can land on disk before
    its parent session's commit_deposit runs. A directory-level
    `git add sessions/<parent>/` would then sweep the subagent files
    into the parent's commit, leaving nothing for the subagent's own
    commit. Explicit per-file add isolates each session's commit to
    its own files.
    """
    rel = f"sessions/{candidate.archive_subpath}"
    sess_dir = archive / "sessions" / candidate.archive_subpath
    # Build an explicit path list. The transcript may be .jsonl or
    # .jsonl.zst depending on the archive's compression mode; glob
    # picks up whichever exists.
    paths_to_add = [f"{rel}/manifest.json"]
    for tf in ("transcript.jsonl", "transcript.jsonl.zst"):
        if (sess_dir / tf).exists():
            paths_to_add.append(f"{rel}/{tf}")
    run_git(archive, "add", *paths_to_add)
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
    *,
    out_fts_rows: list | None = None,
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

    reindex_session(
        conn, candidate.session_id, sess_dir, source_cls=source_cls,
        out_fts_rows=out_fts_rows,
    )


def _parallel_worker_unpack(args_tuple):
    """Module-level adapter so ProcessPoolExecutor.map can call the worker.

    map() only passes one argument per call; we pack the worker's
    fixed-config + per-task tuple here so each pool task can unpack
    it cleanly. Lives at module top so it's picklable.
    """
    archive_str, source_name, candidate_dict, compression, compression_level = args_tuple
    from holotype.parallel import process_candidate_worker
    return process_candidate_worker(
        archive_str, source_name, candidate_dict,
        compression=compression, compression_level=compression_level,
    )


def _drain_result(
    result: dict,
    source_cls: type[Source],
    candidate: DepositCandidate,
    conn,
    archive: Path,
    counts: dict,
    committed: list,
    bulk_initial: list,
    migrations: list,
    bulk_fts_rows: list,
    sign_commits: bool,
    is_bulk_initial: bool,
) -> None:
    """Consume one worker result and drive the SQLite + git serial path.

    Identical bucketing to the serial path; lives separately because
    ProcessPoolExecutor.map yields plain dicts back, and we want one
    place that knows how to translate those into bucket updates and
    per-session commits.
    """
    status = result["status"]
    counts[status] = counts.get(status, 0) + 1

    if status == "error":
        sys.stderr.write(
            f"holotype: worker failed on {candidate.archive_subpath}: "
            f"{result.get('error')}\n"
        )
        return

    if status not in ("new", "updated"):
        return

    transcript_changed = result["transcript_changed"]

    if transcript_changed:
        if is_bulk_initial:
            update_index(
                conn, archive, source_cls, candidate, git_commit=None,
                transcript_changed=True,
                out_fts_rows=bulk_fts_rows,
            )
            bulk_initial.append((source_cls, candidate))
        else:
            commit = commit_deposit(archive, candidate, status, sign_commits=sign_commits)
            update_index(
                conn, archive, source_cls, candidate, commit,
                transcript_changed=True,
            )
            committed.append(f"  {status:>8s}  [{source_cls.name}] {candidate.archive_subpath}")
    else:
        update_index(
            conn, archive, source_cls, candidate, git_commit=None,
            transcript_changed=False,
        )
        migrations.append((source_cls, candidate))


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
            "unaffected. Auto-chunks into multiple commits if the "
            "projected push pack would exceed --max-pack-gib (default "
            "1.5 GiB, under GitHub's 2 GiB ceiling). FTS index inserts "
            "are deferred to a single bulk INSERT + 'optimize' at end."
        ),
    )
    p.add_argument(
        "--fast-compress",
        action="store_true",
        help=(
            "Use zstd -3 instead of the archival -19 --long=27. ~3-5x "
            "faster compression at a modest ratio cost (<10% larger "
            "files typically). Intended for use with --bulk-initial "
            "where wall time matters more than the last few percent "
            "of ratio. Per-deposit decision; doesn't affect verification "
            "(decompression is identical regardless of encoding level)."
        ),
    )
    p.add_argument(
        "--max-pack-gib",
        type=float,
        default=1.5,
        help=(
            "Maximum target chunk size in GiB for auto-chunking under "
            "--bulk-initial (default 1.5). GitHub rejects packs >2 GiB; "
            "1.5 leaves headroom for git's own object metadata. Ignored "
            "without --bulk-initial."
        ),
    )
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        help=(
            "Number of parallel deposit workers (default 0 = auto, "
            "min(os.cpu_count(), 8)). 1 forces serial mode (useful for "
            "debugging). Each worker is an independent process doing "
            "the per-session hash + compress + manifest-build + "
            "transcript-write. The coordinator serializes SQLite + git "
            "writes. Workers don't share state with each other or with "
            "the coordinator — they each re-open zstd subprocesses, "
            "re-import Source classes, and apply live-file safety on "
            "their own slice."
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

        # Per-tick pre-filter: stat each candidate against its stored
        # (size, mtime_ns) and skip the read+hash when unchanged. Anything
        # in this tick's rolling-sweep slice is forced through regardless,
        # for unconditional content re-verification + deposit fsck. Dry-run
        # inspects every candidate and mutates no state.
        sweep_bucket = read_sweep_bucket(archive)
        sweep_candidates: list[tuple[type[Source], DepositCandidate]] = []
        if args.dry_run:
            to_process = candidates
        else:
            to_process = []
            for cls, cand in candidates:
                in_sweep = sweep_bucket_for(cand.session_id) == sweep_bucket
                if in_sweep:
                    sweep_candidates.append((cls, cand))
                if needs_processing(archive, cand, in_sweep=in_sweep):
                    to_process.append((cls, cand))
                else:
                    counts["skipped-unchanged"] += 1

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
        #   commits. Auto-chunked at end if the projected pack exceeds
        #   --max-pack-gib.
        # - per-session committed: when --bulk-initial is OFF, genuine
        #   content changes get one commit each via commit_deposit().
        migrations: list[tuple[type[Source], DepositCandidate]] = []
        bulk_initial: list[tuple[type[Source], DepositCandidate]] = []

        # FTS-row buffer for --bulk-initial. Sessions accumulate FTS
        # rows here instead of inserting per-session; a single
        # bulk_insert_fts_rows() call at end-of-cycle amortizes FTS5
        # segment merges across the whole batch.
        bulk_fts_rows: list[tuple] = []
        compression_level = "fast" if args.fast_compress else "archival"

        # Resolve worker count. 0 = auto. 1 = explicit serial mode.
        # Anything > 1 runs the parallel coordinator path.
        if args.workers == 0:
            import os as _os
            args.workers = min(_os.cpu_count() or 4, 8)
        if args.dry_run:
            args.workers = 1  # dry-run skips the work, no point parallelizing

        with open_index(index_path) as conn:
            if args.workers > 1 and not args.dry_run:
                # Parallel path: dispatch all candidates to a worker
                # pool. Workers write files + manifests to disk and
                # return results; coordinator drains in order and runs
                # the (single-writer) SQLite + git path serially.
                from concurrent.futures import ProcessPoolExecutor
                from holotype.parallel import (
                    candidate_to_dict,
                    process_candidate_worker,
                )

                if not args.quiet:
                    print(f"holotype ingest: {len(to_process)} candidate(s) across {args.workers} worker(s)")

                # Submit in order; results stream back in same order via
                # executor.map.
                cls_by_name = {cls.name: cls for cls, _ in to_process}
                submissions = [
                    (
                        str(archive),
                        cls.name,
                        candidate_to_dict(cand),
                    )
                    for cls, cand in to_process
                ]

                executor = ProcessPoolExecutor(max_workers=args.workers)
                try:
                    results_iter = executor.map(
                        _parallel_worker_unpack,
                        [
                            (s[0], s[1], s[2], compression, compression_level)
                            for s in submissions
                        ],
                    )
                    for (cls, candidate), result in zip(to_process, results_iter):
                        _drain_result(
                            result, cls, candidate, conn, archive,
                            counts, committed, bulk_initial, migrations,
                            bulk_fts_rows, sign_commits, args.bulk_initial,
                        )
                        # SQLite commit cadence — same as serial path.
                        if result["status"] in ("new", "updated"):
                            pending_since_commit += 1
                            if pending_since_commit >= INDEX_COMMIT_BATCH:
                                conn.commit()
                                pending_since_commit = 0
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
            else:
                # Serial path: unchanged from v1.2 / earlier.
                for source_cls, candidate in to_process:
                    if args.dry_run:
                        counts["new"] += 1
                        continue

                    status, session_id, transcript_changed = deposit_one(
                        archive, source_cls, candidate, compression=compression,
                        compression_level=compression_level,
                    )
                    counts[status] = counts.get(status, 0) + 1

                    if status not in ("new", "updated"):
                        continue

                    if transcript_changed:
                        if args.bulk_initial:
                            update_index(
                                conn, archive, source_cls, candidate, git_commit=None,
                                transcript_changed=True,
                                out_fts_rows=bulk_fts_rows,
                            )
                            bulk_initial.append((source_cls, candidate))
                        else:
                            commit = commit_deposit(archive, candidate, status, sign_commits=sign_commits)
                            update_index(
                                conn, archive, source_cls, candidate, commit,
                                transcript_changed=True,
                            )
                            committed.append(f"  {status:>8s}  [{source_cls.name}] {candidate.archive_subpath}")
                    else:
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

        # Bulk-initial commit(s). When the projected pack would exceed
        # --max-pack-gib, auto-chunk by project directory into N
        # commits — same shape as repush_chunked.py but proactive.
        # Single commit when total fits comfortably.
        #
        # When auto_push is on AND we chunked, push each chunk
        # IMMEDIATELY after committing it. Bundling all chunks into one
        # end-of-cycle `git push origin HEAD` defeats the chunking —
        # `pack-objects` builds a single pack from all unpushed commits,
        # so the wire pack size is the SUM of the local chunks, which
        # is exactly the case the chunking was supposed to prevent.
        # Per-chunk push keeps each push payload ≤ --max-pack-gib.
        bulk_chunks_pushed = False
        if bulk_initial:
            bi_candidates = [c for _, c in bulk_initial]
            target_bytes = int(args.max_pack_gib * (1024 ** 3))
            total_bytes = sum(
                dir_size_bytes(session_archive_dir(archive, c)) for c in bi_candidates
            )
            bi_sha_per_session: dict[str, str] = {}

            if total_bytes <= target_bytes:
                # Fits in one commit — existing behavior.
                bi_commit = commit_bulk_initial(archive, bi_candidates, sign_commits=sign_commits)
                if bi_commit:
                    committed.append(
                        f"  bulk-initial  {len(bi_candidates)} session(s) ingested "
                        f"({total_bytes / (1024**3):.2f} GiB)"
                    )
                    for c in bi_candidates:
                        bi_sha_per_session[c.session_id] = bi_commit
            else:
                # Bin-pack by project dir to fit under target_bytes per chunk.
                # Collect unique sessions/<project>/ subtrees touched by
                # this batch, then bin-pack into chunks. Each chunk
                # commit covers all candidates whose project dir is in it.
                project_dirs = sorted({
                    Path("sessions") / c.archive_subpath.split("/", 1)[0]
                    for c in bi_candidates
                })
                chunks = bin_pack_paths(project_dirs, archive, target_bytes)
                if not args.quiet:
                    print(
                        f"  bulk-initial: {total_bytes / (1024**3):.2f} GiB exceeds "
                        f"{args.max_pack_gib} GiB target; auto-chunking into "
                        f"{len(chunks)} commit(s)"
                    )
                sources = sorted({c.source_name for c in bi_candidates})
                src_tag = "+".join(sources) if sources else "?"

                # Map each candidate to its chunk by project dir prefix.
                chunk_for_project: dict[str, int] = {}
                for ci, chunk in enumerate(chunks):
                    for rel in chunk:
                        chunk_for_project[Path(rel).name] = ci

                will_push_per_chunk = auto_push and bool(remote_url) and not args.dry_run

                for ci, chunk in enumerate(chunks, 1):
                    CHUNK_ADD = 500
                    for j in range(0, len(chunk), CHUNK_ADD):
                        run_git(archive, "add", *chunk[j:j + CHUNK_ADD])
                    diff = run_git(archive, "diff", "--cached", "--quiet")
                    if diff.returncode == 0:
                        continue
                    msg = (
                        f"bulk-initial part {ci}/{len(chunks)}: chunked at "
                        f"{args.max_pack_gib} GiB target [{src_tag}]"
                    )
                    commit_args = ["commit", "-m", msg]
                    if sign_commits:
                        commit_args.insert(1, "-S")
                    out = run_git(archive, *commit_args)
                    if out.returncode != 0:
                        sys.stderr.write(out.stderr)
                        continue
                    sha = git_head_short(archive)
                    if sha:
                        for c in bi_candidates:
                            proj = c.archive_subpath.split("/", 1)[0]
                            if chunk_for_project.get(proj) == ci - 1:
                                bi_sha_per_session[c.session_id] = sha
                        committed.append(
                            f"  bulk-initial part {ci}/{len(chunks)}: {sha}"
                        )

                    if will_push_per_chunk:
                        if not args.quiet:
                            print(f"  pushing chunk {ci}/{len(chunks)} to {remote_url}...")
                        # First chunk creates/upgrades the tracking
                        # branch; subsequent chunks fast-forward.
                        push_args = ["push", "-u", "origin", "main"] if ci == 1 else ["push", "origin", "HEAD"]
                        push = run_git(archive, *push_args)
                        if push.returncode == 0:
                            if not args.quiet:
                                print(f"  push chunk {ci}/{len(chunks)} OK")
                            bulk_chunks_pushed = True
                        else:
                            sys.stderr.write(
                                f"holotype: auto-push of chunk {ci}/{len(chunks)} to "
                                f"{remote_url} failed (deposits safe locally; rerun ingest "
                                f"or use `scripts/repush_chunked.py` to retry).\n"
                            )
                            if push.stderr:
                                sys.stderr.write(push.stderr)
                            # Stop pushing further chunks — subsequent
                            # ones would fail until this one lands.
                            will_push_per_chunk = False

            # Backfill sessions.git_commit + bulk-insert FTS rows in
            # one transaction.
            if bi_sha_per_session or bulk_fts_rows:
                with open_index(index_path) as conn2:
                    if bi_sha_per_session:
                        conn2.executemany(
                            "UPDATE sessions SET git_commit = ? WHERE session_id = ?",
                            [(sha, sid) for sid, sha in bi_sha_per_session.items()],
                        )
                    if bulk_fts_rows:
                        n_fts = bulk_insert_fts_rows(conn2, bulk_fts_rows)
                        if not args.quiet:
                            print(f"  bulk-initial: indexed {n_fts} FTS row(s)")
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

        # Rolling-sweep fsck: re-hash this tick's deposit slice against its
        # manifest to catch archive bit-rot. Runs every tick (even idle
        # ones) so the whole archive is re-verified each sweep cycle.
        # Failures are reported loudly and sent to stderr — they indicate
        # the deposited artifact no longer matches what was recorded.
        integrity_failures: list[str] = []
        if not args.dry_run:
            for _cls, cand in sweep_candidates:
                err = verify_deposit_integrity(archive, cand)
                if err:
                    integrity_failures.append(err)
            advance_sweep_cursor(archive, sweep_bucket)
        if integrity_failures:
            sys.stderr.write(
                f"holotype: {len(integrity_failures)} ARCHIVE INTEGRITY "
                f"FAILURE(S) during rolling sweep:\n"
            )
            for f in integrity_failures:
                sys.stderr.write(f"  {f}\n")

        any_changes = counts["new"] + counts["updated"] > 0
        if not args.quiet or any_changes or integrity_failures:
            print(
                f"holotype ingest: new={counts['new']} updated={counts['updated']}"
                f" skipped-live={counts['skipped-live']}"
                f" unchanged={counts['skipped-unchanged']}"
                f" empty={counts['skipped-empty']}"
                f" swept={len(sweep_candidates)}"
                + (f" INTEGRITY-FAIL={len(integrity_failures)}" if integrity_failures else "")
            )
            if committed and not args.quiet:
                print("\n".join(committed))

        # Auto-push if configured. Runs at end of cycle (one push per
        # ingest, not per session) so a single network round-trip
        # covers everything. Push failure does NOT fail the ingest —
        # local deposits are already committed and a future ingest or
        # manual push will retry.
        #
        # Skip if bulk-initial chunking already pushed per chunk —
        # otherwise `pack-objects` would bundle ALL local commits into
        # one pack (HEAD includes all unpushed commits), defeating the
        # chunking. A no-op `git push origin HEAD` would be harmless
        # but noisy; skip it explicitly.
        if any_changes and auto_push and remote_url and not args.dry_run and not bulk_chunks_pushed:
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

        return 0
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
