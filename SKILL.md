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

> "The archive will contain verbatim Claude Code / Codex / Antigravity transcripts, including any file contents, command output, environment details, and tool results the agent saw during sessions. If you add a git remote, that data will be pushed to that remote when you sync — encrypted or not depending on your next choice. Choose carefully."

Then offer the following four options, presented in this order so the recommended option is first:

- **GitHub private repo (encrypted)** — *Recommended for paper-citable archives.* Transcripts are filtered through `git-crypt` before push, so GitHub stores only encrypted blobs. Manifests stay plaintext on the remote (session IDs, timestamps, models, project paths, token counts are visible there). This is the 3-2-1-backup-rule answer for scientific work: local archive + offsite encrypted backup + key backed up separately. **Requires `gh` authed + `git-crypt` installed + GPG key + explicit key-loss acknowledgment.**
- **GitHub private repo (plain)** — Convenient if you fully trust GitHub with your transcript content. No extra dependencies beyond `gh` auth. Faster setup than the encrypted option, but the remote sees everything Claude saw.
- **Local only** — No remote. Easiest install (no external dependencies), safest from third-party-data-exposure concerns, but single-disk-failure = data loss. Pick this if you don't yet have publishing aspirations for these sessions, or if institutional policy bans storing pre-publication data on third-party clouds in any form.
- **Self-hosted / Synology / Other URL** — Gitea / Forgejo / GitLab CE / bare repo on a server you control, or a NAS at home, or any other git remote URL. The encryption choice at Step 4a applies here too — recommended if the server isn't fully under your control (institutional GitHub Enterprise, shared NAS).

