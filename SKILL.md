---
name: holotype
description: User-invocable skill for forensic-grade archival of Claude Code sessions into a hash-chained git repository suitable for citation in scientific publications. Operations include first-time setup, depositing sessions, searching the archive, producing citable bundles, verifying integrity, and loading past-session context. Deliberate manual invocation only — never auto-triggered. Invoke via /holotype.
disable-model-invocation: true
---

# holotype

You are operating on the **holotype** archive — a content-addressable, hash-chained git repository of Claude Code session transcripts. The archive is the canonical scientific record. Treat it as immutable.

## Step 0 — First-time setup (always do this first if no archive exists)

Before depositing anything, the user must consciously choose **where the archive lives** and **whether it has a remote**. Transcripts contain everything Claude saw — file paths, the contents of files that were read, command output that may have included credentials, internal codebase details. Pushing to any remote is a privacy and security decision that must not be a silent default.

**Check** whether `~/.config/holotype/archive-path` exists. If it does, read it to find the archive — setup is already done. If it doesn't, **conduct the wizard one decision at a time**, asking the user via whatever the host CLI provides for structured user input (Claude Code: `AskUserQuestion`; Codex: in-chat prompts; etc.). Do not bypass with `input()` from the script — the wizard belongs in the conversation so the user can revise answers before anything is written. After all answers are collected, invoke `scripts/init.py` with them as flags.

### Wizard steps

**Step 1 — Archive location.** Ask with default suggestion `~/Documents/holotype-archive`. Expand `~` to an absolute path before passing to the script.

**Step 2 — Storage projection (consent).** Before any other decisions, the user should see what this archive will cost on disk. Run:

```bash
python scripts/usage_estimate.py --json
```

The script walks each registered Source's default paths (e.g. `~/.claude/projects/`, `~/.codex/sessions/`) and projects forward from current usage. Show the user:

> "Based on N session file(s) across the last D days of host-CLI history (totaling X GB), expect roughly Y MB/day → Z GB/month of uncompressed deposits. With zstd compression on (default when available), JSONL typically gets 30–60% smaller — content-dependent."

If `combined.no_data` is true (fresh host-CLI install), say so honestly — projection unavailable, defaults are fine, projection can be re-checked later via `scripts/usage_estimate.py`.

Then decide compression. Default is **`auto`**: zstd if the binary is on PATH, plain JSONL otherwise. Detect by:

```bash
command -v zstd
```

- **zstd is installed**: tell the user "Compression will be enabled (zstd available on your system)." Proceed with `--compression auto` (resolves to `zstd`).
- **zstd is NOT installed**: offer to install it on their behalf so they get the storage savings. Detect the package manager and propose the exact command:
  - macOS with Homebrew: `brew install zstd`
  - Debian/Ubuntu: `sudo apt install zstd`
  - Fedora: `sudo dnf install zstd`
  - Arch: `sudo pacman -S zstd`
  - Windows: `winget install zstd` or `scoop install zstd`

  Ask: "zstd isn't installed but I can `brew install zstd` for you so this archive gets compressed deposits (~30–60% smaller). Install? (Y/n)" — never run the install without explicit confirmation. If yes, run the install, then proceed with compression on. If no, fall back to `--compression none` and continue without compression.

If `low_confidence` is true (date span < 7 days), warn the user the projection is based on a short window and is approximate.

Also offer to change archive location at this point (e.g., for a user with a small system disk who wants the archive on an external drive — go back to Step 1) or to cancel.

**Step 3 — Privacy decision: remote or local-only?** This is the critical decision. State explicitly to the user, *before* offering options:

> "The archive will contain verbatim Claude Code transcripts, including any file contents, command output, environment details, and tool results Claude saw during sessions. If you add a git remote, all of that data will be pushed to that remote when you sync. Choose carefully."

Then offer:
- **Local only (recommended for sensitive work)** — no remote. Easiest, safest.
- **GitHub private repo** — convenient if you trust GitHub; data leaves your machine.
- **Self-hosted git** — Gitea / Forgejo / GitLab CE / bare repo on your own server. Most private if you control the server.
- **Synology or local-network git** — middle ground; data stays on hardware you own.
- **Other URL** — user pastes the remote.

**Step 4 — If a remote was chosen, get the URL.** For GitHub, also ask for org/account and repo name and offer to `gh repo create --private` it on the fly (but only if the user says "yes" — never silently).

**Step 5 — Confirm before writing.** Show a summary:
> "I'll create the archive at `<path>` as a new git repo. Compression: `<auto|none|zstd>` (resolves to `<zstd|none>` on this system). GPG-signed commits: `<yes|no>`. Remote: `<url-or-none>`. Push policy: manual (never automatic). Proceed?"

