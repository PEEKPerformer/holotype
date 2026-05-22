#!/usr/bin/env python3
"""Re-emit a too-large bulk-initial commit as N smaller commits and push each.

GitHub's smart-HTTP / SSH push handler rejects any single pack larger
than 2.00 GiB ("fatal: pack exceeds maximum allowed size"). Encrypted
holotype archives hit this on first push because git-crypt's encrypted
blobs are high-entropy and don't benefit from git's pack-zlib delta
compression — the wire pack is essentially the sum of the encrypted
file sizes.

This script automates the recovery surgery:

  1. (default) Pause the launchd background tick so it doesn't make
     concurrent commits while we're rewriting history.
  2. Soft-reset the most recent bulk-initial commit, keeping all
     deposited files on disk.
  3. Bin-pack the deposited session paths into N chunks of comparable
     size (target ~1.5 GiB per chunk by default — well under the 2 GiB
     ceiling). Bin-packing groups paths by their top-level
     `sessions/<project-dir>/` subtree so each project stays together
     in one commit (cleaner `git log` per project).
  4. For each chunk, create a signed commit (inheriting the archive's
     `sign_commits` config) with deterministic message
     `bulk-initial part K/N: ...`.
  5. Push each chunk to origin sequentially.
  6. Resume the launchd tick.

Idempotency: re-running on an archive whose HEAD is no longer a
`bulk-initial:` commit refuses with a clear error. The script is
deliberately conservative — it will not unwind multiple commits or
disturb anything other than a single top-level `bulk-initial:`.

Usage:
    # First-time recovery after auto-push failed with "pack exceeds maximum":
    python scripts/repush_chunked.py

    # Custom chunk size or skip the launchd pause:
    python scripts/repush_chunked.py --max-pack-gib 1.0 --no-pause-launchd

    # Dry-run shows the bin-pack plan without touching anything:
    python scripts/repush_chunked.py --dry-run
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

from holotype.chunking import bin_pack_paths, dir_size_bytes  # noqa: E402


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


def run_git(archive: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(archive), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def head_subject(archive: Path) -> str:
    out = run_git(archive, "log", "-1", "--pretty=%s")
    return out.stdout.strip() if out.returncode == 0 else ""


def launchd_label_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "io.holotype.ingest.plist"


def launchd_loaded() -> bool:
    out = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True
    )
    return "io.holotype.ingest" in out.stdout


def launchd_unload() -> bool:
    p = launchd_label_path()
    if not p.exists():
        return False
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)
    return True


def launchd_load() -> None:
    p = launchd_label_path()
    if p.exists():
        subprocess.run(["launchctl", "load", str(p)], capture_output=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Re-emit a too-large bulk-initial commit as N smaller commits and push each.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument(
        "--max-pack-gib",
        type=float,
        default=1.5,
        help=(
            "Target maximum chunk size in GiB. GitHub rejects packs >2 GiB; "
            "default 1.5 leaves headroom for git's own object metadata. "
            "Lower this if your network is unreliable for multi-GB transfers."
        ),
    )
    p.add_argument(
        "--no-pause-launchd",
        dest="pause_launchd",
        action="store_false",
        default=True,
        help="Don't unload the launchd background tick during the surgery. Risky if the tick fires mid-operation.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the bin-pack plan without modifying anything.",
    )
    p.add_argument(
        "--no-push",
        action="store_true",
        help="Create the chunked commits locally but don't push. Useful when you want to inspect first.",
    )
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        return 2

    head_msg = head_subject(archive)
    if not head_msg.startswith("bulk-initial:"):
        sys.stderr.write(
            f"repush_chunked: HEAD's subject is `{head_msg}`, not a `bulk-initial:` commit.\n"
            f"  Refusing — this script only operates on a single fresh bulk-initial commit.\n"
            f"  If you've made other commits since the bulk-initial, the surgery is more involved\n"
            f"  and should be done manually (see CHANGELOG v1.1.6 / docs/ROADMAP.md).\n"
        )
        return 2

    config = json.loads((archive / ".holotype" / "config.json").read_text())
    deposit_cfg = config.get("deposit") or {}
    sign_commits = bool(deposit_cfg.get("sign_commits"))
    remote_url = ((config.get("remote") or {}).get("url") or "").strip()

    sessions_root = archive / "sessions"
    if not sessions_root.exists():
        sys.stderr.write(f"repush_chunked: no sessions/ dir in archive\n")
        return 2

    project_dirs: list[Path] = []
    for p_dir in sorted(sessions_root.iterdir()):
        if not p_dir.is_dir() or p_dir.name.startswith("."):
            continue
        project_dirs.append(Path("sessions") / p_dir.name)
    if not project_dirs:
        sys.stderr.write("repush_chunked: no project subdirectories under sessions/\n")
        return 2

    target_bytes = int(args.max_pack_gib * (1024 ** 3))
    chunks = bin_pack_paths(project_dirs, archive, target_bytes)

    print(f"repush_chunked: planning {len(chunks)} chunk(s) at ≤{args.max_pack_gib} GiB each")
    for i, chunk in enumerate(chunks, 1):
        total = sum(dir_size_bytes(archive / Path(rel)) for rel in chunk)
        print(f"  chunk {i}/{len(chunks)}: {len(chunk)} project(s), {total / (1024**3):.2f} GiB")

    if args.dry_run:
        print("repush_chunked: --dry-run, nothing changed")
        return 0

    if args.pause_launchd and launchd_loaded():
        print("repush_chunked: pausing launchd background tick")
        launchd_unload()

    try:
        # Soft-reset the bulk-initial commit. Files stay on disk and in
        # the index. We `git reset HEAD` to unstage so we can `git add`
        # per chunk cleanly.
        print(f"repush_chunked: soft-resetting `{head_msg}`")
        out = run_git(archive, "reset", "--soft", "HEAD~1")
        if out.returncode != 0:
            sys.stderr.write(f"reset --soft failed: {out.stderr}\n")
            return 2
        out = run_git(archive, "reset", "HEAD")
        if out.returncode != 0:
            sys.stderr.write(f"reset HEAD (unstage) failed: {out.stderr}\n")
            return 2

        sources = sorted({
            json.loads((archive / rel / sid / "manifest.json").read_text()).get("source", "?")
            for rel in [Path("sessions") / p for p in (archive / "sessions").iterdir() if p.is_dir()]
            for sid in os.listdir(archive / rel) if (archive / rel / sid / "manifest.json").exists()
        })
        sources_tag = "+".join(sources) if sources else "?"

        chunk_commits: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            print(f"repush_chunked: staging chunk {i}/{len(chunks)} ({len(chunk)} project(s))")
            CHUNK_ADD = 300
            for j in range(0, len(chunk), CHUNK_ADD):
                add_out = run_git(archive, "add", *chunk[j:j + CHUNK_ADD])
                if add_out.returncode != 0:
                    sys.stderr.write(f"git add failed on chunk {i} batch {j}: {add_out.stderr}\n")
                    return 2

            commit_msg = (
                f"bulk-initial part {i}/{len(chunks)}: chunked for github 2 GiB pack limit [{sources_tag}]"
            )
            commit_args = ["commit", "-m", commit_msg]
            if sign_commits:
                commit_args.insert(1, "-S")
            commit_out = run_git(archive, *commit_args)
            if commit_out.returncode != 0:
                sys.stderr.write(f"git commit failed on chunk {i}: {commit_out.stderr}\n")
                return 2
            sha = run_git(archive, "rev-parse", "--short", "HEAD").stdout.strip()
            chunk_commits.append(sha)
            print(f"  committed {sha}")

        if args.no_push:
            print("repush_chunked: --no-push, leaving chunks local")
        elif remote_url:
            for i, sha in enumerate(chunk_commits, 1):
                print(f"repush_chunked: pushing chunk {i}/{len(chunk_commits)} ({sha})")
                push_args = ["push"]
                if i == 1:
                    push_args.extend(["-u", "origin", "main"])
                else:
                    push_args.extend(["origin", "HEAD"])
                push_out = subprocess.run(
                    ["git", "-C", str(archive), *push_args],
                    text=True,
                )
                if push_out.returncode != 0:
                    sys.stderr.write(
                        f"push of chunk {i} failed (rc={push_out.returncode}). "
                        f"Local commits are intact; retry manually with "
                        f"`git -C {archive} push`.\n"
                    )
                    return 2
        else:
            print("repush_chunked: no remote configured, skipping push")

        print(f"repush_chunked: done. {len(chunk_commits)} chunk commit(s) on HEAD.")
        return 0

    finally:
        if args.pause_launchd and launchd_label_path().exists():
            print("repush_chunked: resuming launchd background tick")
            launchd_load()


if __name__ == "__main__":
    raise SystemExit(main())
