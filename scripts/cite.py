#!/usr/bin/env python3
"""Produce a citable bundle for a single deposited session.

A citable bundle is a self-contained directory suitable for a paper's
Data Availability deposit (Zenodo, supplementary materials, etc.). It
contains the raw transcript, the manifest, a human-readable Markdown
render of the conversation, and a one-line citation string.

The bundle is independently verifiable: anyone can `shasum -a 256` the
transcript and compare it to the manifest's recorded hash. They do not
need the holotype skill installed.

Usage:
    python scripts/cite.py 3f1c4cf7
    python scripts/cite.py 3f1c4cf7 --out /tmp/my-citation
    python scripts/cite.py 3f1c4cf7 --manifest-only
    python scripts/cite.py 3f1c4cf7 --citation-only
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


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


def resolve_session(archive: Path, prefix: str) -> tuple[str, Path] | None:
    """Find the session whose UUID starts with the given prefix.

    Returns (full_session_id, session_dir) or None if not found or ambiguous.
    """
    sessions_root = archive / "sessions"
    if not sessions_root.exists():
        return None

    matches: list[Path] = []
    for proj in sessions_root.iterdir():
        if not proj.is_dir():
            continue
        for sess in proj.iterdir():
            if sess.is_dir() and sess.name.startswith(prefix):
                matches.append(sess)

    if not matches:
        return None
    if len(matches) > 1:
        sys.stderr.write(f"ambiguous prefix '{prefix}', matches:\n")
        for m in matches:
            sys.stderr.write(f"  {m.name}\n")
        return None

    sess = matches[0]
    return (sess.name, sess)


def render_markdown(transcript: Path) -> str:
    """Render a JSONL transcript as a human-readable Markdown document.

    Lossy by design — for human reading only. The canonical record is
    the raw JSONL alongside.
    """
    out: list[str] = ["# Session transcript\n"]
    out.append(f"_Source: `{transcript.name}` (raw JSONL is the canonical record)_\n")

    with transcript.open("rb") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
            if not msg:
                continue

            role = msg.get("role") or obj.get("type") or "?"
            timestamp = obj.get("timestamp", "")
            content = msg.get("content")

            out.append(f"## {role}  _{timestamp}_\n")

            if isinstance(content, str):
                out.append(content + "\n")
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    t = item.get("type")
                    if t == "text":
                        out.append((item.get("text") or "") + "\n")
                    elif t == "thinking":
                        out.append("<details><summary>thinking</summary>\n\n")
                        out.append((item.get("thinking") or "") + "\n\n</details>\n")
                    elif t == "tool_use":
                        name = item.get("name", "?")
                        inp = json.dumps(item.get("input", {}), indent=2, ensure_ascii=False)
                        out.append(f"**tool_use: `{name}`**\n\n```json\n{inp}\n```\n")
                    elif t == "tool_result":
                        out.append("**tool_result**\n\n")
                        inner = item.get("content")
                        if isinstance(inner, str):
                            out.append(f"```\n{inner}\n```\n")
                        elif isinstance(inner, list):
                            for sub in inner:
                                if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                                    out.append(f"```\n{sub['text']}\n```\n")
            out.append("\n---\n\n")
    return "".join(out)


def git_short_sha(archive: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(archive), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else "?"


def build_citation(manifest: dict, archive_commit: str) -> str:
    sid = manifest.get("session_id", "?")
    sha = manifest.get("sha256", "?")
    dep = manifest.get("deposited_at", "?")
    return (
        f"Holotype session {sid}\n"
        f"  SHA-256:        {sha}\n"
        f"  Deposited:      {dep}\n"
        f"  Archive commit: {archive_commit}\n"
        f"  Models:         {', '.join(manifest.get('models') or ['?'])}\n"
        f"  Messages:       {manifest.get('message_count', '?')}\n"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Produce a citable bundle for one session.")
    p.add_argument("session_id", help="Session UUID or prefix.")
    p.add_argument("--archive", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None,
                   help="Output bundle directory (default: <archive>/cite/<id>/).")
    p.add_argument("--manifest-only", action="store_true",
                   help="Print the manifest JSON to stdout, do not write a bundle.")
    p.add_argument("--citation-only", action="store_true",
                   help="Print only the citation string to stdout.")
    args = p.parse_args(argv)

    archive = find_archive(args.archive)
    resolved = resolve_session(archive, args.session_id)
    if not resolved:
        sys.stderr.write(f"holotype: no session matching prefix '{args.session_id}' in {archive}\n")
        return 1
    session_id, session_dir = resolved

    transcript = session_dir / "transcript.jsonl"
    manifest_path = session_dir / "manifest.json"
    if not (transcript.exists() and manifest_path.exists()):
        sys.stderr.write(f"holotype: session {session_id} is missing files\n")
        return 1

    manifest = json.loads(manifest_path.read_text())
    archive_commit = git_short_sha(archive)
    citation = build_citation(manifest, archive_commit)

    if args.citation_only:
        print(citation)
        return 0
    if args.manifest_only:
        print(json.dumps(manifest, indent=2))
        return 0

    out_dir = args.out or (archive / "cite" / session_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy(transcript, out_dir / "transcript.jsonl")
    shutil.copy(manifest_path, out_dir / "manifest.json")
    (out_dir / "cite.txt").write_text(citation)
    (out_dir / "render.md").write_text(render_markdown(transcript))

    print(f"  bundle:   {out_dir}")
    print(f"  files:    transcript.jsonl, manifest.json, cite.txt, render.md")
    print()
    print(citation, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
