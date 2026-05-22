"""Environment capture for session manifests.

For each deposit we record the minimum context needed to interpret the
transcript later: which Claude Code version produced it, which model(s)
appeared, which working directory it was run from, and the git HEAD of
that working directory at deposit time (if it is a git repo).
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path


def claude_code_version() -> str | None:
    """Best-effort detection of the installed Claude Code version."""
    cc = shutil.which("claude")
    if not cc:
        return None
    try:
        out = subprocess.run(
            [cc, "--version"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def platform_info() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def models_in_transcript(jsonl_path: Path) -> list[str]:
    """Scan a JSONL file for distinct `model` IDs that appeared in assistant turns."""
    seen: set[str] = set()
    with jsonl_path.open("rb") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if isinstance(msg, dict):
                model = msg.get("model")
                if isinstance(model, str):
                    seen.add(model)
    return sorted(seen)


def project_dir_from_archive_path(archive_path: str) -> str | None:
    """Recover the original project working dir from a Claude Code project-dir name.

    Claude Code encodes the cwd in the project dir name by replacing slashes
    with dashes, e.g. `/Users/x/Git/foo` -> `-Users-x-Git-foo`. This reverses
    that encoding to a best-effort path string. Not guaranteed to be exact
    when the original path itself contained dashes.
    """
    if not archive_path.startswith("-"):
        return None
    return archive_path.replace("-", "/")


def git_head(path: Path) -> str | None:
    """Return the short HEAD SHA of the git repo at `path`, or None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _git(path: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def git_state_for_path(path: Path) -> dict | None:
    """Snapshot the git state of the project directory at `path`.

    Returns a dict with `commit` (full sha), `commit_short`, `branch`,
    `dirty` (bool), and `remote` (origin URL if any) — or None if the
    path isn't a git repo or doesn't exist on the local filesystem.

    Captured at deposit time, not session-start. For long-running
    sessions that spanned commits, the recorded state is the HEAD as
    of when ``ingest.py`` ran. Sources that have a more accurate
    session-start git snapshot in their own transcript (Codex's line-1
    header) should supply that to ``ingest.py`` instead — see
    `project_git_state` plumbing in ingest.
    """
    if not path or not path.exists():
        return None
    full = _git(path, "rev-parse", "HEAD")
    if not full:
        return None
    state: dict = {
        "commit": full,
        "commit_short": _git(path, "rev-parse", "--short", "HEAD"),
        "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        "captured_at": "deposit",
    }
    # `--quiet` exits non-zero if there are unstaged changes; together
    # with the diff --cached check below it tells us "is the working
    # tree clean."
    try:
        unstaged = subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet"],
            capture_output=True, timeout=5,
        )
        staged = subprocess.run(
            ["git", "-C", str(path), "diff", "--cached", "--quiet"],
            capture_output=True, timeout=5,
        )
        state["dirty"] = (unstaged.returncode != 0) or (staged.returncode != 0)
    except (subprocess.SubprocessError, OSError):
        state["dirty"] = None
    remote = _git(path, "config", "--get", "remote.origin.url")
    if remote:
        state["remote"] = remote
    return state
