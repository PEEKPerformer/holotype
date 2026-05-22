# Linux: background ingest via user systemd

The macOS launchd background tick (`scripts/install-launchd.py`) catches long-running agent-CLI sessions whose `Stop` hook never fires. Linux users get the same behavior via a user-level systemd timer + service unit. This document is a reference template — not yet installed by a script.

## Why a user-level service

The archive lives in the user's home directory and references the user's session JSONLs. Running ingest as the user (not as root, not via cron) preserves the file ownership chain holotype assumes, and keeps the unit visible to `systemctl --user` for inspection without sudo.

## The service unit

Write to `~/.config/systemd/user/holotype-ingest.service`:

```ini
[Unit]
Description=holotype: deposit any new agent-CLI sessions into the archive
After=default.target

[Service]
Type=oneshot
# Replace <HOLOTYPE_REPO> with the absolute path to your holotype clone
# (e.g. /home/you/Git/holotype) and <ARCHIVE> with the absolute path
# returned by `cat ~/.config/holotype/archive-path`.
ExecStart=/usr/bin/env python3 <HOLOTYPE_REPO>/scripts/ingest.py --archive <ARCHIVE> --quiet
# Hold standard limits — the ingest is mostly I/O bound and should
# never exceed normal user resource caps.
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
```

## The timer unit

Write to `~/.config/systemd/user/holotype-ingest.timer`:

```ini
[Unit]
Description=holotype: fire the deposit service every 30 minutes

[Timer]
# First fire 5 minutes after boot (don't compete with login activity),
# then every 30 minutes thereafter.
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true
Unit=holotype-ingest.service

[Install]
WantedBy=timers.target
```

## Enable + start

```bash
systemctl --user daemon-reload
systemctl --user enable --now holotype-ingest.timer
```

Verify it's scheduled:

```bash
systemctl --user list-timers holotype-ingest.timer
```

## Inspect / debug

```bash
# Did the last run succeed?
systemctl --user status holotype-ingest.service

# Tail past runs:
journalctl --user -u holotype-ingest.service --since "1 hour ago"

# Fire one off-schedule run:
systemctl --user start holotype-ingest.service
```

## Removing

```bash
systemctl --user disable --now holotype-ingest.timer
rm ~/.config/systemd/user/holotype-ingest.{service,timer}
systemctl --user daemon-reload
```

## Caveats

- **User must be logged in for the timer to fire** unless lingering is enabled (`loginctl enable-linger <user>`). For headless servers or auto-suspending laptops, enable lingering or accept that the timer pauses when you're logged out.
- **Concurrency safety is handled inside ingest.py** via `flock` at `<archive>/.holotype/.lock`. If you accidentally run two ingest cycles back-to-back, the second exits cleanly with "another ingest is running, exiting."
- **GPG-signed commits** (`--sign-commits`) need `GPG_TTY` available. For a systemd-driven invocation, that usually means using `gpg-agent` with `allow-loopback-pinentry` and an unlocked key. Test interactively first.
- This template doesn't push to a remote — same as the macOS launchd tick. Pushing is always an explicit user action.

## When to write `scripts/install-systemd.py`

If installing this manually becomes annoying, write a `scripts/install-systemd.py` mirroring `scripts/install-launchd.py`'s structure: template strings, `--archive` flag, idempotent install/uninstall, refuses to run on non-Linux. The hardest part is detecting whether `systemctl --user` is even available (containers / WSL2-without-systemd / minimal distros). Until that's done, this document is the canonical reference.
