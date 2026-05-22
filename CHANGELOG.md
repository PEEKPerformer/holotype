# Changelog

All notable changes to `holotype`. Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-05-22

First public release. The substrate that makes Digital Discovery's LLM-DAS requirement satisfiable for the project's v2.0.0 paper, and the general-purpose scientific-provenance archive for agent-CLI sessions.

### Added

#### Sources
- **Claude Code source** — Claude Code top-level sessions plus nested subagent transcripts under `subagents/`. Reads from both the live `~/.claude/projects/` and the durable `~/Documents/Claude-Backups/` mirror, deduplicating on `(source, session_id)`.
- **Codex source** — Date-partitioned rollouts under `~/.codex/sessions/`. Parses the real wrapped Codex schema (`{timestamp, type, payload}` with outer types `session_meta` / `event_msg` / `response_item` / `turn_context`). Sensitivity boundary at `sessions/` — never reads the credential-bearing parent.
- **Google Antigravity source** — Plaintext JSONL under `~/.gemini/antigravity-cli/brain/<uuid>/.system_generated/logs/transcript_full.jsonl`. Ignores the encrypted `.pb` blob in `conversations/`. Sensitivity boundary at `brain/` — never reads `~/.gemini/oauth_creds.json` or `google_accounts.json` at the parent level.
- **`Source` ABC + `docs/ADDING_A_SOURCE.md`** — LLM-adaptable extension model. When invoked from an unrecognized host CLI, the skill instructs the host LLM to read the guide and write a new Source class against the documented contract rather than silently dropping that CLI's sessions.

