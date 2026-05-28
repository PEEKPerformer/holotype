---
name: holotype
description: User-invocable skill for forensic-grade archival of Claude Code sessions into a hash-chained git repository suitable for citation in scientific publications. Operations include first-time setup, depositing sessions, searching the archive, producing citable bundles, verifying integrity, and loading past-session context. Deliberate manual invocation only — never auto-triggered. Invoke via /holotype.
disable-model-invocation: true
---

# holotype

You are operating on the **holotype** archive — a content-addressable, hash-chained git repository of Claude Code session transcripts. The archive is the canonical scientific record. Treat it as immutable.

## Before any operation — check for updates

The user invoked the skill, which is the only signal we accept for outbound network. Run this once at the start of any holotype operation (setup, ingest, search, cite, verify, paper-bundle — anything):

```bash
python scripts/update_check.py --json
```

The script hits the GitHub releases API, caches the result for 24 hours under `~/.cache/holotype/`, exits 0 on network failure, and emits one of:

- `{"status": "current", ...}` — nothing to say; proceed.
- `{"status": "update-available", "local": "X", "latest": "Y", ...}` — tell the user **once** at the top of the response: *"A newer holotype is available (X → Y). To update: `git -C <repo-root> pull`. Continuing with X."* Then proceed with whatever they asked for. Do not block on the update; do not run the pull on their behalf without explicit confirmation.
- `{"status": "ahead", ...}` — user is on a dev checkout ahead of the latest tag; ignore silently.
- `{"status": "unreachable", ...}` — offline or GitHub API hiccup; ignore silently.

Because of the 24h cache, this is effectively free across an active session.

## Step 0 — First-time setup (always do this first if no archive exists)

### Guiding principle: ask only what has a real cost the user can weigh

A wizard question is itself a UX signal — it implies "this needs your judgment because there's a tradeoff." Every spurious question makes a non-expert search for a hidden cost they shouldn't have to think about. So before asking anything, name the concrete cost a reasonable user would weigh in picking the "wrong" answer. If you can't, **don't ask** — pick the better default, do it, and surface the undo in the post-setup summary.

By that test, the entire first-time setup has exactly **two real questions**: where the archive lives, and whether to back it up to a private GitHub repo. Everything else (compression, signing, retention bump, scheduled-backup install, scheduled-backup interval, encryption-when-backup-is-on) is silently defaulted to the better choice.

Also name BOTH sides of any tradeoff you do present. The previous wizard loudly described the privacy cost of "push to GitHub" while leaving the cost of "local only" unstated, which biased non-experts toward local-only — the strictly worse choice for anyone who might ever cite a session in a paper, because their laptop's disk is a single point of failure for their entire research record.

**Check** whether `~/.config/holotype/archive-path` exists. If it does, read it to find the archive — setup is already done. If it doesn't, run the wizard below: one question at a time, via whatever the host CLI provides for structured user input (Claude Code: `AskUserQuestion`; Codex: in-chat prompts). Do not bypass with `input()` from the script — the wizard belongs in the conversation so the user can revise answers before anything is written.

### Wizard

**Open with a plain-English orientation** (do not start running scripts yet — the user invoked `/holotype` and has no idea what it is). Lead with the universal pain (lost work), not the academic value (citing). Most users want to not lose their chats; the small slice who will ever cite a transcript in a paper get that benefit for free:

> "Heads up — Claude Code automatically deletes every conversation after 30 days. If you've ever tried to find a chat from last month and couldn't, that's why. Codex does the same thing.
>
> Holotype saves a copy of every conversation to a folder on this Mac, so this stops happening. Each copy gets a tamper-proof timestamp — useful if you ever want to cite a chat in a paper, but mostly you'll just appreciate that everything is still there when you need it.
>
> Want to set it up? Two questions, takes a minute."

**Step 0a — Auto-mode preflight.** Claude Code's auto-mode classifier evaluates each Bash call independently and will mid-flow block legitimate setup commands (modifying `~/.claude/settings.json`, installing the launchd plist) even after the user verbally said yes. Detect auto-mode (Claude Code displays "auto mode on" in the status bar) and ask once:

> "Quick — looks like auto-mode is on. Setup modifies a few system files and the classifier will likely block those mid-flow even after you say yes here. Easier if you toggle auto-mode off for the next 60 seconds (Shift+Tab cycles modes), then back on after setup. Want to do that?"

