#!/usr/bin/env python3
"""Initialize a new holotype archive.

This script is invoked at the END of the conversational setup wizard
(see SKILL.md, Step 0). The wizard collects the user's choices via Claude;
this script then materializes the archive on disk and writes its config.

It is designed to be invoked from the skill with all answers as flags,
or run standalone with the same flags from any shell.

Usage:
    python scripts/init.py \\
        --path ~/Documents/holotype-archive \\
        --remote-url "" \\
        --remote-kind none

    python scripts/init.py \\
        --path ~/Documents/holotype-archive \\
        --remote-url git@github.com:USER/holotype-archive.git \\
        --remote-kind github-private

Refuses to overwrite an existing archive unless --force is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

ARCHIVE_FORMAT_VERSION = 1
HOLOTYPE_SUBDIR = ".holotype"
CONFIG_FILENAME = "config.json"
POINTER_FILE = Path.home() / ".config" / "holotype" / "archive-path"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Initialize a new holotype archive.")
    p.add_argument(
        "--path",
        required=True,
        type=lambda s: Path(os.path.expanduser(s)).resolve(),
        help="Absolute path where the archive will live.",
    )
    p.add_argument(
        "--remote-url",
        default="",
        help='Git remote URL, or "" for local-only. Default: local-only.',
    )
    p.add_argument(
        "--remote-kind",
        default="none",
        choices=["none", "github-private", "self-hosted", "synology", "other"],
        help="Informational label for the remote kind (stored in config).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing archive's config (DANGEROUS).",
    )
    return p.parse_args(argv)


def run(cmd: list[str], cwd: Path | None = None) -> None:
    """Run a command and raise if it fails. Stdout/stderr inherited."""
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}")


def write_verify_md(archive: Path) -> None:
    (archive / "VERIFY.md").write_text(
        dedent(
            """\
            # Verifying this holotype archive (no Claude required)

            This archive is a plain git repository. You can verify its integrity
            using stock Unix tools, independent of the `holotype` skill that
            produced it. The skill is convenience automation; this archive is
            the artifact.

            ## Structure

            ```
            <archive>/
            ├── .holotype/config.json     # Archive config (version, remote, etc.)
            ├── README.md                  # This archive's identity
            ├── VERIFY.md                  # This file
            └── sessions/
                └── <project-dir>/<session-id>/
                    ├── transcript.jsonl   # Raw, byte-for-byte from ~/.claude/projects/
                    ├── manifest.json      # SHA-256, env capture, model IDs, timestamps
                    └── attachments/       # Optional binary attachments
            ```

            ## To verify a single session's transcript hash

            ```bash
            cd <archive>/sessions/<project-dir>/<session-id>/
            recomputed=$(shasum -a 256 transcript.jsonl | awk '{print $1}')
            recorded=$(jq -r '.sha256' manifest.json)
            [ "$recomputed" = "$recorded" ] && echo "OK" || echo "MISMATCH"
            ```

            ## To verify the entire archive

            ```bash
            cd <archive>
            find sessions -name manifest.json | while read m; do
                d=$(dirname "$m")
                recomputed=$(shasum -a 256 "$d/transcript.jsonl" | awk '{print $1}')
                recorded=$(jq -r '.sha256' "$m")
                if [ "$recomputed" != "$recorded" ]; then
                    echo "TAMPER: $d"
                fi
            done
            ```

            ## To inspect the deposit history

            ```bash
            git -C <archive> log --oneline sessions/
            ```

            Each commit corresponds to one deposit. A clean archive will show
            one commit per session, with deterministic messages of the form:

                deposit: <project-dir>/<session-id> (<message-count> msgs, <iso-timestamp>)

            ## What this archive does NOT include

            - Files Claude read or wrote outside its own transcript
            - The state of any remote git repos referenced in the transcripts
            - User secrets that were never visible to Claude
            - Anything stripped by external compaction tools (claude-vault, etc.)

            The transcript is the canonical record of *what Claude saw and did*.
            Reproducing the experiment from this archive requires combining the
            transcript with the corresponding code/data repositories at their
            recorded git HEADs.
            """
        )
    )


def write_archive_readme(archive: Path) -> None:
    (archive / "README.md").write_text(
        dedent(
            f"""\
            # holotype archive

            This is a [holotype](https://github.com/PEEKPerformer/holotype) archive — a
            content-addressable, hash-chained git repository of Claude Code session
            transcripts, intended for scientific reproducibility.

            - Created: {datetime.now(timezone.utc).isoformat(timespec='seconds')}
            - Archive format version: {ARCHIVE_FORMAT_VERSION}
            - Verify: see [VERIFY.md](VERIFY.md) for standalone verification (no Claude needed).

            The skill that maintains this archive lives at `~/Git/holotype/`. Operations
            on the archive go through the skill or its bundled scripts. The archive
            itself is plain git; nothing here requires the skill to read.
            """
        )
    )


def write_config(archive: Path, remote_url: str, remote_kind: str) -> None:
    config = {
        "archive_format_version": ARCHIVE_FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": {
            "preferred": ["~/Documents/Claude-Backups"],
            "fallback": ["~/.claude/projects"],
        },
        "remote": {
            "url": remote_url,
            "kind": remote_kind,
            "push_policy": "manual",
        },
        "deposit": {
            "commit_strategy": "per-session",
            "sign_commits": False,
        },
        "verification": {
            "hash_algorithm": "sha256",
        },
    }
    cfg_dir = archive / HOLOTYPE_SUBDIR
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / CONFIG_FILENAME).write_text(json.dumps(config, indent=2) + "\n")


def write_archive_gitignore(archive: Path) -> None:
    (archive / ".gitignore").write_text(
        dedent(
            """\
            # macOS
            .DS_Store

            # Editor temp files
            *~
            *.swp
            """
        )
    )


def write_pointer(archive: Path) -> None:
    """Write the global archive pointer.

    Honored only when HOLOTYPE_NO_POINTER is unset. Tests and selftest set
    HOLOTYPE_NO_POINTER=1 so they don't clobber the user's real pointer.
    """
    if os.environ.get("HOLOTYPE_NO_POINTER"):
        return
    POINTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    POINTER_FILE.write_text(str(archive) + "\n")


def init_archive(args: argparse.Namespace) -> int:
    archive: Path = args.path
    config_path = archive / HOLOTYPE_SUBDIR / CONFIG_FILENAME

    if archive.exists() and any(archive.iterdir()):
        if config_path.exists() and not args.force:
            print(
                f"refusing to overwrite existing archive at {archive}\n"
                "  pass --force to overwrite the config (the existing data is left intact).",
                file=sys.stderr,
            )
            return 1
        if not config_path.exists():
            print(
                f"warning: {archive} exists and is non-empty but has no holotype config.",
                file=sys.stderr,
            )

    archive.mkdir(parents=True, exist_ok=True)

    is_new_repo = not (archive / ".git").exists()
    if is_new_repo:
        run(["git", "init", "-b", "main"], cwd=archive)

    write_config(archive, args.remote_url, args.remote_kind)
    write_archive_readme(archive)
    write_verify_md(archive)
    write_archive_gitignore(archive)
    (archive / "sessions").mkdir(exist_ok=True)

    run(["git", "add", "."], cwd=archive)

    if is_new_repo:
        commit_msg = f"init: holotype archive (format v{ARCHIVE_FORMAT_VERSION})"
    else:
        commit_msg = f"init: holotype config (format v{ARCHIVE_FORMAT_VERSION})"

    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=archive
    )
    if status.returncode != 0:
        run(["git", "commit", "-m", commit_msg], cwd=archive)

    if args.remote_url:
        existing = subprocess.run(
            ["git", "-C", str(archive), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
        if existing.returncode == 0:
            current = existing.stdout.strip()
            if current != args.remote_url:
                print(f"updating origin: {current} -> {args.remote_url}")
                run(["git", "-C", str(archive), "remote", "set-url", "origin", args.remote_url])
        else:
            run(["git", "-C", str(archive), "remote", "add", "origin", args.remote_url])

    write_pointer(archive)

    print()
    print(f"  archive:     {archive}")
    print(f"  config:      {config_path}")
    print(f"  pointer:     {POINTER_FILE}")
    print(f"  remote:      {args.remote_url or '(none — local only)'}")
    print(f"  remote kind: {args.remote_kind}")
    print()
    print("Next steps:")
    print("  - To deposit sessions:  python scripts/ingest.py")
    if args.remote_url:
        print("  - To push to remote:    git -C", archive, "push -u origin main")
        print("    (only when you explicitly want to publish — never auto-pushed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return init_archive(args)


if __name__ == "__main__":
    raise SystemExit(main())