#### Archive
- **Hash-chained git substrate** — every deposit is one commit with deterministic messages `deposit:` / `update:`. The archive is plain git; verification needs no holotype install.
- **Per-archive locked config** at `<archive>/.holotype/config.json` (compression mode, sign-commits flag, remote URL). Portable — moves with the archive folder.
- **Append-only by design** — once deposited, sessions are immutable. Re-ingesting the same session is a no-op (SHA-256 dedup); a session whose source grows produces an `update` commit. Manifests are never rewritten in-place except when `manifest_version` lags behind current.
- **Optional zstd compression at deposit** — `--compression auto` (default; uses zstd if the binary is on PATH, else plain JSONL) or `--compression {zstd|none}`. Locked for the life of the archive.
- **Two-track verification** — manifests record both the canonical uncompressed SHA-256 (`sha256`) and the on-disk compressed SHA-256 (`sha256_compressed`). Reviewers verify whichever they have tools for.
- **Optional GPG-signed deposit commits** via `init.py --sign-commits`. Off by default.
- **Auto-push after every ingest** when a remote is configured at init — `config.deposit.auto_push` defaults to `true` when `remote.url` is set, `false` for local-only archives. Privacy decision happens at remote-configuration time (wizard Step 3's explicit warning); auto-push honors that consent without forcing per-cycle re-confirmation. Pre-publication / IP-sensitive workflows can opt out via `--no-auto-push`. Push failures emit a warning but never fail the ingest — local deposits are committed first.
- **Live-file safety** — `ingest.py` skips JSONLs modified in the last 2 seconds and re-checks mtime after reading. Atomic writes throughout.
- **Concurrency safety** — exclusive `flock` at `<archive>/.holotype/.lock`. Concurrent ingests exit cleanly.

#### Manifest v4 (reproducibility fields)
- `project_git_state: {commit, commit_short, branch, dirty, remote, captured_at}` — the project repo's state at session-start (sourced from the host CLI's own header when available — Codex's `session_meta.git`) or at deposit-time (probed by holotype from the recorded `cwd`). `captured_at` distinguishes the two.
- `wall_clock_seconds` — `last_timestamp − first_timestamp`.
- `total_input_tokens` / `total_output_tokens` / `total_cache_creation_tokens` / `total_cache_read_tokens` — aggregated LLM token usage across the session. Claude Code feeds per-turn deltas via `message.usage`; Codex feeds cumulative readings via `token_count` events; the aggregator prefers a final cumulative reading when present.
- `compression` + `sha256_compressed` — present from v3 onward; documented under "Archive" above.
- `parent_session_id` + `source` — present from v2 onward.

`MessageInfo` gained `usage`, `session_metadata`, and `token_count_kind` so Sources can carry the new signals without source-specific code in the manifest scanner.

#### Scripts
- `scripts/init.py` — interactive-wizard-driven archive creation. Writes config, README, VERIFY.md, gitignore, and the initial commit.
- `scripts/ingest.py` — deposit any new sessions from registered Sources' default paths.
- `scripts/verify.py` — two-track hash-chain check, including tamper detection.
- `scripts/search.py` — FTS5 query over the SQLite-backed index.
- `scripts/cite.py` — produce a citable bundle for one session (transcript + manifest + cite.txt + render.md).
- `scripts/paper_bundle.py` — package many sessions into one Zenodo-ready deposit with master `BUNDLE_MANIFEST.json` and optional tarball + SHA-256 sidecar.
- `scripts/context.py` — print the path of a session's transcript (decompressing to a cache file when the archive is compressed) so the host LLM can `Read` it directly.
- `scripts/reindex.py` — drop and rebuild the SQLite index from the canonical git-tracked manifests.
- `scripts/usage_estimate.py` — pre-setup storage projection from host-CLI source dirs.
- `scripts/install-launchd.py` — opt-in macOS 30-minute background tick.
- `scripts/configure-host-retention.py` — bump host CLI's `cleanupPeriodDays` to effectively-never.
- `scripts/selftest.py` — ~30 end-to-end checks covering every source, both compression modes, paper bundle, and verify.

#### Index
- SQLite + FTS5 derived index at `<archive>/.holotype/index.sqlite`. Schema version 4 (sessions / messages / messages_fts). Rebuildable on demand; never the source of truth.
- **Perf**: ingest holds a single SQLite connection across the cycle (vs. one open-per-session in earlier drafts), uses `executemany()` for batch line inserts, batches `COMMIT` per 50 sessions, and sets `synchronous=NORMAL` + 64 MiB cache. Skips the FTS rebuild when the on-disk transcript bytes haven't changed — the dominant case for a manifest-version migration. Together these turn a previously-multi-hour bulk re-ingest into minutes for a 6000-session archive.
- **Combined "migrate:" commits for manifest-only refreshes.** When a manifest-version bump fires the re-process path but the on-disk transcript bytes are unchanged, every affected session would have produced its own "update:" commit under the old per-session-commit rule — for a 6000-session migration that's 6000 commits saying nothing useful. The new behavior emits exactly one `migrate: refresh N manifest(s) to schema v<V> [<sources>]` commit covering the whole batch. Genuine deposits and content updates still get per-session commits as before — only the "schema bump, bytes identical" case bundles. `git log sessions/<X>/<Y>/manifest.json` still finds the migration; the file shares the commit with its siblings instead of holding its own.
- The `messages.raw_json` column was removed in schema v4: it duplicated the canonical JSONL on disk and ballooned the index multi-GB without serving any query the FTS table didn't already cover.

#### Skill
- `SKILL.md` with `disable-model-invocation: true` (Anthropic standard) and `agents/openai.yaml` with `policy.allow_implicit_invocation: false` (OpenAI standard). User-invocable only; the skill is never auto-triggered.
- Conducted-by-LLM setup wizard with explicit consent steps for storage cost, compression mode, remote URL, and host-CLI retention bumps.
- Cross-CLI portability per the [agentskills.io](https://agentskills.io) open standard.

#### Project hygiene
- `pyproject.toml` declaring Python >=3.11, MIT license, stdlib-only deps.
- `LICENSE` (MIT).
- `.github/workflows/selftest.yml` running selftest on ubuntu-latest and macos-latest with Python 3.11 and 3.12.

### Design notes

- **No filtering of transcript content, ever.** `claude-vault` and similar tools strip tool calls as "noise." holotype exists because that tradeoff is wrong for scientific reproducibility. The tool calls *are* the experimental record.
- **No silent network behavior.** Setup is an explicit conversation about remote URL choice. Push to remote is never automatic — every push is per-action user-confirmed.
- **No auto-invocation.** Depositing data into a scientific archive is a deliberate user decision, not an opportunistic background task. Brenden's `Stop`-hook rsync to `~/Documents/Claude-Backups/` is the "everything, automatically" layer; holotype is the curated layer on top.
- **Verify with stock Unix tools.** The `VERIFY.md` shipped inside every archive (and inside every `paper_bundle.py` deposit) uses only `shasum`, `jq`, `git`, and optionally `zstd`. A reviewer five years from now needs no holotype install.

### Known limitations

- The macOS launchd background tick has no Linux/Windows equivalent shipped (Linux: write a user systemd unit; Windows: Task Scheduler). The core skill works cross-platform; only the auto-tick is macOS-only.
- Antigravity Source's schema was captured from a single observed session — additional record types may emerge as the CLI matures. The "not gospel" note in `SKILL.md` encourages the host LLM to verify on-disk reality before trusting the documented schema.
- `claude_code` model identifiers depend on Anthropic's per-turn `message.model` field. If that field disappears in a future Claude Code release, `manifest.models` will be empty until the parser is updated.
