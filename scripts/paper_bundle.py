#!/usr/bin/env python3
"""Bundle multiple sessions into a single Zenodo-ready deposit.

The single-session ``cite.py`` is great for inspecting one session;
papers cite many. This script accepts a list of session ID prefixes
and produces a directory containing:

  - one subdir per session with plain ``transcript.jsonl`` +
    ``manifest.json`` + ``cite.txt`` + ``view.html``
  - a top-level ``BUNDLE_MANIFEST.json`` listing every session with
    its canonical SHA-256, model IDs, message count, etc.
  - a top-level ``index.html`` linking to each session's ``view.html``
  - a top-level ``VERIFY.md`` describing how a reviewer hashes each
    transcript and compares against the BUNDLE_MANIFEST
  - optionally, a ``<bundle>.tar.gz`` and ``<bundle>.tar.gz.sha256``
    sidecar so the whole bundle has a single citation hash

Usage:
    python scripts/paper_bundle.py --sessions 3f1c4cf7,a8b2,deadbeef \\
        --out ~/Desktop/zenodo-v200/
    python scripts/paper_bundle.py --sessions-file paper-cites.txt \\
        --out ~/Desktop/zenodo-v200/ --tarball
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.archive import resolve_session_by_prefix
from holotype.compression import read_transcript_bytes
from holotype.viewer import render_index, render_session


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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_commit(archive: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(archive), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else "?"


def parse_session_list(args: argparse.Namespace) -> list[str]:
    items: list[str] = []
    if args.sessions:
        items.extend([s.strip() for s in args.sessions.split(",") if s.strip()])
    if args.sessions_file:
        text = Path(args.sessions_file).expanduser().read_text()
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                items.append(line)
    return items


def write_reviewer_readme(out_dir: Path, bundle_manifest: dict) -> None:
    """Write README.md at the bundle root, oriented toward a journal reviewer.

    Frames the deposit in the language of the journal reproducibility policy
    (RSC Digital Discovery and similar): "log files including the inputs and
    outputs", "model identifier", "generation date". A reviewer who's never
    seen holotype should be able to read this and know what to open first.
    """
    sessions = bundle_manifest.get("sessions") or []
    n = len(sessions)
    paper_title = bundle_manifest.get("paper_title") or ""
    paper_doi = bundle_manifest.get("paper_doi") or ""

    models: set[str] = set()
    first_ts: str | None = None
    last_ts: str | None = None
    for s in sessions:
        for m in s.get("models") or []:
            if m and m != "<synthetic>":
                models.add(m)
        ft = s.get("first_timestamp")
        lt = s.get("last_timestamp")
        if ft and (first_ts is None or ft < first_ts):
            first_ts = ft
        if lt and (last_ts is None or lt > last_ts):
            last_ts = lt

    models_line = ", ".join(sorted(models)) if models else "(not recorded by this source)"
    date_range = (
        f"{first_ts} to {last_ts}" if (first_ts and last_ts and first_ts != last_ts)
        else (first_ts or last_ts or "(not recorded)")
    )

    paper_line = ""
    if paper_title and paper_doi:
        paper_line = f"the accompanying paper *{paper_title}* ({paper_doi})"
    elif paper_title:
        paper_line = f"the accompanying paper *{paper_title}*"
    elif paper_doi:
        paper_line = f"the accompanying paper ({paper_doi})"
    else:
        paper_line = "the accompanying paper"

    sessions_word = "session" if n == 1 else "sessions"

    body = (
        f"# LLM session logs — reviewer guide\n\n"
        f"This deposit contains the verbatim log files for {n} large-language-model "
        f"{sessions_word} referenced in {paper_line}. It is provided per journal "
        f"reproducibility requirements for LLM-assisted research (e.g. RSC Digital "
        f"Discovery's *Use of large language models in research* policy).\n\n"
        f"## Models used\n\n"
        f"{models_line}\n\n"
        f"## Generation date range\n\n"
        f"{date_range}\n\n"
        f"## How to read this deposit (no special tools required)\n\n"
        f"- Open `index.html` in any modern browser to see the full session list "
        f"with metadata. Click any session card to read its transcript in a "
        f"styled, scrollable view. Works offline; no internet connection needed.\n\n"
        f"- Each session subdirectory under this root contains:\n"
        f"  - `transcript.jsonl` — the **verbatim** record of every input and "
        f"output (user messages, assistant responses, tool calls, tool results, "
        f"thinking blocks where the model supported them). This is the canonical "
        f"artifact the paper's claims rest on.\n"
        f"  - `manifest.json` — model identifier(s), first/last timestamps, "
        f"message count, SHA-256 of the transcript, environment snapshot, and "
        f"(when the host CLI recorded it) the git state of the project repo at "
        f"session start.\n"
        f"  - `view.html` — self-contained HTML rendering of `transcript.jsonl` "
        f"for browser review. Regenerated from the JSONL; not part of the "
        f"integrity chain — the JSONL is.\n"
        f"  - `cite.txt` — one-screen citation block.\n\n"
        f"## How to verify integrity\n\n"
        f"See `VERIFY.md` for the canonical procedure using stock Unix tools "
        f"(`shasum`, `jq`). Each transcript's SHA-256 is recorded in both the "
        f"per-session `manifest.json` and the top-level `BUNDLE_MANIFEST.json`.\n\n"
        f"## What this deposit does NOT include\n\n"
        f"- Files the LLM read or wrote outside its own transcript (these are "
        f"referenced by path inside the transcripts).\n"
        f"- The state of external code repositories referenced in the "
        f"transcripts. Those are recorded by commit hash in each session's "
        f"`manifest.json` under `project_git_state` (when the host CLI captured "
        f"one). Reproduce by `git checkout` of the corresponding repo at the "
        f"recorded commit.\n\n"
        f"---\n\n"
        f"Generated by [holotype](https://github.com/PEEKPerformer/holotype), "
        f"a forensic-grade archival tool for LLM agent CLI sessions.\n"
    )
    (out_dir / "README.md").write_text(body)


def write_verify_md(out_dir: Path) -> None:
    (out_dir / "VERIFY.md").write_text(
        dedent(
            """\
            # Verifying this paper bundle (no Claude required)

            This directory bundles one or more holotype-archived LLM sessions
            referenced by a paper. Each subdirectory is one session. Every
            session ships its raw ``transcript.jsonl`` in plain (uncompressed)
            JSONL so verification needs only stock Unix tools.

            ## To verify every session at once

            ```bash
            jq -r '.sessions[] | "\\(.sha256)  \\(.session_id)/transcript.jsonl"' \\
                BUNDLE_MANIFEST.json | shasum -a 256 -c -
            ```

            Each session's per-file ``manifest.json`` carries the same
            ``sha256`` so per-session verification is also possible:

            ```bash
            cd <session-id>/
            recomputed=$(shasum -a 256 transcript.jsonl | awk '{print $1}')
            recorded=$(jq -r '.sha256' manifest.json)
            [ "$recomputed" = "$recorded" ] && echo "OK" || echo "MISMATCH"
            ```

            ## What this bundle includes per session

            - ``transcript.jsonl`` — verbatim JSONL the host CLI wrote
            - ``manifest.json`` — SHA-256, env capture, model IDs, timestamps,
              token totals, and (when available) the project repo's git
              state at session-start
            - ``cite.txt`` — one-screen citation block
            - ``view.html`` — self-contained HTML rendering of the
              transcript for browser-based review (regenerated from
              the JSONL; not part of the hash chain)

            ## What this bundle does NOT include

            - Files the LLM read or wrote outside its own transcript
            - The state of any external git repos referenced in the
              transcripts — those are recorded by commit hash in each
              manifest's ``project_git_state`` field, where the host CLI
              captured one. Reproduce by ``git checkout`` of the
              corresponding repo at the recorded commit.

            The bundle's own integrity is rooted in ``BUNDLE_MANIFEST.json``
            and (if produced) the sibling ``<bundle>.tar.gz.sha256``.
            """
        )
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bundle multiple sessions into a paper-ready deposit.")
    p.add_argument("--sessions", default="",
                   help="Comma-separated session ID prefixes.")
    p.add_argument("--sessions-file", default="",
                   help="Path to a file listing session ID prefixes (one per line; # comments OK).")
    p.add_argument("--out", required=True, type=Path,
                   help="Output bundle directory.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--tarball", action="store_true",
                   help="Also emit <bundle>.tar.gz + .sha256 sidecar.")
    p.add_argument("--paper-title", default="",
                   help="Optional paper title to record in BUNDLE_MANIFEST.")
    p.add_argument("--paper-doi", default="",
                   help="Optional paper DOI to record in BUNDLE_MANIFEST.")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        return 2

    prefixes = parse_session_list(args)
    if not prefixes:
        sys.stderr.write("paper_bundle: no sessions specified (use --sessions or --sessions-file)\n")
        return 2

    out_dir: Path = args.out.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    sessions_meta: list[dict] = []
    failures: list[str] = []
    for prefix in prefixes:
        sess_dir = resolve_session_by_prefix(archive, prefix)
        if sess_dir is None:
            failures.append(prefix)
            continue

        manifest = json.loads((sess_dir / "manifest.json").read_text())
        bundle_sess = out_dir / sess_dir.name
        bundle_sess.mkdir(parents=True, exist_ok=True)

        transcript_bytes = read_transcript_bytes(sess_dir)
        if transcript_bytes is None:
            failures.append(f"{prefix} (transcript unreadable)")
            continue
        (bundle_sess / "transcript.jsonl").write_bytes(transcript_bytes)
        shutil.copy(sess_dir / "manifest.json", bundle_sess / "manifest.json")

        # Per-session cite.txt + render.md, sourced from cite.py logic.
        # We inline a minimal version to avoid importing the cite.py
        # script as a module (it has argparse at module scope).
        cite_txt = (
            f"Holotype session {manifest.get('session_id','?')}\n"
            f"  SHA-256:        {manifest.get('sha256','?')}\n"
            f"  Deposited:      {manifest.get('deposited_at','?')}\n"
            f"  Source:         {manifest.get('source','?')}\n"
            f"  Models:         {', '.join(manifest.get('models') or ['?'])}\n"
            f"  Messages:       {manifest.get('message_count','?')}\n"
            f"  Wall clock (s): {manifest.get('wall_clock_seconds','?')}\n"
        )
        gs = manifest.get("project_git_state")
        if isinstance(gs, dict):
            cite_txt += (
                f"  Project repo:   {gs.get('remote') or '?'} @ "
                f"{gs.get('commit_short') or gs.get('commit') or '?'} "
                f"({gs.get('captured_at','?')})\n"
            )
        (bundle_sess / "cite.txt").write_text(cite_txt)

        try:
            render_session(manifest, bundle_sess / "transcript.jsonl", bundle_sess / "view.html")
        except Exception as e:  # rendering is best-effort; never fail the bundle
            sys.stderr.write(f"  warning: view.html render failed for {prefix}: {e}\n")

        sessions_meta.append({
            "session_id": manifest.get("session_id"),
            "source": manifest.get("source"),
            "sha256": manifest.get("sha256"),
            "models": manifest.get("models", []),
            "message_count": manifest.get("message_count"),
            "first_timestamp": manifest.get("first_timestamp"),
            "last_timestamp": manifest.get("last_timestamp"),
            "wall_clock_seconds": manifest.get("wall_clock_seconds"),
            "total_input_tokens": manifest.get("total_input_tokens"),
            "total_output_tokens": manifest.get("total_output_tokens"),
            "project_git_state": manifest.get("project_git_state"),
        })

    bundle_manifest = {
        "schema_version": 1,
        "produced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive_commit": archive_commit(archive),
        "paper_title": args.paper_title or None,
        "paper_doi": args.paper_doi or None,
        "session_count": len(sessions_meta),
        "sessions": sessions_meta,
    }
    (out_dir / "BUNDLE_MANIFEST.json").write_text(
        json.dumps(bundle_manifest, indent=2) + "\n"
    )
    write_verify_md(out_dir)
    write_reviewer_readme(out_dir, bundle_manifest)
    try:
        render_index(out_dir, bundle_manifest, out_dir / "index.html")
    except Exception as e:
        sys.stderr.write(f"  warning: index.html render failed: {e}\n")

    if args.tarball:
        tar_path = out_dir.with_suffix(out_dir.suffix + ".tar.gz")
        # tar from the parent dir so the archive contains the bundle
        # folder at top level (Zenodo-friendly).
        subprocess.run(
            ["tar", "-czf", str(tar_path),
             "-C", str(out_dir.parent), out_dir.name],
            check=True,
        )
        sha = sha256_file(tar_path)
        (tar_path.parent / f"{tar_path.name}.sha256").write_text(f"{sha}  {tar_path.name}\n")
        print(f"  tarball: {tar_path}")
        print(f"  sha256:  {sha}")

    print(f"  bundle:    {out_dir}")
    print(f"  sessions:  {len(sessions_meta)}")
    if failures:
        print(f"  failed:    {len(failures)} ({', '.join(failures)})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
