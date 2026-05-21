#!/usr/bin/env python3
"""Install a macOS launchd agent that runs `ingest.py` every 30 minutes.

This catches sessions that never get explicitly closed — long-running
conversations whose Stop hooks never fire because the user never exits
Claude Code or runs /compact.

The agent is local-only. It never pushes to a remote. It runs ingest
with --quiet so it doesn't surface output unless something deposits.

The plist is written to ~/Library/LaunchAgents/io.holotype.ingest.plist
and loaded with launchctl. Re-running the script is safe: it unloads
any previous version before writing the new one.

Usage:
    python scripts/install-launchd.py --archive ~/Documents/holotype-archive
    python scripts/install-launchd.py --uninstall

The script REFUSES to run on non-macOS platforms with a clear message.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

LABEL = "io.holotype.ingest"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs"
DEFAULT_INTERVAL_SECONDS = 1800  # 30 minutes


def require_macos() -> None:
    if platform.system() != "Darwin":
        sys.stderr.write(
            "holotype install-launchd: this installer is macOS-only.\n"
            "  On Linux: use a systemd user unit instead (see docs/launchd.md).\n"
            "  On Windows: use Task Scheduler (see docs/launchd.md).\n"
        )
        raise SystemExit(2)


def plist_content(*, python: Path, ingest_py: Path, archive: Path, interval: int) -> str:
    log_out = LOG_DIR / "holotype.out.log"
    log_err = LOG_DIR / "holotype.err.log"
    return dedent(
        f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{LABEL}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python}</string>
                <string>{ingest_py}</string>
                <string>--archive</string>
                <string>{archive}</string>
                <string>--quiet</string>
            </array>
            <key>StartInterval</key>
            <integer>{interval}</integer>
            <key>RunAtLoad</key>
            <false/>
            <key>ProcessType</key>
            <string>Background</string>
            <key>StandardOutPath</key>
            <string>{log_out}</string>
            <key>StandardErrorPath</key>
            <string>{log_err}</string>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
            </dict>
        </dict>
        </plist>
        """
    )


def unload_if_loaded() -> None:
    """launchctl unload is fine to call on a non-existent plist (it'll
    just say so on stderr). We do not bail on its failure."""
    if PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True,
            text=True,
        )


def install(archive: Path, interval: int) -> int:
    require_macos()

    archive = archive.expanduser().resolve()
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(
            f"holotype install-launchd: {archive} doesn't look like a holotype archive.\n"
            f"  expected {archive / '.holotype' / 'config.json'}\n"
            f"  run the setup wizard first, or pass --archive at an existing archive.\n"
        )
        return 2

    ingest_py = Path(__file__).resolve().parent / "ingest.py"
    if not ingest_py.exists():
        sys.stderr.write(f"missing {ingest_py}\n")
        return 2

    python = Path(sys.executable).resolve()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    unload_if_loaded()

    PLIST_PATH.write_text(
        plist_content(python=python, ingest_py=ingest_py, archive=archive, interval=interval)
    )

    load = subprocess.run(
        ["launchctl", "load", str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )
    if load.returncode != 0:
        sys.stderr.write(f"launchctl load failed: {load.stderr}")
        return 1

    print()
    print(f"  installed:   {PLIST_PATH}")
    print(f"  label:       {LABEL}")
    print(f"  interval:    {interval} s ({interval // 60} min)")
    print(f"  python:      {python}")
    print(f"  archive:     {archive}")
    print(f"  stdout log:  {LOG_DIR / 'holotype.out.log'}")
    print(f"  stderr log:  {LOG_DIR / 'holotype.err.log'}")
    print()
    print("The agent will start running on the next interval boundary.")
    print("To uninstall:  python scripts/install-launchd.py --uninstall")
    return 0


def uninstall() -> int:
    require_macos()
    unload_if_loaded()
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        print(f"removed {PLIST_PATH}")
    else:
        print("no holotype launchd agent installed")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--archive", type=Path, default=None,
                   help="Archive path (required unless --uninstall).")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                   help="Seconds between runs (default 1800 = 30 min).")
    p.add_argument("--uninstall", action="store_true",
                   help="Remove the agent instead of installing.")
    args = p.parse_args(argv)

    if args.uninstall:
        return uninstall()

    if not args.archive:
        # Fall back to pointer file
        pointer = Path.home() / ".config" / "holotype" / "archive-path"
        if pointer.exists():
            args.archive = Path(pointer.read_text().strip())
        else:
            p.error("--archive is required (no pointer file at ~/.config/holotype/archive-path)")

    return install(args.archive, args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