If they say yes, wait for them to confirm. If they say no, proceed but be ready to fall back to the `!`-prefix shell-escape workaround at each blocked step (see Step A for how to explain `!` to non-terminal users).

**Step A — Calibration (one multi-select).** This is *not* a setup decision. The answer changes how the rest of the conversation is phrased — it has real impact, which is why it earns its place. Ask:

> "Quick — which of these have you worked with? (Helps me know how much to explain. Pick any, none is fine.)"
>
> - git or GitHub
> - encryption keys (GPG, SSH)
> - the terminal / command line
> - Python

Record the answers. Use them downstream:

- **Did NOT check `git`**: never say "commit," "remote," "branch," "repo," "push." Say "saved copy," "backup location," "send a copy to GitHub."
- **Did NOT check `encryption keys`**: never say "GPG," "signing," "key pair." If encryption is enabled, describe it as "GitHub stores the backup scrambled so they can't read it." Mention the signing step only in the post-setup summary as a one-liner.
- **Did NOT check `the terminal`**: never hand them a shell command to copy-paste. Run every command via the host CLI's Bash tool. If the auto-mode classifier blocks something, announce plainly: *"My runtime won't let me modify that file directly — I'll show you what to type yourself in a moment, here's exactly what and why."* Then offer the `!<command>` form (and explain it: "Anything in the chat box starting with `!` runs as a shell command on your Mac, and the output comes back to me.").
- **Did NOT check `Python`**: never surface `python scripts/foo.py` as the action; describe the action by what it does ("set up the archive," "back up the existing conversations").
- **Checked all four**: be terse. Skip the explanations. Use the original wizard's vocabulary freely.

**Step B — Archive location (one question with default).** Has a real cost: disk space, where files live, what backs it up. Default to `~/holotype-archive` (top-level home, never iCloud-synced). Ask:

> "Where should the archive live on your Mac? (Default: `~/holotype-archive`)"

Expand `~` to an absolute path. If the user is in `~/UConn_phd/` or another personal research dir, mention that as an alternative ("...or I can put it next to your research folder at `~/UConn_phd/holotype-archive`").

**Actively refuse iCloud-synced paths.** This is not advisory — it's a confirmed failure mode that silently breaks the background ingest job. iCloud's lazy fileprovider holds the archive's `.lock` file in an `EAGAIN/deadlock` state, every `launchd` tick fails with `OSError: [Errno 11] Resource deadlock avoided: …/.lock`, and the user has no idea their archive has been frozen for days (until a "live" badge in the browse UI eventually reveals it). Detect:

```bash
# 1. Is iCloud Desktop & Documents sync enabled?
defaults read MobileMeAccounts 2>/dev/null | grep -E 'CLOUDDESKTOP|MOBILE_DOCUMENTS' | grep -q 'Enabled = 1' && echo "iCloud-Docs-sync-on"
# 2. Does the candidate path resolve under a known iCloud-synced root?
case "<abs-path>" in
  "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Library/Mobile Documents"/*) echo "iCloud-path" ;;
esac
```

If **both** are true (sync is on AND path is under `~/Documents/`, `~/Desktop/`, or `~/Library/Mobile Documents/`): refuse plainly and suggest `~/holotype-archive`. Don't try to be clever about partial paths or symlinks — just say: *"That folder is synced to iCloud, which silently breaks holotype's background ingest job (the lock file becomes unwritable). Pick somewhere outside `~/Documents/` and `~/Desktop/` — `~/holotype-archive` is the conventional spot."* If only one is true (path is under `~/Documents/` but sync is off, OR sync is on but path is `~/Git/...`), proceed.

Also do **not** suggest putting the archive inside an Obsidian vault — Obsidian indexes everything and will choke on the binary `.zst` / `.git` objects.

**Step C — Backup to GitHub? (one question, both sides named).** Has a real cost on either side. Ask, with the cost of *both* choices stated honestly:

> "Want to also save the archive to a private GitHub repo? (Strongly recommended for research work.)
>
> - **Yes** — saved on your Mac AND on GitHub. GitHub stores it scrambled so they can't read the conversation contents. I'll set up the scrambling key and save it in two places so it can't be lost. (Requires you to be signed into `gh` — I'll check.)
> - **No, local only** — saved only on your Mac. If your disk fails or your laptop is lost or stolen, every conversation is gone forever, including any you might want to cite in a future paper. Pick this only if your institution forbids storing pre-publication data on third-party clouds, or if you have your own backup setup that you trust for the `~/Documents` folder."

