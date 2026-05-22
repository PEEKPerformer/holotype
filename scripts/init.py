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
        "--compression",
        default="auto",
        choices=["auto", "none", "zstd"],
        help=(
            "Per-deposit compression. 'auto' (default) uses zstd if the binary "
            "is on PATH, otherwise stores plain JSONL and records that fact in "
            "the config. 'zstd' forces compression and refuses init if the "
            "binary is missing. 'none' stores plain JSONL. Whichever is "
            "resolved is locked for the life of the archive."
        ),
    )
    p.add_argument(
        "--sign-commits",
        action="store_true",
        help=(
            "GPG-sign every deposit commit (config.deposit.sign_commits=true). "
            "Requires a configured GPG signing key. ingest.py refuses to proceed "
            "if the key isn't available — better to fail than silently produce "
            "unsigned commits in an archive the user thinks is signed."
        ),
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
            ├── .holotype/config.json      # Archive config (version, remote, compression mode)
            ├── README.md                   # This archive's identity
            ├── VERIFY.md                   # This file
            └── sessions/
                └── <project-dir>/<session-id>/
                    ├── transcript.jsonl        # Plain JSONL  (uncompressed archives)
                    │  -OR-
                    ├── transcript.jsonl.zst    # Zstd-compressed JSONL  (compressed archives)
                    ├── manifest.json       # SHA-256, env capture, model IDs, timestamps
                    └── attachments/        # Optional binary attachments
            ```

            Whether the deposit is plain or compressed is recorded per-session in
            `manifest.json` (`"compression": null` or `"compression": "zstd"`)
            and globally in `.holotype/config.json` (`deposit.compression`).

            ## Manifest fields used for verification

            | Field                          | Meaning                                                              |
            |--------------------------------|----------------------------------------------------------------------|
            | `sha256`                       | SHA-256 of the **uncompressed** JSONL bytes (canonical / citation hash) |
            | `sha256_compressed`            | SHA-256 of the on-disk `.jsonl.zst` (only set when compression is on)|
            | `compression`                  | `null` or `"zstd"`                                                   |
            | `manifest_version`             | Schema version (currently 4)                                         |

            ## Reproducibility fields (manifest_version >= 4)

            These let a reviewer reproduce the experiment, not just confirm bytes haven't changed.

            | Field                          | Meaning                                                              |
            |--------------------------------|----------------------------------------------------------------------|
            | `project_git_state`            | `{commit, commit_short, branch, dirty, remote, captured_at}` for the project repo the LLM operated on. `captured_at` is `session-start` (most accurate; sourced from the host CLI's own session header) or `deposit` (probed by holotype at deposit time). `null` if no git repo was detectable. |
            | `wall_clock_seconds`           | `last_timestamp - first_timestamp` in seconds                        |
            | `total_input_tokens`           | Sum of input tokens billed across the session (`null` if the source didn't report) |
            | `total_output_tokens`          | Sum of output tokens billed across the session                       |
            | `total_cache_creation_tokens`  | Sum of cache-creation tokens (Claude Code; absent elsewhere)         |
            | `total_cache_read_tokens`      | Sum of cache-read / cached-input tokens                              |

            To reproduce a session: take the manifest's `project_git_state.commit`, `git checkout` it in the referenced repo, install the recorded host CLI version (from `env.claude_code_version`), and replay against the same tool surface.

            ## Two verification tracks

            Pick the track you can run. Either is sufficient.

            ### Track A — uncompressed hash (requires `zstd` if the deposit is compressed)

            For plain deposits:
            ```bash
            cd <archive>/sessions/<project-dir>/<session-id>/
            recomputed=$(shasum -a 256 transcript.jsonl | awk '{print $1}')
            recorded=$(jq -r '.sha256' manifest.json)
            [ "$recomputed" = "$recorded" ] && echo "OK" || echo "MISMATCH"
            ```

            For compressed deposits (needs `zstd` installed — `brew install zstd` /
            `apt install zstd`):
            ```bash
            cd <archive>/sessions/<project-dir>/<session-id>/
            recomputed=$(zstd -dc transcript.jsonl.zst | shasum -a 256 | awk '{print $1}')
            recorded=$(jq -r '.sha256' manifest.json)
            [ "$recomputed" = "$recorded" ] && echo "OK" || echo "MISMATCH"
            ```

            ### Track B — compressed hash (no zstd needed)

            Only available for compressed deposits. Hashes the `.jsonl.zst` file
            as it sits on disk and compares to `sha256_compressed` in the manifest.

            ```bash
            cd <archive>/sessions/<project-dir>/<session-id>/
            recomputed=$(shasum -a 256 transcript.jsonl.zst | awk '{print $1}')
            recorded=$(jq -r '.sha256_compressed' manifest.json)
            [ "$recomputed" = "$recorded" ] && echo "OK" || echo "MISMATCH"
            ```

            Use Track A when you want to inspect the JSONL content. Use Track B
            when you only need to verify that the bytes on disk haven't been
            tampered with and you don't have `zstd` handy.

            ## Verifying the entire archive

            ```bash
            cd <archive>
            find sessions -name manifest.json | while read m; do
                d=$(dirname "$m")
                comp=$(jq -r '.compression // "none"' "$m")
                if [ -f "$d/transcript.jsonl" ]; then
                    recomputed=$(shasum -a 256 "$d/transcript.jsonl" | awk '{print $1}')
                    recorded=$(jq -r '.sha256' "$m")
                elif [ -f "$d/transcript.jsonl.zst" ] && command -v zstd >/dev/null; then
                    recomputed=$(zstd -dc "$d/transcript.jsonl.zst" | shasum -a 256 | awk '{print $1}')
                    recorded=$(jq -r '.sha256' "$m")
                elif [ -f "$d/transcript.jsonl.zst" ]; then
                    recomputed=$(shasum -a 256 "$d/transcript.jsonl.zst" | awk '{print $1}')
                    recorded=$(jq -r '.sha256_compressed' "$m")
                else
                    echo "MISSING_TRANSCRIPT: $d"
                    continue
                fi
                if [ "$recomputed" != "$recorded" ]; then
                    echo "TAMPER: $d"
                fi
            done
            ```

            ## Inspecting the deposit history

            ```bash
            git -C <archive> log --oneline sessions/
            ```

            Each commit corresponds to one deposit. A clean archive will show
            one commit per session, with deterministic messages of the form:

                deposit: [<source>] sessions/<project-dir>/<session-id>

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


def write_config(
    archive: Path,
    remote_url: str,
    remote_kind: str,
    compression: str,
    sign_commits: bool,
) -> None:
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
            "sign_commits": sign_commits,
            "compression": compression,
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

            # Derived data (SQLite index, decompressed transcript cache).
            # The git-tracked JSONLs + manifests are the source of truth;
            # the items below are rebuilt on demand.
            .holotype/index.sqlite
            .holotype/index.sqlite-*
            .holotype/cache/
            .holotype/.lock
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

    # Resolve --compression auto → concrete mode based on whether the zstd
    # binary is installed. Write the resolved value into config.json so
    # ingest/verify/cite don't have to re-check at every invocation.
    import shutil as _sh
    if args.compression == "auto":
        if _sh.which("zstd"):
            args.compression = "zstd"
            print("init: zstd detected — enabling at-deposit compression")
        else:
            args.compression = "none"
            print(
                "init: zstd not on PATH — falling back to uncompressed deposits.\n"
                "  install zstd later (`brew install zstd` / `apt install zstd`) "
                "to re-init with compression on; the wizard's storage step can "
                "drive that for you."
            )
    elif args.compression == "zstd":
        # Hard mode: refuse init if zstd is missing rather than silently
        # downgrading and ending up with an archive whose user *thought*
        # they were getting compression.
        if _sh.which("zstd") is None:
            print(
                "init: --compression zstd was requested but the `zstd` binary is "
                "not on PATH.\n  install with `brew install zstd` (macOS) or "
                "`apt install zstd` (Debian/Ubuntu), or re-run with "
                "--compression auto or --compression none.",
                file=sys.stderr,
            )
            return 2

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

    if args.sign_commits:
        # Probe for a signing key. Don't fail init outright — the user
        # may want to wire up gpg right after init — but loudly warn.
        probe = subprocess.run(
            ["git", "config", "--get", "user.signingkey"],
            capture_output=True, text=True,
        )
        if not probe.stdout.strip():
            print(
                "init: --sign-commits requested but `git config user.signingkey` "
                "is empty. Set it (e.g. `git config --global user.signingkey "
                "<KEYID>`) before the first ingest, or future deposits will fail.",
                file=sys.stderr,
            )

    write_config(archive, args.remote_url, args.remote_kind, args.compression, args.sign_commits)
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
    print(f"  compression: {args.compression}")
    print(f"  sign commits: {args.sign_commits}")
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