The encrypted-GitHub option is recommended because it covers the most common scientific-archive failure modes (single-disk loss + needing offsite backup for paper citations) without leaking transcript content to the remote. If the user picks it, the wizard chains through `gh repo create` + `git-crypt` install + key generation as needed — none of which happens silently. If the user picks any other option, Step 4a will still independently ask about encryption (it's a separate decision from "do I want a remote").

**Step 4 — If a remote was chosen, get the URL.** For GitHub, also ask for org/account and repo name and offer to `gh repo create --private` it on the fly (but only if the user says "yes" — never silently).

**Step 4a — Encryption-before-push (only if a remote was chosen).** If the user picked **GitHub private repo (encrypted)** at Step 3, encryption is already implied — skip the "y/N" question and go straight to the data-loss confirmation below. Otherwise (any other remote option in Step 3), ask:

> "Filter transcripts through `git-crypt` before push, so the remote stores only encrypted blobs? (y/N — default no)
>
> Choose yes if you don't fully trust the remote with the raw transcript content — e.g. an institutional GitHub Enterprise you share with non-collaborators, an S3-backed git provider, or a Synology NAS that other family members can access. Manifests stay plaintext on the remote (session IDs, timestamps, models, project paths, token totals are still visible there) — only the transcript file contents are encrypted.
>
> **Data-loss risk**: if you lose the GPG key, every encrypted deposit becomes unrecoverable — including any paper-cited session. The key lives at `.git/git-crypt/keys/default` inside the archive and is NOT pushed to the remote. You MUST export and back it up to at least two locations (password manager + offline USB, paper QR backup, trusted collaborator) before depending on the archive for citation. Holotype cannot recover lost data."

If the user says yes (or if encryption is implied from the Step 3 recommendation):
- Verify `git-crypt` is on PATH (`command -v git-crypt`). If missing, offer to install via the same per-OS package manager the zstd step uses (`brew install git-crypt` / `apt install git-crypt` / `dnf install git-crypt` / `pacman -S git-crypt`). Require explicit confirmation before running the install.
- Confirm the data-loss acknowledgment by repeating: *"You understand that losing the GPG key means the archive's encrypted deposits cannot be recovered. Holotype cannot recover lost data. Proceed? (yes/no)"* — only proceed on an unambiguous "yes."
- The `init.py` invocation will need both `--encrypt-transcripts` and `--i-understand-key-loss-means-data-loss`.
- After init completes, point the user at `<archive>/HOW_TO_BACK_UP_YOUR_KEY.md` and remind them to back up the key *before* the first ingest.

If the user says no (or this step is skipped because no remote was chosen): proceed without encryption.

**Step 4b — Auto-push policy (only if a remote was chosen).** Ask:

> "Auto-push to `<url>` after every ingest? (Y/n — Recommended)
>
> Recommended (Y) for most users: the archive stays in sync with the remote without you having to remember to push. Privacy decision already made when you picked the remote — transcripts will be pushed there.
>
> Decline (n) if your transcripts may contain pre-publication embargo data, IP-sensitive lab measurements, or other material you want to gate on per-push review. With auto-push off, you'd run `git -C <archive> push` manually when you're ready to publish."

Default is yes. The privacy warning was already shown at Step 3; this step is about *when* the user wants the push to happen, not *whether* the remote can see the data.

**Step 4c — GPG-signed deposit commits.** Recommended for any archive destined for a paper's Zenodo deposit — signing adds a tamper-evident layer that survives cloning. Ask:

> "GPG-sign every deposit commit? (Y/n — Recommended for paper-citable archives)
>
> Adds the `--sign-commits` flag to init, setting `config.deposit.sign_commits=true` so every future deposit is GPG-signed."

If the user says yes, check whether a signing key is configured:

```bash
git config --global user.signingkey
```

- **A key is configured**: tell the user "Signing key already configured: `<keyid>`. Proceeding with signing on." and continue.
- **No key configured**: offer to generate one — symmetric to the zstd/git-crypt install offers:

  > "No GPG signing key is configured. I can generate an Ed25519 key for you (modern, small, fast). The passphrase decision matters:
  >   * **No passphrase** (recommended for an automated archive — the launchd / systemd tick can't answer a passphrase prompt). The key file on disk is the only thing protecting the signature.
  >   * **Passphrase** — gpg-agent caches it across commits within a session, but background-tick ingests will fail until you unlock.
  >
  > Generate now? (Y/n)"

  If yes, ask one more question — whose identity to bind to the key:

  > "Bind the key to which email? Defaults to your `git config --global user.email` value: `<email>`."

  Then generate via `gpg --batch --gen-key` with a config block:

  ```
  %no-protection
  Key-Type: EDDSA
  Key-Curve: ed25519
  Name-Real: <user.name>
  Name-Email: <email>
  Expire-Date: 0
  %commit
  ```

  Extract the new key's fingerprint and wire it in:

  ```bash
  git config --global user.signingkey <FINGERPRINT>
  git config --global gpg.program "$(command -v gpg)"
  ```

  Test it produces a signature before continuing:

  ```bash
  echo test | gpg --clearsign --local-user <FINGERPRINT> > /dev/null
  ```

  Then offer (optional, for the GitHub "Verified" badge): "Upload the public key to GitHub? Requires the `write:gpg_key` OAuth scope — I can refresh your `gh auth` for it, or you can skip and run `gh gpg-key add` later." If yes, run `gh auth refresh -s write:gpg_key` then `gpg --armor --export <FINGERPRINT> | gh gpg-key add -`.

If the user declines signing entirely, init proceeds with `sign_commits=false`. The archive is still hash-chained — signing is an *additional* tamper layer, not a substitute for the per-session SHA-256.

**Step 5 — Confirm before writing.** Show a summary as a structured table (Markdown table, or aligned text in CLIs that don't render Markdown):

```
┌─────────────────────────────┬──────────────────────────────────────────────────────┐
│           Setting           │                       Value                          │
├─────────────────────────────┼──────────────────────────────────────────────────────┤
│ Archive path                │ <abs-path>                                           │
│ Compression                 │ <auto|none|zstd> (resolves to <zstd|none>)           │
│ Remote                      │ <url-or-none>                                        │
│ Encrypt transcripts at push │ <yes|no> [(git-crypt <version>)]                     │
│ Key-loss ack                │ <Confirmed | n/a>                                    │
│ Auto-push after ingest      │ <yes|no>                                             │
│ GPG-signed commits          │ <yes|no> [(key <keyid>)]                             │
└─────────────────────────────┴──────────────────────────────────────────────────────┘
```

Then ask "Proceed?" Wait for explicit confirmation. The user can still back out and revise any answer.

**Step 6 — Run init.** Call:
```bash
python scripts/init.py --path <abs-path> --remote-url <url-or-empty> --remote-kind <github-private|self-hosted|synology|other|none> --compression <auto|none|zstd> [--sign-commits] [--auto-push|--no-auto-push] [--encrypt-transcripts --i-understand-key-loss-means-data-loss]
```

The script writes `<archive>/.holotype/config.json` (including the *resolved* compression mode — `auto` is replaced with the concrete choice — and the resolved auto-push flag), drops `VERIFY.md` + `README.md` into the archive, makes the initial commit, and writes a pointer file at `~/.config/holotype/archive-path` so future sessions can find the archive. If `--compression zstd` is forced but zstd isn't on PATH, init exits 2; `--compression auto` falls back to plain JSONL with a notice. Auto-push defaults to yes when `--remote-url` is set, no otherwise.

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

**Step 7a — Back up the git-crypt key (only if encryption is on).** Skip this step if `config.deposit.encrypt_transcripts` is false. Otherwise, drive the key-backup conversation now — *before* the launchd tick is installed and *before* the first ingest pushes encrypted blobs to the remote. The key was created by `init.py` at `<archive>/.git/git-crypt/keys/default`. It exists nowhere else. If this disk dies before the key is backed up, every encrypted deposit on the remote becomes permanently unrecoverable.

State the situation explicitly:

> "init created the git-crypt key at `<archive>/.git/git-crypt/keys/default`. That's the ONLY copy. Before the first ingest pushes encrypted blobs to GitHub, you should back it up to at least one offsite location. Where would you like to export it?"

Detect candidate destinations on the user's machine BEFORE offering options:

```bash
# macOS iCloud Drive root
test -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs" && echo iCloud_root
# Desktop & Documents sync indicator (macOS iCloud Drive option)
test -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Documents" && echo iCloud_documents
# Other common cloud syncs
test -d "$HOME/Dropbox" && echo dropbox
test -d "$HOME/Google Drive" -o -d "$HOME/Library/CloudStorage/GoogleDrive-${USER}@gmail.com" && echo gdrive
test -d "$HOME/OneDrive" && echo onedrive
# 1Password CLI
command -v op >/dev/null && echo op_cli
```

Offer destinations in this rough priority, presenting only those whose detection succeeded plus the manual-path option:

- **1Password CLI (`op`)** — best for archives destined for paper citation; the key is stored as a secure note. After export, the local file should be shredded. Run: `git-crypt export-key /tmp/holotype-keyfile.key && op item create --category=password --title="holotype git-crypt key (<archive-name>)" --vault=Private holotype_key="$(base64 -i /tmp/holotype-keyfile.key)" && rm -P /tmp/holotype-keyfile.key`.
- **iCloud Drive** — `~/Library/Mobile Documents/com~apple~CloudDocs/holotype-keyfile.key`. macOS encrypts in transit + at rest in iCloud. Counts as offsite automatically.
- **Dropbox / Google Drive / OneDrive** — same idea, the user's chosen cloud sync.
- **~/Documents/holotype-keyfile.key** — *only if iCloud Drive's "Desktop & Documents" sync is on* (detect via `iCloud_documents` above). Surface this explicitly: "Your `~/Documents/` is iCloud-synced; exporting here is automatically offsite." If iCloud Documents sync isn't on, downgrade this option (`~/Documents/` becomes "local disk only — move it elsewhere later").
- **Custom path you provide** — the user types a path (e.g. an external USB drive mountpoint).
- **Skip — I'll back it up myself** — last resort. Print the exact command for the user to run later: `cd <archive> && git-crypt export-key /path/to/your/backup-keyfile.key`.

Run the export with the correct invocation (git-crypt is a top-level command, not a `git` subcommand):

```bash
cd <archive>
git-crypt export-key <destination-path>
chmod 0600 <destination-path>
```

After export, verify the file mode is 0600 and confirm whether the destination is genuinely offsite. If the destination is on the local disk only (not in any recognized cloud-sync location and not on a separate physical volume), surface this to the user honestly:

> "Exported to `<path>`. This is on the same disk as the archive — it is NOT a real backup. If this disk dies, both the key and the encrypted remote become inaccessible. Move it to a cloud-synced folder, external drive, or password manager before depending on the archive for citation."

If the destination IS in a recognized cloud-sync location (or 1Password CLI), confirm:

> "Exported to `<path>` — this location is offsite-synced, so the key now exists on multiple physical devices. You're good. Refer to `<archive>/HOW_TO_BACK_UP_YOUR_KEY.md` for adding collaborators or a second backup destination."

Either way, point the user at the archive's `HOW_TO_BACK_UP_YOUR_KEY.md` for the canonical reference (it covers `git-crypt export-key` syntax, recovery testing via `git clone` + `git-crypt unlock`, and adding collaborators via `git-crypt add-gpg-user`).

**Step 8 — Background-tick opt-in (macOS only).** Ask:

> "Claude Code / Codex sessions often run for hours without explicit close. A 30-minute background tick will catch sessions that the Stop hook misses by running `ingest.py` on a timer.
>
> Push behavior follows your auto-push config: with auto-push **on** (your choice), the tick will deposit AND push after each cycle. With auto-push **off**, the tick only deposits locally; pushes remain manual.
>
> Install? (Y/n)"

If yes:
```bash
python scripts/install-launchd.py --archive <abs-path>
```

If on Linux or Windows, skip this step and tell the user the equivalent can be set up later via a user systemd unit (Linux — see `docs/LINUX_SYSTEMD.md`) or Task Scheduler (Windows).

**Step 9a — First-ingest scale warning.** *Before* launching the first ingest, surface the realistic cost. Re-run `scripts/usage_estimate.py --json` to get current numbers and tell the user something like:

> "Your existing host-CLI history is ~N session files totaling ~X GB. The first ingest will:
>   * Hash each transcript (fast — Apple Silicon SHA-NI / x86 SHA-NI)
>   * Compress each (zstd, fast)
>   * Encrypt each via git-crypt (if encryption is on)
>   * Commit each to the archive
>   * Push to the remote at the end (if auto-push is on — pushes ~Y GB of encrypted blobs)
>
> Realistic ETAs (calibrate from the matrix below — choose the row that matches your config):"

```
┌──────────────────┬────────────┬────────────┬────────────────────────────────────────────────┐
│   Configuration  │ Per-session│ Bulk-init  │                     Notes                      │
├──────────────────┼────────────┼────────────┼────────────────────────────────────────────────┤
│ Plain, no sign   │ ~3-5 min   │ ~1-2 min   │ Dominated by SQLite + git plumbing.            │
│ Plain + sign     │ ~25-35 min │ ~3-5 min   │ Per-session: ~300 ms GPG sig × N commits.      │
│ Encrypted        │ ~30-40 min │ ~20-30 min │ git-crypt forks per-file at `git add` time.    │
│                  │            │            │ Bulk-initial does NOT save the filter cost.    │
│ Encrypted + sign │ ~50-90 min │ ~20-30 min │ Encryption dominates; signing is one extra sig │
│                  │            │            │ in bulk-initial mode.                          │
└──────────────────┴────────────┴────────────┴────────────────────────────────────────────────┘
```

> Numbers above are for ~6000-session archives on an M-series Mac. Scale linearly with session count. Subsequent (steady-state) ingests are fast in all configurations — only new sessions get processed.
>
> *Why encrypted bulk-initial isn't faster*: git-crypt invokes one fork+exec of its clean filter per file at `git add` time. The bulk-initial combined commit doesn't avoid this per-file filter cost — it only avoids the per-commit GPG signing cost. A future v1.2 may use git's process-filter protocol to make encrypted bulk-initial much faster; for now, plan for the table above.

Then offer the user a choice about commit topology for THIS first ingest:

> "Per-session commits or one combined commit for the initial backfill?
>   * **Per-session** (default): one git commit per deposited session. Granular `git log sessions/X/Y/Z/` history. Slow with signing on (each commit signs separately).
>   * **Bulk-initial**: one combined `bulk-initial: N sessions ingested` commit for all backfilled sessions. Loses per-session ordering inside the initial backfill (they all share one commit). Saves the per-commit-sign cost (one sig instead of N). Does NOT save the per-file encryption cost.
>
> Recommended for a fresh setup with hundreds-plus sessions to backfill AND signing on: **bulk-initial**. For smaller archives or when signing is off: **per-session** is fine."

If they pick bulk-initial, pass the `--bulk-initial` flag.

**Step 9b — First ingest, with live progress.** Run the initial deposit AS A BACKGROUND TASK and arm a Monitor that emits per-milestone events plus one completion event. Do NOT just `Bash run_in_background` and go silent — for a 5-30 minute ingest the user wants progress feedback.

Kick off the ingest in the background:

```bash
python scripts/ingest.py --archive <abs-path> [--bulk-initial]
```

Immediately arm a Monitor with the following script. Counts deposited *manifests* (not commits) so the progress signal works for both per-session and `--bulk-initial` modes (manifests are written eagerly by `deposit_one()`; commits are deferred under `--bulk-initial`):

```bash
archive=<abs-path>
start_ts=$(date +%s)
start_files=$(find "$archive/sessions" -name manifest.json 2>/dev/null | wc -l | tr -d ' ')
last_milestone=0
while pgrep -f "scripts/ingest.py" > /dev/null; do
  cur=$(find "$archive/sessions" -name manifest.json 2>/dev/null | wc -l | tr -d ' ')
  delta=$((cur - start_files))
  if [ "$delta" -ge "$((last_milestone + 500))" ]; then
    elapsed=$(( $(date +%s) - start_ts ))
    rate=$((delta * 60 / (elapsed + 1)))
    echo "$(date +%H:%M:%S) milestone: manifests=$cur (+$delta) rate=${rate}/min elapsed=${elapsed}s"
    last_milestone=$((last_milestone + 500))
  fi
  sleep 15
done
final=$(find "$archive/sessions" -name manifest.json 2>/dev/null | wc -l | tr -d ' ')
elapsed=$(( $(date +%s) - start_ts ))
echo "$(date +%H:%M:%S) COMPLETE: final manifests=$final (+$((final - start_files))) elapsed=${elapsed}s"
```

Set the Monitor `timeout_ms` to 3600000 (the maximum, 60 min) for a typical first ingest. Pick `description` like "holotype first-ingest progress (one event per ~500 deposits)" — that string appears in every notification the user sees.

Each milestone event tells the user "we're alive, depositing at N/min, at K total." The COMPLETE event signals the cycle is over. When the Monitor notification fires, run the validation checks (Step 9c).

For *subsequent* ingests (steady-state, after the first one) the Monitor is unnecessary — those typically finish in seconds-to-minutes for a normal daily delta. Background-tick ingests don't need a Monitor either; they're fire-and-forget and write to `~/Library/Logs/holotype-ingest.{out,err}` (or the launchd-configured paths).

**Step 9c — Post-ingest validation.** After the Monitor's COMPLETE event fires, sanity-check the result:

```bash
# Counts by source
python -c "
from holotype.archive import iter_all_sessions
from pathlib import Path
counts = {}
for _, m in iter_all_sessions(Path('<abs-path>')):
    counts[m.get('source','?')] = counts.get(m.get('source','?'),0) + 1
print('by source:', counts)
"

# Full hash-chain verify
python scripts/verify.py --archive <abs-path>

# Confirm the remote has the bulk-initial commit
git -C <abs-path> log --oneline origin/main..HEAD 2>/dev/null || \
  git -C <abs-path> log --oneline -3
```

Report the per-source counts, the verify summary, and (if a remote is configured) confirm the push landed. This is the canonical "setup done, archive is healthy" moment.

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
3. **Push policy is set at archive-init time, not at ingest time.** When `config.deposit.auto_push` is true (the default when a remote was configured at init), ingest pushes after a successful cycle — that's what the user opted into at the wizard's Step 4b. When auto-push is false, only push when the user explicitly asks. Never *re-configure* push policy mid-stream without going back through the wizard; the privacy decision lives at init.
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