Default: Yes. The default is what gets picked if the user is unsure; the right default for a research user is the redundant, encrypted backup.

If the user says **No**, skip to Step E.

**Step D — GitHub setup (only if user said Yes in Step C).** No more questions about encryption — encrypted-when-pushed is implied by saying yes to backup, and asking again would re-introduce decision cost. Drive the setup:

1. Check `gh auth status`. If not authed, run `gh auth login` (interactive — user logs in in their browser, output streams back).
2. Ask the *one* unavoidable question: repo name. Default `holotype-archive`. Confirm org/account.
3. Run `gh repo create <org>/<name> --private` if the repo doesn't already exist.
4. Check for `git-crypt` on PATH. If missing, install via the detected package manager (`brew install git-crypt` on macOS; `apt`/`dnf`/`pacman` on Linux). Do this without asking — if the user checked `the terminal`, mention it ("installing git-crypt via brew, takes 5 seconds"); if not, just do it silently and mention it in the summary.
5. Walk the **key backup conversation** plainly. The key is *not* a scary data-loss bomb here — the canonical archive is the local copy, which stays readable forever; losing the GPG key only makes the *GitHub copy* unreadable. Frame it as belt-and-suspenders: "Let me save a backup of the scrambling key to your password manager / iCloud Keychain / a USB stick / a printable QR code — pick one (or several)." Detect candidates (iCloud Drive, 1Password CLI, USB volumes mounted under `/Volumes/`) and offer the available ones.

**Step E — Run init with everything else defaulted.** Call:

```bash
python scripts/init.py \
    --path <abs-path> \
    --remote-url <url-or-empty> \
    --remote-kind <github-private|none> \
    --compression auto \
    --sign-commits \
    [--auto-push] \
    [--encrypt-transcripts --i-understand-key-loss-means-data-loss]
```

If signing is on (it always is by default) and no GPG signing key is configured, **generate one silently** via `gpg --batch --gen-key` with an Ed25519 / no-passphrase config block bound to the user's `git config --global user.email`. Wire it into `git config --global user.signingkey` and `git config --global gpg.program`. Don't ask first — there's no real tradeoff (no-passphrase is required for the scheduled-backup job; the key file's protection is the same as `~/.ssh/id_*`, which the OS already protects).

If `zstd` is missing, install via the detected package manager without asking, then fall through to `--compression auto` (resolves to `zstd`).

**Step F — Retention bump + scheduled backup (no asking, just doing).** Run silently:

```bash
python scripts/configure-host-retention.py     # bumps cleanupPeriodDays to 36500 (never prune)
python scripts/install-launchd.py --archive <path>   # default 30-min interval
```

Neither has a real tradeoff to weigh. The retention bump literally means "don't let Claude Code delete files holotype is trying to read" — there's no scenario where the user wants holotype to silently lose data. The scheduled backup catches long sessions and crashed sessions that never fire a clean Stop hook — without it, the tool silently fails for the most common real-world session shapes.

