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

from holotype import ledger
from holotype.archive import iter_all_sessions
from holotype.compression import (
    COMPRESSED_TRANSCRIPT,
    PLAIN_TRANSCRIPT,
    decompress_bytes,
    zstd_available,
)
from holotype.hashing import sha256_bytes, sha256_file


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
    """Yield (session_id, session_dir, manifest_path) for each session,
    walking the archive at arbitrary depth (Claude Code subagents AND
    Codex deposits under sessions/codex/.../)."""
    for sess_dir, _ in iter_all_sessions(archive):
        if prefix and not sess_dir.name.startswith(prefix):
            continue
        yield sess_dir.name, sess_dir, sess_dir / "manifest.json"


def verify_session(sess_dir: Path, manifest: dict) -> dict:
    """Verify a single session, choosing the best track for the deposit.

    Returns a dict with at least ``status`` ('OK' | 'TAMPER' | 'MISSING_HASH'
    | 'NO_ZSTD' | 'NO_TRANSCRIPT' | 'ERROR'). Track choice:

    - Uncompressed deposit: hash ``transcript.jsonl``, compare to
      ``manifest.sha256`` (canonical).
    - Compressed deposit + zstd installed: decompress, hash uncompressed
      bytes, compare to ``manifest.sha256`` (canonical track).
    - Compressed deposit + no zstd: hash ``transcript.jsonl.zst``
      directly, compare to ``manifest.sha256_compressed`` (fallback
      track, valid for reviewers who can't or won't install zstd).
    """
    plain = sess_dir / PLAIN_TRANSCRIPT
    compressed = sess_dir / COMPRESSED_TRANSCRIPT
    recorded_uncompressed = manifest.get("sha256")
    recorded_compressed = manifest.get("sha256_compressed")

    if plain.exists():
        if not recorded_uncompressed:
            return {"status": "MISSING_HASH",
                    "detail": "manifest has no sha256 field"}
        recomputed = sha256_file(plain)
        if recomputed == recorded_uncompressed:
            return {"status": "OK", "track": "uncompressed",
                    "sha256": recomputed}
        return {"status": "TAMPER", "track": "uncompressed",
                "recorded": recorded_uncompressed, "recomputed": recomputed}

    if compressed.exists():
        # Prefer the canonical (uncompressed) track when zstd is available;
        # otherwise fall back to verifying the file as-stored.
        if zstd_available() and recorded_uncompressed:
            try:
                raw = decompress_bytes(compressed.read_bytes())
            except Exception as e:
                return {"status": "ERROR", "detail": f"decompress failed: {e}"}
            recomputed = sha256_bytes(raw)
            if recomputed == recorded_uncompressed:
                return {"status": "OK", "track": "uncompressed-via-zstd",
                        "sha256": recomputed}
            return {"status": "TAMPER", "track": "uncompressed-via-zstd",
                    "recorded": recorded_uncompressed, "recomputed": recomputed}

        if recorded_compressed:
            recomputed = sha256_file(compressed)
            if recomputed == recorded_compressed:
                return {"status": "OK", "track": "compressed",
                        "sha256_compressed": recomputed}
            return {"status": "TAMPER", "track": "compressed",
                    "recorded": recorded_compressed, "recomputed": recomputed}

        return {"status": "NO_ZSTD",
                "detail": "compressed deposit but zstd unavailable and "
                          "manifest has no sha256_compressed"}

    return {"status": "NO_TRANSCRIPT", "detail": "no transcript file in session dir"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify the hash chain of the holotype archive.")
    p.add_argument("session_id", nargs="?", default=None,
                   help="Optional UUID prefix to verify a single session.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-chain", action="store_true",
                   help="Skip the hash-chain (ledger) walk; verify per-file only.")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        return 2

    results: list[dict] = []
    n_pass = 0
    n_fail = 0
    n_missing_hash = 0

    for sid, sess_dir, manifest_path in iter_sessions(archive, args.session_id):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            results.append({"session_id": sid, "status": "ERROR", "detail": f"manifest unreadable: {e}"})
            n_fail += 1
            continue

        outcome = verify_session(sess_dir, manifest)
        outcome["session_id"] = sid
        results.append(outcome)
        status = outcome["status"]
        if status == "OK":
            n_pass += 1
        elif status == "MISSING_HASH":
            n_missing_hash += 1
        else:
            n_fail += 1

    # Whole-archive hash-chain verification. The per-session loop above
    # proves each file matches its OWN manifest — necessary but not
    # sufficient, since a coordinated edit of a transcript + its manifest is
    # internally consistent. The chain proves the SET and ORDER of deposits
    # is intact: nothing inserted, deleted, or substituted. It's a
    # whole-archive property, so it's skipped for a single-session filter and
    # when --no-chain is passed. An absent ledger (a pre-ledger archive that
    # hasn't been ingested since the upgrade) warns rather than fails — the
    # next ingest bootstraps it, or run scripts/build_ledger.py.
    chain = None
    chain_failed = False
    if not args.no_chain and not args.session_id:
        if ledger.ledger_path(archive).exists():
            chain = ledger.verify_chain(archive)
            chain_failed = not chain["ok"]
        else:
            chain = {"absent": True}

    if args.json:
        print(json.dumps({"results": results,
                          "summary": {"pass": n_pass, "fail": n_fail,
                                      "missing_hash": n_missing_hash},
                          "chain": chain}, indent=2))
    else:
        for r in results:
            status = r["status"]
            sid = r["session_id"][:8]
            if status == "OK":
                shown_hash = r.get("sha256") or r.get("sha256_compressed") or ""
                track = r.get("track", "")
                tag = f" via {track}" if track and track != "uncompressed" else ""
                print(f"  OK      {sid}  {shown_hash[:12]}…{tag}")
            elif status == "TAMPER":
                print(f"  TAMPER  {sid}  ({r.get('track','?')} track)")
                print(f"          recorded:   {r['recorded']}")
                print(f"          recomputed: {r['recomputed']}")
            else:
                print(f"  {status:<13s}{sid}  {r.get('detail','')}")
        print()
        total = n_pass + n_fail + n_missing_hash
        print(f"  {n_pass}/{total} sessions verified clean"
              f"{f', {n_fail} tampered' if n_fail else ''}"
              f"{f', {n_missing_hash} with missing hash field' if n_missing_hash else ''}")

        if chain is not None:
            if chain.get("absent"):
                print("  CHAIN   not present (pre-ledger archive — run "
                      "scripts/build_ledger.py or re-ingest to seal it)")
            elif chain["ok"]:
                print(f"  CHAIN OK  {chain['length']} link(s), "
                      f"head {chain['head'][:12]}…")
            else:
                if chain["broken_at"] is not None:
                    print(f"  CHAIN BROKEN at seq {chain['broken_at']} "
                          f"(link does not match its predecessor)")
                if chain["orphans"]:
                    print(f"  CHAIN ORPHANS: {len(chain['orphans'])} on-disk "
                          f"deposit(s) not recorded in the ledger:")
                    for sid in chain["orphans"][:10]:
                        print(f"          {sid}")
                if chain["missing"]:
                    print(f"  CHAIN note: {len(chain['missing'])} ledger "
                          f"entr(ies) have no on-disk session (deletion?).")

    if n_fail > 0 or chain_failed:
        return 1
    if n_pass == 0 and not args.session_id:
        sys.stderr.write("holotype: no sessions to verify\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
