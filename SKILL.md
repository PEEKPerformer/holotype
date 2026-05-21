---
name: holotype
description: User-invocable skill for forensic-grade archival of Claude Code sessions into a hash-chained git repository suitable for citation in scientific publications. Operations include first-time setup, depositing sessions, searching the archive, producing citable bundles, verifying integrity, and loading past-session context. Deliberate manual invocation only — never auto-triggered. Invoke via /holotype.
disable-model-invocation: true
---

# holotype

You are operating on the **holotype** archive — a content-addressable, hash-chained git repository of Claude Code session transcripts. The archive is the canonical scientific record. Treat it as immutable.

## Step 0 — First-time setup (always do this first if no archive exists)

Before depositing anything, the user must consciously choose **where the archive lives** and **whether it has a remote**. Transcripts contain everything Claude saw — file paths, the contents of files that were read, command output that may have included credentials, internal codebase details. Pushing to any remote is a privacy and security decision that must not be a silent default.

**Check** whether `~/.config/holotype/archive-path` exists. If it does, read it to find the archive — setup is already done. If it doesn't, **conduct the wizard conversationally** (use `AskUserQuestion` for each step, do not just call `input()`), then invoke `scripts/init.py` at the end with all answers as flags.

### Wizard steps

**Step 1 — Archive location.** Ask with default suggestion `~/Documents/holotype-archive`. Expand `~` to an absolute path before passing to the script.

**Step 2 — Privacy decision: remote or local-only?** This is the critical decision. State explicitly to the user, *before* offering options:

> "The archive will contain verbatim Claude Code transcripts, including any file contents, command output, environment details, and tool results Claude saw during sessions. If you add a git remote, all of that data will be pushed to that remote when you sync. Choose carefully."

Then offer:
- **Local only (recommended for sensitive work)** — no remote. Easiest, safest.
- **GitHub private repo** — convenient if you trust GitHub; data leaves your machine.
- **Self-hosted git** — Gitea / Forgejo / GitLab CE / bare repo on your own server. Most private if you control the server.
- **Synology or local-network git** — middle ground; data stays on hardware you own.
- **Other URL** — user pastes the remote.

**Step 3 — If a remote was chosen, get the URL.** For GitHub, also ask for org/account and repo name and offer to `gh repo create --private` it on the fly (but only if the user says "yes" — never silently).

**Step 4 — Confirm before writing.** Show a summary:
> "I'll create the archive at `<path>` as a new git repo. Remote: `<url-or-none>`. Push policy: manual (never automatic). Proceed?"

**Step 5 — Run init.** Call:
```bash
python scripts/init.py --path <abs-path> --remote-url <url-or-empty> --remote-kind <github-private|self-hosted|synology|other|none>
```

The script writes `<archive>/.holotype/config.json`, drops `VERIFY.md` + `README.md` into the archive, makes the initial commit, and writes a pointer file at `~/.config/holotype/archive-path` so future sessions can find the archive.

**Step 6 — Background-tick opt-in (macOS only).** Ask:

> "Claude Code sessions often run for hours without explicit close. A 30-minute background tick will catch sessions that the Stop hook misses. It is local-only and never pushes to a remote. Install? (Y/n)"

If yes:
```bash
python scripts/install-launchd.py --archive <abs-path>
```

If on Linux or Windows, skip this step and tell the user the equivalent can be set up later via systemd user unit (Linux) or Task Scheduler (Windows).

**Step 7 — First ingest.** Run an initial deposit to seed the archive from the existing rsync backup:
```bash
python scripts/ingest.py --archive <abs-path>
```

Report the count of sessions deposited.

**Do not skip the wizard.** If the user says "just set it up with defaults," walk through the questions anyway and let them say "yes, yes, local-only, yes, yes, yes" to each. The point is informed consent on the remote decision, not speed.

## Where things live (after setup)

- **Skill source**: this directory (`~/Git/holotype/` or wherever the user has it).
- **Archive**: path is recorded in `~/.config/holotype/archive-path` (a single-line text file). The archive itself contains its config at `<archive>/.holotype/config.json` and is portable — move the folder, the config goes with it.
- **Source JSONLs**: `~/.claude/projects/` (live) and `~/Documents/Claude-Backups/` (the rsync mirror — preferred source because it can't be pruned).

## The non-negotiables

Read these before any operation:

1. **Never hand-edit a deposited transcript.** The archive's git tree is append-only from outside. If a session JSONL grows in the source (a session keeps running and adds more turns), the next `ingest.py` will detect the change and record it as an `update` commit — that is the only mechanism by which a deposited transcript ever changes on disk.
2. **Never filter content.** Tool calls, tool results, thinking blocks, system reminders, hook outputs — every byte is part of the scientific record. The skill's whole point is forensic completeness.
3. **Never auto-push to a remote.** Even if a remote is configured, push only when the user explicitly says so. Confirm before each push, summarizing what will be uploaded (count of new sessions, total bytes).
4. **The archive is git-tracked.** Every deposit is a commit with a deterministic message of the form `deposit: <project>/<session-id>` or `update: <project>/<session-id>`. Both the JSONL and its manifest live under `sessions/<project-dir>/<session-id>/`.
5. **The SQLite index at `<archive>/.holotype/index.sqlite` is derived data.** It speeds up search. It is NOT a source of truth. It can be deleted and rebuilt with `scripts/reindex.py` (planned) at any time.
6. **Live-file safety is built in.** `ingest.py` skips JSONLs modified in the last 2 seconds and re-checks mtime after reading. You should not need to add additional checks.

## Common operations

| Want to... | Run |
|------------|-----|
| First-time setup | Conduct wizard, then `python scripts/init.py --path <p> --remote-url <u> --remote-kind <k>` |
| Deposit new sessions (preferred — from the rsync backup) | `python scripts/ingest.py --source ~/Documents/Claude-Backups` |
| Deposit directly from live Claude Code state | `python scripts/ingest.py --source ~/.claude/projects` |
| Find sessions matching text (grep MVP) | `python scripts/search.py "<query>"` |
| Print the manifest for one session | `python scripts/cite.py <session-id> --manifest-only` |
| Produce a citable bundle for one session | `python scripts/cite.py <session-id>` |
| Verify the archive's hash chain | `python scripts/verify.py` |
| Load a past session's transcript into this chat | `python scripts/context.py <session-id>` then `Read` the printed path |
| Push to the configured remote (explicit, never auto) | confirm with user, then `git -C <archive> push` |

Session IDs are UUID prefixes — typically 8 hex chars are enough to disambiguate.

## Invocation model

This skill is **user-invocable only** (`disable-model-invocation: true`). You will never auto-trigger it based on conversational cues. The user invokes it explicitly with `/holotype`, and only then do you consult this file. If the user is doing paper-relevant work and a Claude Code archival pattern would help them, you may *mention* that `/holotype` exists — but you do not invoke it on their behalf.

Rationale: depositing transcripts into a hash-chained scientific archive is a deliberate provenance decision, not an opportunistic background task. The unfiltered safety-net backup is already handled by the user's `Stop` hook (rsync to `~/Documents/Claude-Backups/`). Holotype is for *curated* deposits the user wants on the record. Conflating those two layers would undermine both.

## Verification without Claude

The archive is a plain git repository. A reviewer or auditor with no access to Claude Code can verify it using stock Unix tools. See [VERIFY.md](VERIFY.md) for the procedure. The skill is *convenience automation*; the archive itself is the scientific artifact.