For high-stakes archives (anything destined for a paper's Zenodo deposit), additionally offer GPG-signed commits — adds a `--sign-commits` flag to init that turns on `config.deposit.sign_commits=true`, after which every deposit commit is GPG-signed. Requires `git config user.signingkey` to be set; if it's empty, init still proceeds but warns that the first ingest will fail until the user wires GPG up.

**Step 6 — Run init.** Call:
```bash
python scripts/init.py --path <abs-path> --remote-url <url-or-empty> --remote-kind <github-private|self-hosted|synology|other|none> --compression <auto|none|zstd> [--sign-commits]
```

The script writes `<archive>/.holotype/config.json` (including the *resolved* compression mode — `auto` is replaced with the concrete choice), drops `VERIFY.md` + `README.md` into the archive, makes the initial commit, and writes a pointer file at `~/.config/holotype/archive-path` so future sessions can find the archive. If `--compression zstd` is forced but zstd isn't on PATH, init exits 2; `--compression auto` falls back to plain JSONL with a notice.

**Step 7 — Host-CLI retention check.** Holotype's forensic-completeness promise has a hole if the host CLI prunes session transcripts before holotype can deposit them. Claude Code's default `cleanupPeriodDays` is 30; we want effectively-never. Run:

```bash
python scripts/configure-host-retention.py --check-only
```

If the script reports `would-update`, ask:

> "Claude Code is set to delete session transcripts after N days. Holotype can't deposit what's been deleted. Bump retention to ~100 years (the canonical 'never prune' value)? (Y/n)"

If yes:
```bash
python scripts/configure-host-retention.py
```

If the script reports `already-ok` or `skipped-not-installed`, no action needed.

**Step 8 — Background-tick opt-in (macOS only).** Ask:

> "Claude Code sessions often run for hours without explicit close. A 30-minute background tick will catch sessions that the Stop hook misses. It is local-only and never pushes to a remote. Install? (Y/n)"

If yes:
```bash
python scripts/install-launchd.py --archive <abs-path>
```

If on Linux or Windows, skip this step and tell the user the equivalent can be set up later via systemd user unit (Linux) or Task Scheduler (Windows).

**Step 9 — First ingest.** Run an initial deposit to seed the archive from the existing host-CLI session stores:
```bash
python scripts/ingest.py --archive <abs-path>
```

Report the count of sessions deposited.

**Do not skip the wizard.** If the user says "just set it up with defaults," walk through the questions anyway and let them say "yes, yes, local-only, yes, yes, yes, yes" to each. The point is informed consent on the storage cost, the remote decision, and on modifying the host CLI's settings — not speed.

## Where things live (after setup)

- **Skill source**: this directory (`~/Git/holotype/` or wherever the user has it).
- **Archive**: path is recorded in `~/.config/holotype/archive-path` (a single-line text file). The archive itself contains its config at `<archive>/.holotype/config.json` and is portable — move the folder, the config goes with it.
- **Source JSONLs (Claude Code)**: `~/.claude/projects/` (live) and `~/Documents/Claude-Backups/` (the rsync mirror — preferred source because it can't be pruned).
- **Source JSONLs (Codex)**: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl`. Codex deposits land under `sessions/codex/<YYYY>/<MM>/<DD>/<session-id>/` in the archive. Note: holotype is forbidden from reading anything else in `~/.codex/` — `auth.json` and credentials sit at that parent level.

## Supported agent CLIs (sources)

Each Source knows how to discover and parse transcripts from one agent CLI. Adding a new CLI is one file under `holotype/sources/`. Current sources:

| Source name | Default paths | Filename convention | Notes |
|-------------|---------------|---------------------|-------|
| `claude-code` | `~/Documents/Claude-Backups`, `~/.claude/projects` | `<session-uuid>.jsonl` with optional `subagents/agent-*.jsonl` | Subagents nested under their parent session in the archive |
| `codex` | `~/.codex/sessions` | `rollout-<iso-ts>-<uuid>.jsonl` | Date-partitioned in the archive; no subagent files (Codex spawn_agent embeds in parent rollout) |
| `antigravity` | `~/.gemini/antigravity-cli/brain` | `<conversation-uuid>/.system_generated/logs/transcript_full.jsonl` | Picks `transcript_full.jsonl` (native-JSON tool args) over the sibling `transcript.jsonl` (re-stringified args). The encrypted `.pb` in `conversations/` is ignored. `~/.gemini/oauth_creds.json` is OUT OF SCOPE — Source root is `brain/`, never the `.gemini` parent. |

To explicitly ingest from one source path: `python scripts/ingest.py --source <path> --source-name <name>`. To ingest from all detected defaults across all sources: `python scripts/ingest.py`.

### A note on freshness — this skill is not gospel

Agent CLIs are evolving fast and their on-disk formats drift. The Source classes shipped here capture the schemas we empirically observed at the time the file was written; nothing prevents Anthropic / OpenAI / Google from renaming fields, adding new record types, splitting subagent storage, or moving directory paths next month. **If you are reading this skill and something doesn't match what you see on the host CLI's actual filesystem, trust the filesystem and update the Source.**

Concretely, you (the LLM running this skill) should:

- Open a session file on the user's machine and read a few lines before assuming the parser still matches. A diff between observed and documented schema is the cheapest fix possible.
- If the existing Source under-reports a field (missing model, missing tool flag, miscounted messages), patch `holotype/sources/<name>.py`, add a fixture line that exercises the new shape, and re-run `scripts/selftest.py`.
- If a brand-new CLI shows up, follow the next section.

The non-negotiables (forensic completeness, append-only, no silent push) are stable. The mechanics of "where the bytes come from and how they're shaped" are not, and you're encouraged to verify rather than recite.

### If you're running in a CLI that holotype doesn't yet recognize

You — the LLM in the host CLI — are expected to **read [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md) and write a new `Source` class** rather than pretend the CLI's sessions don't exist. The Source ABC is the contract; the document is the field guide for filling it in safely. The user's invocation of `/holotype` (or its host-CLI equivalent) implies "make this work for me here," not "skip my CLI silently."

When you do this, validate via `python scripts/selftest.py` against a fixture you wrote that mirrors a real session from your CLI. If selftest passes, your source is integration-ready. If it doesn't, surface the failure to the user before depositing anything — a wrong Source corrupts the archive's forensic claim.

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
| First-time setup | Conduct wizard, then `python scripts/init.py --path <p> --remote-url <u> --remote-kind <k> --compression <auto|none|zstd>` |
| Estimate storage cost from existing host-CLI history | `python scripts/usage_estimate.py` |
| Check / bump host-CLI retention | `python scripts/configure-host-retention.py [--check-only]` |
| Install macOS background tick (after setup) | `python scripts/install-launchd.py --archive <p>` |
| Deposit new sessions (preferred — from the rsync backup) | `python scripts/ingest.py --source ~/Documents/Claude-Backups` |
| Deposit directly from live Claude Code state | `python scripts/ingest.py --source ~/.claude/projects` |
| Find sessions matching text (FTS5) | `python scripts/search.py "<query>"` |
| Just the citation string for one session | `python scripts/cite.py <session-id> --citation-only` |
| Print the manifest for one session | `python scripts/cite.py <session-id> --manifest-only` |
| Produce a full citable bundle for one session | `python scripts/cite.py <session-id>` |
| Bundle many sessions for a paper's Zenodo deposit | `python scripts/paper_bundle.py --sessions a,b,c --out <dir> [--tarball]` |
| Verify the archive's hash chain | `python scripts/verify.py` |
| Verify a single session | `python scripts/verify.py <session-id>` |
| Drop and rebuild the SQLite index from the canonical archive | `python scripts/reindex.py` |
| Load a past session's transcript into this chat | `python scripts/context.py <session-id>` then `Read` the printed path |
| Push to the configured remote (explicit, never auto) | confirm with user, then `git -C <archive> push` |

Session IDs are UUID prefixes — typically 8 hex chars are enough to disambiguate.

## Invocation model

This skill is **user-invocable only**. Implicit invocation is disabled in both implementations of the open agent skills standard:
- Claude Code reads `disable-model-invocation: true` from SKILL.md frontmatter.
- Codex reads `policy.allow_implicit_invocation: false` from `agents/openai.yaml`.

You will never auto-trigger this skill based on conversational cues. The user invokes it explicitly (`/holotype` in Claude Code, `$holotype` in Codex), and only then do you consult this file. If the user is doing paper-relevant work and an archival pattern would help them, you may *mention* that holotype exists — but you do not invoke it on their behalf.

Rationale: depositing transcripts into a hash-chained scientific archive is a deliberate provenance decision, not an opportunistic background task. The unfiltered safety-net backup is already handled separately (e.g., a `Stop`-hook rsync to `~/Documents/Claude-Backups/` on the original author's machine). Holotype is for *curated* deposits the user wants on the record. Conflating those two layers would undermine both.

## Verification without Claude

The archive is a plain git repository. A reviewer or auditor with no access to Claude Code can verify it using stock Unix tools. See [VERIFY.md](VERIFY.md) for the procedure. The skill is *convenience automation*; the archive itself is the scientific artifact.
