"""Process-pool worker for parallel per-session ingest.

The serialized hot path in v1.x ingest spends most wall time on
per-session work that's CPU-bound and independent across sessions:
read JSONL, hash, compress (zstd subprocess), encrypt-via-git-crypt
(at git add time, not here), build manifest. None of these need the
SQLite + git serialization that the coordinator handles.

This module exposes :func:`process_candidate_worker` — a top-level
function (so it pickles cleanly for ProcessPoolExecutor) that
performs the full ``deposit_one`` work on one candidate and returns
a result dict the coordinator can use to drive SQLite + git
serially in the right order.

Workers stay independent: each one re-imports the Source class by
name (classes themselves don't always pickle across spawn-start
processes), reads its own JSONL, applies live-file safety on its
own, runs zstd in its own subprocess, and writes the manifest +
transcript to disk before returning. Bytes are NOT shipped through
the pipe — only the small result dict.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any


def process_candidate_worker(
    archive_str: str,
    source_name: str,
    candidate_dict: dict,
    *,
    compression: str | None,
    compression_level: str,
) -> dict[str, Any]:
    """Process one deposit candidate inside a worker process.

    Returns a result dict the coordinator drains in order:

      ``status``: one of "new" / "updated" / "skipped-live" /
        "skipped-empty" / "skipped-unchanged" / "error"
      ``session_id``: candidate's session id (echoed for ordering)
      ``transcript_changed``: bool — True only when the on-disk
        transcript bytes actually changed vs prior deposit
      ``archive_subpath``: candidate's archive_subpath (echoed)
      ``source_name``: ditto
      ``parent_session_id``: ditto
      ``project_dir_encoded``: ditto
      ``error``: str | None — exception text if status == "error"

    The candidate is round-tripped through a dict because dataclasses
    with Path fields pickle fine on macOS/Linux but the dict form is
    explicit and platform-neutral.
    """
    # Local imports — workers run in a separate process, no shared state.
    import sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from holotype.sources import source_by_name
    from holotype.sources.base import DepositCandidate

    # Re-hydrate candidate. jsonl_path needs to become a Path again.
    cand = DepositCandidate(
        source_name=candidate_dict["source_name"],
        jsonl_path=_Path(candidate_dict["jsonl_path"]),
        session_id=candidate_dict["session_id"],
        archive_subpath=candidate_dict["archive_subpath"],
        parent_session_id=candidate_dict.get("parent_session_id"),
        project_dir_encoded=candidate_dict.get("project_dir_encoded", ""),
    )
    archive = _Path(archive_str)
    source_cls = source_by_name(source_name)

    # Import deposit_one lazily — it lives in scripts/ingest.py which
    # has argparse at module top, so we use importlib to load it.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ht_ingest_worker", str(repo_root / "scripts" / "ingest.py")
    )
    ingest_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingest_mod)

    try:
        status, session_id, transcript_changed = ingest_mod.deposit_one(
            archive, source_cls, cand,
            compression=compression,
            compression_level=compression_level,
        )
        return {
            "status": status,
            "session_id": session_id,
            "transcript_changed": transcript_changed,
            "archive_subpath": cand.archive_subpath,
            "source_name": cand.source_name,
            "parent_session_id": cand.parent_session_id,
            "project_dir_encoded": cand.project_dir_encoded,
            "jsonl_path": str(cand.jsonl_path),
            "error": None,
        }
    except Exception as e:
        return {
            "status": "error",
            "session_id": cand.session_id,
            "transcript_changed": False,
            "archive_subpath": cand.archive_subpath,
            "source_name": cand.source_name,
            "parent_session_id": cand.parent_session_id,
            "project_dir_encoded": cand.project_dir_encoded,
            "jsonl_path": str(cand.jsonl_path),
            "error": f"{type(e).__name__}: {e}",
        }


def candidate_to_dict(candidate) -> dict:
    """Round-trip a DepositCandidate through a plain dict for picklability."""
    d = asdict(candidate)
    # Path → str for explicit pipe serialization.
    d["jsonl_path"] = str(d["jsonl_path"])
    return d