If a Bash call gets blocked by an auto-mode classifier or permission prompt, fall back to "show the user the `!<command>` form and explain it" (only for users who checked `the terminal`; for non-terminal users, the LLM keeps trying through the host's permission mechanism rather than dumping shell commands on them).

**Step G — First ingest (streaming progress, not buffered tail).** Run:

```bash
python scripts/ingest.py --archive <path>
```

Stream output live — do **not** pipe through `| tail -50`. A non-programmer staring at a frozen screen for 2+ minutes will assume the tool hung. If your host CLI lets you mark a long-running command as background or stream-tracked, do so.

**Step H — Tailored summary.** Show what got done, and how to undo each piece. Two variants based on the calibration answer:

**For users who checked `git` + `terminal` (i.e. programmers)** — terse line items with shell undos:

```
✓ Archive: ~/Documents/holotype-archive (zstd-compressed)
✓ GPG signing on (key: ~/.gnupg, no passphrase) — useful for paper citations
✓ Retention bumped to 36500 days (revert in ~/.claude/settings.json)
✓ Launchd job io.holotype.ingest, 30-min interval (uninstall: python scripts/install-launchd.py --uninstall)
✓ Pushed to <remote-url>, encrypted at rest (git-crypt key backed up to <location>)
✓ Ingested 95 existing sessions (55 Claude Code + 40 Codex)

Browse the archive in your browser:  python scripts/browse.py
```

**For users who checked nothing** — plain sentences with "ask Claude" undos. Save the citation mention for last and make it optional, not a headline; most users don't think of chats as paper-citable:

> Done — here's what I set up on your Mac:
> - Your Claude Code conversations are being saved to `~/Documents/holotype-archive`. They get compressed to save space.
> - I told Claude Code to stop auto-deleting old conversations.
> - I set up an automatic backup that runs every 30 minutes in the background, so new conversations get saved without you having to remember.
> - I also set up an encrypted backup to a private GitHub repo (`<url>`) and saved the unscramble key to `<location>`. The local copy on your Mac is always readable; the GitHub copy needs the key.
> - 95 existing conversations were saved into the archive just now.
>
> **To browse what's saved**, just say "show me my holotype archive" — I'll open it in your browser.
>
> Side note: each saved copy has a tamper-proof signature, so you can prove you wrote it if you ever want to cite the chat in a paper. Ask if you ever need details. Otherwise it's invisible. To turn things off later, just ask: "turn off holotype's background backup," "restore Claude Code's default retention," etc.

When the user later says "show me my holotype archive" (or any variant — "browse the saved chats," "open my archive," etc.), run `python scripts/browse.py` as a backgrounded task and surface the printed URL to the user. The script starts a localhost-only HTTP server that renders the index and each session on demand — nothing is written to disk. Remind the user to press Ctrl-C in the terminal (or send a "stop browsing" message) when they're done.

Hand off cleanly. Don't ask any further questions unless the user does.

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
3. **Push policy is set at archive-init time, not at ingest time.** When `config.deposit.auto_push` is true (the default when a remote was configured at init), ingest pushes after a successful cycle — that's what the user opted into when they said "yes, back up to GitHub" at setup. When auto-push is false, only push when the user explicitly asks. Never *re-configure* push policy mid-stream without going back through the wizard; the privacy decision lives at init.
4. **The archive is git-tracked.** Every deposit is a commit with a deterministic message of the form `deposit: <project>/<session-id>` or `update: <project>/<session-id>`. Both the JSONL and its manifest live under `sessions/<project-dir>/<session-id>/`.
5. **The SQLite index at `<archive>/.holotype/index.sqlite` is derived data.** It speeds up search. It is NOT a source of truth. It can be deleted and rebuilt with `scripts/reindex.py` (planned) at any time.
6. **Live-file safety is built in.** `ingest.py` skips JSONLs modified in the last 2 seconds and re-checks mtime after reading. You should not need to add additional checks.

## Common operations

| Want to... | Run |
|------------|-----|
| First-time setup | Conduct wizard, then `python scripts/init.py --path <p> --remote-url <u> --remote-kind <k> --compression <auto|none|zstd>` |
| Estimate storage cost from existing host-CLI history | `python scripts/usage_estimate.py` |
| Check / bump host-CLI retention | `python scripts/configure-host-retention.py [--check-only]` |
| Install macOS scheduled-backup job (auto, at setup) | `python scripts/install-launchd.py --archive <p>` |
| Deposit new sessions (preferred — from the rsync backup) | `python scripts/ingest.py --source ~/Documents/Claude-Backups` |
| Deposit directly from live Claude Code state | `python scripts/ingest.py --source ~/.claude/projects` |
| Browse the archive in a browser (localhost server, no disk cache) | `python scripts/browse.py` |
| Find sessions matching text (FTS5) | `python scripts/search.py "<query>"` |
| Just the citation string for one session | `python scripts/cite.py <session-id> --citation-only` |
| Print the manifest for one session | `python scripts/cite.py <session-id> --manifest-only` |
| Produce a full citable bundle for one session | `python scripts/cite.py <session-id>` |
| Bundle many sessions for a paper's Zenodo deposit | `python scripts/paper_bundle.py --sessions a,b,c --out <dir> [--tarball]` |
| Recover when first push fails with "pack exceeds 2 GiB" | `python scripts/repush_chunked.py` |
| Pause / resume the macOS scheduled-backup job (for repo surgery) | `python scripts/install-launchd.py --pause` then `--resume` |
| Check whether a newer holotype release is available | `python scripts/update_check.py [--json]` |
| Verify the archive (per-file hashes + hash chain) | `python scripts/verify.py` |
| Verify a single session (per-file only) | `python scripts/verify.py <session-id>` |
| (Re)build or seal the hash-chain ledger | `python scripts/build_ledger.py` (ingest auto-bootstraps; only needed if the ledger is lost or to seal a pre-2.4.0 archive by hand) |
| Drop and rebuild the SQLite index from the canonical archive | `python scripts/reindex.py` |
| Load a past session's transcript into this chat | `python scripts/context.py <session-id>` then `Read` the printed path |
| Push to the configured remote (explicit, never auto) | confirm with user, then `git -C <archive> push` |

Session IDs are UUID prefixes — typically 8 hex chars are enough to disambiguate.

## Troubleshooting an existing install

If a user invokes the skill and reports that their archive seems frozen or out of date — *"nothing new has been saved in days,"* *"the browse view shows my live session as `live` and won't update,"* *"my conversations from yesterday aren't in the archive"* — diagnose in this order:

**1. Check the launchd job actually exists and is loaded:**

```bash
launchctl list | grep io.holotype.ingest
```

If absent, the job got uninstalled (perhaps during repo surgery via `--pause` that was never `--resume`d). Reinstall: `python scripts/install-launchd.py --archive <archive-path>`.

**2. Tail the stderr log for ingest errors:**

```bash
tail -30 ~/Library/Logs/holotype.err.log
```

The most common failure mode is `OSError: [Errno 11] Resource deadlock avoided: <archive>/.holotype/.lock`. **This is iCloud's lazy fileprovider holding the lock file in an unwritable state.** It happens when the archive is under `~/Documents/`, `~/Desktop/`, or any `~/Library/Mobile Documents/` path AND `defaults read MobileMeAccounts` shows `CLOUDDESKTOP / Enabled = 1`. The fix is to relocate the archive out of iCloud:

1. `python scripts/install-launchd.py --uninstall`
2. `mv <icloud-archive-path> ~/holotype-archive`  *(may take a while if iCloud is materializing files — let it finish)*
3. Update the pointer: `echo ~/holotype-archive > ~/.config/holotype/archive-path`
4. `rm -f ~/holotype-archive/.holotype/.lock`  *(remove the stuck lock so the new path starts clean)*
5. `python scripts/install-launchd.py --archive ~/holotype-archive`
6. Catch-up run: `python scripts/ingest.py --archive ~/holotype-archive`

Do not propose this fix without first confirming both signals (the EAGAIN/deadlock log line AND iCloud-Documents sync on) — moving the archive is a heavy operation and the user should know it's the right call.

**3. If launchd is loaded and logs are clean but nothing is being deposited:**

Check whether sessions are being correctly identified as live but the tick is racing the host CLI. `is_live_file()` skips files modified in the last 2 seconds. A persistently-active session that's written-to every <2s could starve. Workaround: bump the interval back from any longer value to the 1800s default, OR run `python scripts/ingest.py` manually to force a deposit at a quiet moment.

## Invocation model

This skill is **user-invocable only**. Implicit invocation is disabled in both implementations of the open agent skills standard:
- Claude Code reads `disable-model-invocation: true` from SKILL.md frontmatter.
- Codex reads `policy.allow_implicit_invocation: false` from `agents/openai.yaml`.

You will never auto-trigger this skill based on conversational cues. The user invokes it explicitly (`/holotype` in Claude Code, `$holotype` in Codex), and only then do you consult this file. If the user is doing paper-relevant work and an archival pattern would help them, you may *mention* that holotype exists — but you do not invoke it on their behalf.

Rationale: depositing transcripts into a hash-chained scientific archive is a deliberate provenance decision, not an opportunistic background task. The unfiltered safety-net backup is already handled separately (e.g., a `Stop`-hook rsync to `~/Documents/Claude-Backups/` on the original author's machine). Holotype is for *curated* deposits the user wants on the record. Conflating those two layers would undermine both.

## Verification without Claude

The archive is a plain git repository. A reviewer or auditor with no access to Claude Code can verify it using stock Unix tools. See [VERIFY.md](VERIFY.md) for the procedure. The skill is *convenience automation*; the archive itself is the scientific artifact.
