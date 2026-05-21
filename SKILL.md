---
name: holotype
description: Forensic-grade archival of Claude Code sessions for scientific reproducibility. Use when depositing past sessions into the archive, searching the archive for prior work, producing citable bundles for papers, verifying archive integrity, or loading a past session's context into the current chat. Trigger on phrases like "deposit this session", "search the archive", "cite this conversation", "verify the holotype archive", "set up holotype", or "load context from session X".
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

**Do not skip the wizard.** If the user says "just set it up with defaults," walk through the questions anyway and let them say "yes, yes, local-only, yes" to each. The point is informed consent on the remote decision, not speed.

## Where things live (after setup)

- **Skill source**: this directory (`~/Git/holotype/` or wherever the user has it).
- **Archive**: path is recorded in `~/.config/holotype/archive-path` (a single-line text file). The archive itself contains its config at `<archive>/.holotype/config.json` and is portable — move the folder, the config goes with it.
- **Source JSONLs**: `~/.claude/projects/` (live) and `~/Documents/Claude-Backups/` (the rsync mirror — preferred source because it can't be pruned).

## The non-negotiables

Read these before any operation:

1. **Never modify a deposited transcript.** Once a JSONL is in the archive, it is immutable. Re-ingesting is a no-op (UUID dedup). If a session needs an addendum, deposit a *new* session that references the prior one — never edit in place.
2. **Never filter content.** Tool calls, tool results, thinking blocks, system reminders, hook outputs — every byte is part of the scientific record. The skill's whole point is forensic completeness.
3. **Never auto-push to a remote.** Even if a remote is configured, push only when the user explicitly says so. Confirm before each push, summarizing what will be uploaded (count of new sessions, total bytes).
4. **Verify after every deposit.** Run `scripts/verify.py` after ingest. If the hash chain breaks, stop and report — do not "fix" by re-hashing.
5. **The archive is git-tracked.** Every deposit is a commit. Commits should be deterministic (same input → same hash) and reviewable.

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

## When to invoke this skill proactively

- After a paper-relevant session ends, suggest depositing.
- When the user mentions citing a Claude conversation in a paper or note.
- When the user asks "when did we work on X" or "find that conversation about Y".
- When the user is preparing supplementary material for a publication.

## When NOT to invoke

- For general backup of conversations (the user's `Stop` hook already rsyncs everything to `~/Documents/Claude-Backups/` — that is the unfiltered safety net). Holotype is for *curated, citable* deposits.
- For search across hundreds of sessions purely for navigation — claude-conversation-extractor and similar tools handle that better. Holotype's search is for finding deposit candidates, not casual browsing.

## Verification without Claude

The archive is a plain git repository. A reviewer or auditor with no access to Claude Code can verify it using stock Unix tools. See [VERIFY.md](VERIFY.md) for the procedure. The skill is *convenience automation*; the archive itself is the scientific artifact.
