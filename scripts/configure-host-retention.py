#!/usr/bin/env python3
"""Extend host-CLI session-transcript retention so holotype has time
to deposit before the host CLI prunes.

Currently handles Claude Code (`~/.claude/settings.json` —
`cleanupPeriodDays`). Codex's session retention is not surfaced as a
configurable setting in the version checked here; if/when it is, add
a parallel check for `~/.codex/config.toml`.

Why this exists:

  Claude Code's default `cleanupPeriodDays` is 30. Holotype deposits on
  demand or via the launchd tick. If a session is over 30 days old by
  the time the user thinks to deposit it, it's gone — the rsync
  backup catches files only up to the last sync, and once the source
  JSONL has been deleted the rsync mirror can also lose it on the
  next `rsync --delete` cycle. Bumping retention to ~100 years
  (`36500`) makes Claude Code effectively "never delete" — the
  pruning still exists, just out at a horizon you'll never hit.

Read-only `--check-only` mode reports current state without modifying
anything. The setup wizard uses this first to ask the user; if they
say yes, it re-invokes without `--check-only` to apply.

Usage:
    python scripts/configure-host-retention.py --check-only
    python scripts/configure-host-retention.py --target-days 36500
    python scripts/configure-host-retention.py --json --check-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

DEFAULT_TARGET = 36500       # ~100 years; the canonical "never prune" value
DEFAULT_MIN_OK = 365         # treat anything >= 1 year as already safe-enough


def report_claude_code(target: int, min_ok: int, check_only: bool) -> dict:
    """Inspect Claude Code's retention setting. If needed and not in
    check-only mode, update it atomically."""
    result = {
        "host": "claude-code",
        "settings_path": str(CLAUDE_SETTINGS),
        "exists": CLAUDE_SETTINGS.exists(),
        "current": None,
        "target": target,
        "action": "unchanged",
        "note": "",
    }

    if not CLAUDE_SETTINGS.exists():
        result["action"] = "skipped-not-installed"
        result["note"] = (
            "Claude Code settings.json not found. If Claude Code is installed, "
            "launch it once to create the settings file, then re-run."
        )
        return result

    try:
        settings = json.loads(CLAUDE_SETTINGS.read_text())
    except json.JSONDecodeError as e:
        result["action"] = "error"
        result["note"] = f"settings.json is not valid JSON: {e}"
        return result

    current = settings.get("cleanupPeriodDays")
    result["current"] = current

    if current is not None and current >= min_ok:
        result["action"] = "already-ok"
        result["note"] = (
            f"cleanupPeriodDays = {current} is already >= {min_ok}; no change needed."
        )
        return result

    if check_only:
        result["action"] = "would-update"
        result["note"] = (
            f"cleanupPeriodDays is {'<unset>' if current is None else current}; "
            f"would set to {target} (~{target//365} years)."
        )
        return result

    settings["cleanupPeriodDays"] = target
    tmp = CLAUDE_SETTINGS.with_suffix(".json.partial")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, CLAUDE_SETTINGS)
    result["action"] = "updated"
    result["note"] = (
        f"cleanupPeriodDays: {current} -> {target}. "
        "Takes effect at next Claude Code startup (cleanup runs at launch)."
    )
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Extend host-CLI transcript retention.")
    p.add_argument("--target-days", type=int, default=DEFAULT_TARGET,
                   help=f"Value to set (default {DEFAULT_TARGET}, ~100 years).")
    p.add_argument("--min-acceptable", type=int, default=DEFAULT_MIN_OK,
                   help=f"Don't modify if current is >= this (default {DEFAULT_MIN_OK}).")
    p.add_argument("--check-only", action="store_true",
                   help="Report without modifying anything.")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of formatted text.")
    args = p.parse_args(argv)

    reports = [report_claude_code(args.target_days, args.min_acceptable, args.check_only)]

    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        for r in reports:
            print(f"  {r['host']}:  {r['settings_path']}")
            if r["current"] is not None:
                print(f"    current cleanupPeriodDays: {r['current']}")
            else:
                print(f"    current cleanupPeriodDays: <unset>")
            print(f"    action: {r['action']}")
            if r["note"]:
                print(f"    note:   {r['note']}")
            print()

    # Exit code: 0 = clean (already-ok or updated), 1 = action would be needed
    # (only meaningful in --check-only mode), 2 = error.
    for r in reports:
        if r["action"] == "error":
            return 2
        if r["action"] == "would-update":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
