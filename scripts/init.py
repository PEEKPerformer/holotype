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
        "--encrypt-transcripts",
        action="store_true",
        help=(
            "Filter transcript.jsonl / transcript.jsonl.zst paths through "
            "git-crypt before push, so the remote stores only encrypted "
            "blobs. Manifests stay plaintext (metadata is still visible to "
            "the remote: session IDs, timestamps, models, project paths). "
            "Requires git-crypt installed, --remote-url set, and "
            "--i-understand-key-loss-means-data-loss to acknowledge that "
            "losing the GPG key makes the data unrecoverable."
        ),
    )
    p.add_argument(
        "--i-understand-key-loss-means-data-loss",
        dest="key_loss_ack",
        action="store_true",
        help=(
            "Acknowledge that with --encrypt-transcripts on, losing the "
            "GPG key means losing access to every encrypted deposit. The "
            "remote and any fresh clone are unrecoverable without the key. "
            "Holotype cannot recover lost data. Required for "
            "--encrypt-transcripts to proceed."
        ),
    )
    push_group = p.add_mutually_exclusive_group()
    push_group.add_argument(
        "--auto-push",
        dest="auto_push",
        action="store_const",
        const=True,
        default=None,
        help=(
            "Push to the configured remote after every successful ingest. "
            "Default when --remote-url is set. Privacy decision happens here "
            "(transcripts WILL be pushed to the remote you configured) — "
            "pair with --remote-url accordingly."
        ),
    )
    push_group.add_argument(
        "--no-auto-push",
        dest="auto_push",
        action="store_const",
        const=False,
        help=(
            "Keep push manual even when a remote is configured. Required when "
            "transcripts may contain pre-publication embargo data or other "
            "material the user wants to gate on per-push review."
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
    auto_push: bool,
    encrypt_transcripts: bool,
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
            # push_policy is "auto" when ingest pushes after every cycle,
            # "manual" when the user prefers per-push review. The actual
            # gate is deposit.auto_push below; this string is informational.
            "push_policy": "auto" if auto_push else "manual",
        },
        "deposit": {
            "commit_strategy": "per-session",
            "sign_commits": sign_commits,
            "compression": compression,
            # When true and a remote is configured, ingest.py runs
            # `git push` after a successful ingest. Privacy decision was
            # made at remote-configuration time (the wizard's explicit
            # warning that transcripts will be pushed); auto_push just
            # honors that decision without forcing the user to remember.
            "auto_push": auto_push,
            # When true, transcripts are filtered through git-crypt
            # before reaching the git object store. Manifests stay
            # plaintext. See HOW_TO_BACK_UP_YOUR_KEY.md inside the
            # archive — losing the key makes deposits unrecoverable.
            "encrypt_transcripts": encrypt_transcripts,
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


_ENCRYPT_DATA_LOSS_BANNER = """\
==================================================================
  ENCRYPTION ENABLED — DATA LOSS RISK ACKNOWLEDGED
==================================================================
  Transcripts will be filtered through git-crypt before push, so the
  remote stores ENCRYPTED BLOBS that cannot be decrypted without
  your GPG key.

  If you lose your GPG key:
    * Fresh clones of this remote CANNOT be read.
    * Any paper citing this archive's session IDs loses access to
      the transcript content backing the citation.
    * Holotype cannot recover lost data.

  Before you depend on this archive for any paper:
    1. Back up your GPG private key to at least TWO locations on
       different media (password manager + offline USB / printed
       paper backup).
    2. Test recovery: in a separate dir, `git clone` the remote and
       `git-crypt unlock` with the backup. If it works, you're safe.
    3. Document who else (lab PI, institutional IT, co-author) has
       access to the key so the archive survives your job change.

  Manifests stay PLAINTEXT on the remote (session IDs, timestamps,
  models, project paths, token counts are visible). Encryption
  covers only the transcript file contents.
==================================================================
"""


def init_archive(args: argparse.Namespace) -> int:
    archive: Path = args.path
    config_path = archive / HOLOTYPE_SUBDIR / CONFIG_FILENAME

    # Resolve --compression auto → concrete mode based on whether the zstd
    # binary is installed. Write the resolved value into config.json so
    # ingest/verify/cite don't have to re-check at every invocation.
    import shutil as _sh

    # --encrypt-transcripts preflight. Three preconditions must hold AT
    # init time before we touch any state — silently degrading any of
    # them would produce an archive whose user thinks transcripts are
    # encrypted when they're not, which is worse than refusing.
    if args.encrypt_transcripts:
        if not args.remote_url:
            print(
                "init: --encrypt-transcripts requires --remote-url. There's "
                "no point encrypting at-push for a local-only archive — the "
                "data never leaves your machine.",
                file=sys.stderr,
            )
            return 2
        if _sh.which("git-crypt") is None:
            print(
                "init: --encrypt-transcripts requires the `git-crypt` binary "
                "on PATH.\n  install with `brew install git-crypt` (macOS) "
                "or `apt install git-crypt` (Debian/Ubuntu), then re-run.",
                file=sys.stderr,
            )
            return 2
        if not args.key_loss_ack:
            sys.stderr.write(_ENCRYPT_DATA_LOSS_BANNER)
            sys.stderr.write(
                "\ninit: refusing to enable encryption without an explicit\n"
                "acknowledgment. Re-run with --i-understand-key-loss-means-data-loss\n"
                "to confirm you have a key-backup plan.\n"
            )
            return 2
        # All preconditions met — print the banner anyway so the user
        # sees it in the init transcript itself.
        sys.stderr.write(_ENCRYPT_DATA_LOSS_BANNER)
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

    # Resolve auto_push default: enabled when a remote is configured (the
    # user already consented to "transcripts on this remote" at wizard
    # step 3), disabled for local-only archives (nothing to push to).
    if args.auto_push is None:
        args.auto_push = bool(args.remote_url)
    if args.auto_push and not args.remote_url:
        print(
            "init: --auto-push requested but --remote-url is empty. "
            "Cannot auto-push without a remote configured.",
            file=sys.stderr,
        )
        return 2

    if args.encrypt_transcripts:
        # Set up git-crypt INSIDE the archive's git repo. This creates
        # .git/git-crypt/keys/default (the symmetric key); a fresh clone
        # needs `git-crypt unlock` against an exported keyfile or a GPG
        # collaborator add. We deliberately don't auto-export the key
        # here — the user must export it themselves to a backup location
        # (the data-loss banner walks them through). Auto-exporting
        # would create a sense of "init handled it for me" that the
        # threat model doesn't support.
        gc = subprocess.run(["git-crypt", "init"], cwd=archive,
                            capture_output=True, text=True)
        if gc.returncode != 0:
            sys.stderr.write(
                f"init: `git-crypt init` failed:\n{gc.stderr}\n"
                "  Aborting before writing any config. No state changed.\n"
            )
            return 2

        # .gitattributes tells git which paths to filter through
        # git-crypt. The patterns match both the plain and compressed
        # transcript filenames the archive may use.
        gitattrs = archive / ".gitattributes"
        gitattrs_content = (
            "# holotype: transcript bytes are filtered through git-crypt\n"
            "# so the remote stores only encrypted blobs. Manifests stay\n"
            "# plaintext (search/cite metadata is still useful on the remote).\n"
            "sessions/**/transcript.jsonl filter=git-crypt diff=git-crypt\n"
            "sessions/**/transcript.jsonl.zst filter=git-crypt diff=git-crypt\n"
        )
        gitattrs.write_text(gitattrs_content)

        # Drop a key-backup how-to into the archive. Survives clones,
        # so a future operator who only has the encrypted clone can
        # at least see the recovery plan the original author committed to.
        (archive / "HOW_TO_BACK_UP_YOUR_KEY.md").write_text(
            dedent(
                """\
                # Backing up the git-crypt key for this archive

                This archive's transcript bytes are encrypted at rest via
                `git-crypt`. The remote stores only encrypted blobs.

                ## What you must back up

                The symmetric key lives at `.git/git-crypt/keys/default`
                inside this archive's local clone. It is NOT in the
                git working tree and NOT in the remote. **It exists
                only on the machine that ran `init.py --encrypt-transcripts`.**

                If you lose this file and don't have a backup, every
                encrypted deposit in this archive becomes unrecoverable.

                ## How to back it up

                Export to a portable keyfile:

                ```bash
                git-crypt export-key /path/to/backup-keyfile.key
                ```

                Then store that file:

                - In a password manager that accepts binary attachments.
                - On an offline USB drive in a secure location.
                - With a trusted collaborator or institutional IT.

                Optionally print it as a paper QR backup for
                disaster-recovery scenarios.

                ## How to test recovery

                Before depending on the archive for any paper:

                ```bash
                # In a scratch directory:
                git clone <remote-url> recovery-test
                cd recovery-test
                git-crypt unlock /path/to/backup-keyfile.key
                cat sessions/*/manifest.json | head     # should be readable plaintext
                head sessions/*/transcript.jsonl        # should be readable plaintext, not garbage
                ```

                If the transcripts decrypt cleanly, your backup is good.

                ## Adding collaborators

                Use `git-crypt add-gpg-user <gpg-key-id>` to grant
                another GPG identity access. Each collaborator can then
                `git-crypt unlock` after cloning.

                ## What does NOT get encrypted

                - `manifest.json` (per-session metadata)
                - `README.md`, `VERIFY.md`, `HOW_TO_BACK_UP_YOUR_KEY.md`
                - The `.holotype/config.json`

                Anyone with read access to the remote can see session
                IDs, timestamps, models, project paths, and token totals.
                Only the transcript file contents are encrypted.
                """
            )
        )

    write_config(
        archive,
        args.remote_url,
        args.remote_kind,
        args.compression,
        args.sign_commits,
        args.auto_push,
        args.encrypt_transcripts,
    )
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
    print(f"  auto-push:   {args.auto_push}")
    print(f"  encrypt:     {args.encrypt_transcripts}")
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
