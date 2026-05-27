#!/usr/bin/env python3
"""Restore env fields lost during the v2.2.0 v4→v5 manifest backfill.

The v5 backfill rebuilt every manifest by calling build_manifest fresh,
which re-probes env at re-process time. Re-probing from a launchd-tick
context can't recover values like ``env.claude_code_version`` that were
captured at the original deposit moment, so those fields came back as
``null`` and overwrote the real values that were correctly captured at
the original deposit.

The data isn't lost forever — every pre-v5 commit in the archive's git
history still has the original env. This script walks each session's
manifest, finds the first commit that wrote that manifest (i.e. the
original deposit), extracts the original env block, and merges it into
the current manifest. Then it makes one combined ``recover:`` commit.

Idempotent: sessions whose current env already carries a non-null
``claude_code_version`` (or wherever the recovery has already run) are
skipped. Sessions that never had a value to recover (e.g. the original
deposit also had ``null`` because the env probe failed then too) are
skipped silently.

Stdlib only.

Usage:
    python scripts/recover_v5_env.py [--archive PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


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


def first_commit_for_path(archive: Path, rel_path: str) -> str | None:
    """Return the SHA of the first commit that introduced `rel_path`."""
    r = subprocess.run(
        ["git", "-C", str(archive), "log",
         "--diff-filter=A", "--reverse", "--format=%H", "--", rel_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    out = r.stdout.strip().split("\n")
    return out[0] if out and out[0] else None


def manifest_at_commit(archive: Path, commit: str, rel_path: str) -> dict | None:
    r = subprocess.run(
        ["git", "-C", str(archive), "show", f"{commit}:{rel_path}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (ValueError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write anything; just report what would change.")
    args = ap.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        return 2

    sessions_root = archive / "sessions"
    if not sessions_root.is_dir():
        sys.stderr.write("no sessions directory\n")
        return 1

    recovered: list[Path] = []
    already_ok: int = 0
    nothing_to_recover: int = 0
    no_history: int = 0

    for manifest_path in sorted(sessions_root.rglob("manifest.json")):
        try:
            current = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue

        current_env = current.get("env") if isinstance(current.get("env"), dict) else {}
        current_cc = current_env.get("claude_code_version")
        if current_cc:
            # Already healthy (either never lost it, or recovery already ran).
            already_ok += 1
            continue

        # Only Claude Code sessions ever had claude_code_version; Codex /
        # Antigravity manifests legitimately have null and shouldn't be
        # "recovered."
        if current.get("source") != "claude-code":
            already_ok += 1
            continue

        rel = manifest_path.relative_to(archive).as_posix()
        first = first_commit_for_path(archive, rel)
        if not first:
            no_history += 1
            continue

        original = manifest_at_commit(archive, first, rel)
        if not original:
            no_history += 1
            continue

        orig_env = original.get("env") if isinstance(original.get("env"), dict) else {}
        orig_cc = orig_env.get("claude_code_version")
        if not orig_cc:
            # The original deposit also lacked it. Genuinely unrecoverable.
            nothing_to_recover += 1
            continue

        # Merge: take the original env wholesale, then preserve any fields
        # the CURRENT env added that the original lacked (e.g. a newer
        # holotype_version added by a later migration — we don't want to
        # rewrite that backwards).
        merged_env = {**orig_env, **{k: v for k, v in current_env.items()
                                     if k not in orig_env or v}}
        # Crucially: claude_code_version comes from orig_env (we just verified
        # current_env has it null), but if orig_env's holotype_version is
        # older than current's, prefer current's. Same for platform if
        # current has it.
        if current_env.get("holotype_version") and orig_env.get("holotype_version"):
            # Prefer the later one (current was set during the v5 build).
            merged_env["holotype_version"] = current_env["holotype_version"]

        if merged_env == current_env:
            already_ok += 1
            continue

        current["env"] = merged_env
        if args.dry_run:
            print(f"  would-recover: {rel}  (cc={orig_cc!r})")
        else:
            manifest_path.write_text(
                json.dumps(current, indent=2, sort_keys=True) + "\n"
            )
        recovered.append(manifest_path)

    print(f"\nrecovered:           {len(recovered)}")
    print(f"already-healthy:     {already_ok}")
    print(f"original-also-null:  {nothing_to_recover}")
    print(f"no-history-found:    {no_history}")

    if args.dry_run or not recovered:
        return 0

    # Stage and commit in one batch.
    rels = [str(p.relative_to(archive)) for p in recovered]
    # `git add` argv has practical limits; chunk if huge.
    CHUNK = 200
    for i in range(0, len(rels), CHUNK):
        r = subprocess.run(
            ["git", "-C", str(archive), "add", "--", *rels[i:i + CHUNK]],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            sys.stderr.write(f"git add failed: {r.stderr}\n")
            return 1
    msg = (
        f"recover: restore env.claude_code_version on {len(recovered)} manifest(s) "
        f"lost in the v2.2.0 v5 backfill\n\n"
        f"The v4→v5 backfill rebuilt manifests via build_manifest, which "
        f"re-probes env at re-process time. From a launchd-tick context the "
        f"claude_code_version probe came back null, overwriting the real "
        f"values captured at original deposit. This commit restores those "
        f"fields by extracting them from the first commit that wrote each "
        f"manifest. Transcript SHA-256s are unchanged.\n"
    )
    r = subprocess.run(
        ["git", "-C", str(archive), "commit", "-m", msg],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(f"git commit failed: {r.stderr}\n")
        return 1
    print(f"\n  committed: {r.stdout.strip().splitlines()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
