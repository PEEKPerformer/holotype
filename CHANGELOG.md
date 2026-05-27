# Changelog

All notable changes to `holotype`. Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.5] — 2026-05-27

A 3-day-uptime health check on the maintainer's own archive revealed that the v2.2.0 `manifest_version` 4→5 backfill had silently overwritten `env.claude_code_version` on 96% of manifests. This release fixes the regression going forward and ships a recovery script that restores the lost fields from git history.

### The regression

When `build_manifest()` re-runs on an existing deposit (which is what a `manifest_version` bump triggers), it was always re-probing env fresh — including `claude_code_version`. From a `launchd`-tick context the probe returns `null` because the binary isn't reachable the way it is at user-invoked deposit time. The fresh `null` then overwrote the real version string the original deposit had captured. Transcript SHA-256 hashes were unchanged (the regression is purely metadata), but the forensic claim about *which version of the host CLI saw this session* was silently zeroed out on every existing session.

Same loss would have repeated on every future `MANIFEST_VERSION` bump unless fixed.

### Fixed forward (`holotype/manifest.py` + `scripts/ingest.py`)

- `build_manifest()` gains an `original_deposited_at: str | None = None` parameter. When supplied, the manifest's `deposited_at` is preserved instead of replaced with `datetime.now()`.
- `scripts/ingest.py` now detects pure-migration ingests (`prior_sha == new_sha and prior_version != MANIFEST_VERSION`) and:
  - Preserves `env` from the prior manifest (the original deposit-time env capture)
  - Preserves `deposited_at` from the prior manifest
  - Preserves `project_git_state` from the prior manifest when present (was captured closer to session-start)
- Real content updates (`prior_sha != new_sha`) re-capture env fresh — that env corresponds to the moment of the update, which is the right semantics.

### Recovery (`scripts/recover_v5_env.py`)

New script. For each session manifest in the archive, walks `git log --diff-filter=A --reverse` to find the commit that originally introduced it, extracts the original env block from that commit, and merges any non-null fields back into the current manifest. Idempotent — runs over an already-recovered archive as a no-op. One combined `recover:` commit at the end.

Ran against the maintainer's 3,338-session archive at release time:
- **2,972 manifests recovered** (env.claude_code_version restored from `null` to e.g. `"2.1.149 (Claude Code)"`)
- 241 already healthy
- 116 had original `null` too (env probe failed at original deposit; genuinely unrecoverable)
- 9 lacked git history (couldn't find the first commit; degenerate sessions)

### Selftest

- Pure-migration test now plants a sentinel `claude_code_version` and `deposited_at` in the pre-migration manifest, then asserts both survive the v3→v5 migration. Catches the regression.
- New step: run `recover_v5_env.py` against a corrupted manifest (env nulled out and committed) and assert it gets restored to the value from the first commit. Confirms recovery works end-to-end.

### Notes

- No schema bump (still `manifest_version: 5`); only the *contents* of the env block changed.
- Hash chain unchanged across recovery — `verify.py` still reports `3,338/3,338 verified clean`.
- Encryption-at-rest on the remote independently re-verified during the same health check: GitHub-stored transcript blobs begin with the `\x00GITCRYPT\x00` magic header followed by AES-encrypted payload. Manifests stay plaintext on the remote (by design, for searchable metadata).

---

## [2.2.4] — 2026-05-24

Paper bundles now ship a reviewer-oriented `README.md` at the root, templated from `BUNDLE_MANIFEST` and framed in the language of journal reproducibility policies for LLM-assisted research (the target audience: a Digital Discovery / *Use of large language models in research* reviewer who has never seen holotype).

### Added

- **`scripts/paper_bundle.py` writes `README.md`** alongside the existing `VERIFY.md` + `BUNDLE_MANIFEST.json` + `index.html`. The README is auto-populated with:
  - Session count and paper title/DOI (if provided)
  - Unique list of models used across the deposit (deduplicated, sorted, `<synthetic>` sentinel filtered)
  - Generation date range (earliest first-timestamp to latest last-timestamp across sessions)
  - Explicit map to journal-required artifacts: *"log files including the inputs and outputs"* → `transcript.jsonl`; *"model identifier"* → `manifest.models`; *"generation date"* → `manifest.first_timestamp` / `last_timestamp`
  - Instruction to open `index.html` first (the v2.2.0 reader serves the no-LLM reviewer)
  - Pointer to `VERIFY.md` for hash verification with stock Unix tools
  - Honest "what this deposit does NOT include" section (external file states, referenced git repos)

The README is the bundle's first-impression artifact when opened on Zenodo or by a peer reviewer. It explains itself without requiring familiarity with the tool.

### Selftest

New assertion that paper bundles include `README.md`, that it carries the journal-oriented framing string, the Models/Generation-date sections, the paper title passed at bundle time, and references to both `index.html` and `VERIFY.md`.

---

## [2.2.3] — 2026-05-24

Closes the iCloud-lockfile failure mode that the v2.2.2 "live" badge surfaced on the maintainer's own machine.

### Discovered

Brenden's archive at `~/Documents/holotype-archive/` had the launchd tick failing silently since the v5 backfill ran. Every subsequent tick errored with:

```
OSError: [Errno 11] Resource deadlock avoided: <archive>/.holotype/.lock
```

The cause: iCloud's "Desktop & Documents Folders" sync was on, and its lazy fileprovider held the lock file in an unwritable state. The user never knew until the v2.2.2 live badge revealed 5 sessions with 15-hour lag — not because conversations were active, but because the tick was broken.

### SKILL.md changes

- **Wizard Step B now actively refuses iCloud-synced paths.** Detects via `defaults read MobileMeAccounts` for `CLOUDDESKTOP / Enabled = 1` AND the candidate path being under `~/Documents/`, `~/Desktop/`, or `~/Library/Mobile Documents/`. Default suggestion changed from `~/Documents/holotype-archive` to `~/holotype-archive` (top-level home, outside any sync set).
- **New "Troubleshooting an existing install" section.** When a user reports a frozen archive, the LLM is now instructed to: (1) check `launchctl list` for the job, (2) tail `~/Library/Logs/holotype.err.log` for the EAGAIN/deadlock signature, (3) if found, propose the relocate-out-of-iCloud migration explicitly. Documents the 6-step relocation procedure (uninstall launchd → mv → update pointer → rm stale .lock → reinstall launchd → catch-up ingest).
- **Important guardrail:** "Do not propose the migration speculatively — confirm both signals first." Moving a multi-GB archive is heavy and the user should know it's the right call.

### Why no code-level enforcement

Detecting iCloud-sync state robustly in Python requires probing `defaults` (macOS-specific), checking xattrs (`com.apple.fileprovider.*`), or watching for the `@` mark on directory listings — none of which is portable. The right place to enforce is in the LLM-driven wizard where natural language judgment is cheap. `scripts/init.py` could add a refusal for macOS users in a future release, but the policy belongs in SKILL.md first.

### No code changes

Selftest unchanged. No version-incompatible behavior. v2.2.2 archives keep working identically — this is pure operator-knowledge.

---

## [2.2.2] — 2026-05-24

Closes the perception-of-freshness gap in the reader: when a session is currently being written to, the browse UI says so.

### Added

- **"Live" badge on session cards.** The browse server now stats `manifest.source_path` against `manifest.deposited_at` per session at index-render time. When the source JSONL has been modified since the last deposit, the card gets a `session-card.live` left-border accent and an inline `● live` badge next to the project name. Confirmed against a real archive: my current interactive session (mid-conversation, source mtime ahead of deposit) shows up flagged; idle sessions don't.
- **Live banner in session views.** When the user clicks into a live session, the rendered view shows an amber banner above the transcript: *"The source file at &lt;path&gt; has been modified since this snapshot was ingested at &lt;timestamp&gt;. The conversation may have grown — the next ingest tick will capture it."* Tells the user explicitly that they're reading a snapshot, not the current state.

The detection is cheap (one `stat()` per session at index-render time, ~300 ms for a 3,000-session archive), localhost-only, and gracefully degrades — missing or unreachable source paths just don't get the live flag rather than erroring.

### Why

[v2.2.1 changelog] honest take: holotype's safety story for growing JSONLs is solid (2-second grace window in `is_live_file()`, `mtime`-around-read in `read_with_stable_check()`, `update:` commits preserve full history), but the user-facing UX gave no signal that a session was still being written. A 30-minute background tick means a 3-hour conversation can be up to 30 min stale in the archive at any moment — without an indicator, the user has no way to know "this is a snapshot" vs "this is final."

### Selftest

Synthetic assertion that bumping a source file's mtime ahead of its `deposited_at` produces both `class="live-badge"` and `session-card live` in the rendered index, plus a `live-banner` with "may have grown" text in the session view.

---

## [2.2.1] — 2026-05-24

Closes the gaps from v2.2.0 — visual validation in a real browser, plus the items I admitted I'd shipped without verifying.

### Bug fixes

- **`permission-mode` and `ai-title` are now recognized Claude Code meta record types.** v2.2.0 left these falling through to "(record shape not recognized)" in the viewer. Found 511 of them in a single real session (256 permission-mode + 255 ai-title). Now classified as meta and folded into the leading-meta collapsed block where they belong.
- **`_project_basename` now handles Claude Code's dash-encoded paths.** Search results were rendering project headers as `-Users-brendenferland-Git-ResistaMet-GUI` because the basename helper only split on `/`. It now treats both POSIX paths (`/Users/brendenferland/Git/foo` → `foo`) and dash-encoded paths (`-Users-brendenferland-Git-foo` → `foo`) correctly.
- **Session-view header label was inconsistent.** Card UI says "project"; header said "project path". Now both say "project".
- **Paper-bundle `index.html` rendered with no banner.** v2.2.0's `render_index` refactor parameterized the banner with `banner: str | None = None` default — which dropped the explanatory banner from the paper-bundle path. Restored as the default value so paper bundles still get the "Open any session below to read the rendered transcript…" banner, with the browse server overriding to its own text.

### Selftest

- **Search assertion tightened.** Was: "either there are results or there's a no-results note." Now: demands real `<div class="search-result">` blocks AND `<mark>` highlighting from FTS5's `snippet()` function, plus an empty-query path assertion.
- **v4→v5 backfill validated end-to-end.** The existing manifest-version-mismatch migration test now also asserts that the post-migration manifest has `first_user_message_excerpt` populated with text matching one of the fixture openers. This is the closed-loop confirmation that existing v4 archives will surface excerpts on cards after their next ingest cycle.

### Visual validation

End-to-end inspection in Chrome against the real 3,056-session archive: title is "holotype — your archive"; cards show project name + human date + tools/thinking badges + output-token signal + secondary id/repo/subagent line; subagent grouping renders (24 `<details>` groups); leading-meta collapsed by default; 194 long tool results collapsed; user prompts styled blue (not dim grey); dark mode renders cleanly under `prefers-color-scheme: dark`; `/search?q=memory` returns 50 results with 103 `<mark>` highlights and basename-displayed project headers.

---

## [2.2.0] — 2026-05-23

The reader is now usable at archive scale. Three categories of work, motivated by visually inspecting the v2.1.0 reader against a 3,056-session real archive.

### Bug fixes

- **`viewer.py` read the wrong manifest field for the project path.** Every session view said `project path: ?` because the renderer looked for `project_path` but manifest v4+ uses `project_dir_decoded`. Browse session cards also fell through to UUIDs.
- **Browse index rendered with paper-bundle title/banner.** Said `holotype paper bundle` everywhere and instructed the user to look for a `view.html` file that doesn't exist in the live archive. `render_index` is now parameterized so the browse path uses its own strings.
- **Real user prompts were styled as `system` (dim grey) blocks.** When Claude Code emits a user record with a string `message.content` (not the structured list-of-blocks form), the normalizer's fallback ignored the role and classified as system. Role now wins: a user-role record with string content becomes a user message regardless of content shape.
- **Browse server failed to rebind on quick restart** (`Address already in use`). Now sets `allow_reuse_address`.
- **Filtered `<synthetic>` from displayed model names** (a manifest sentinel that was leaking into the UI).
- **Page title on session views was the raw UUID.** Now uses `holotype: <project-basename> · <short-id>` when a project path is recorded.

### Reader UX

- **Richer session cards.** Each card now leads with the project basename, not the UUID. Below: human-readable date (e.g. `May 22, 2026 · 11:49`), message count, `tools + thinking` badges, output-token engagement signal, model. A secondary line shows the short session ID, the repo URL when known, and any subagent count.
- **Subagents grouped under their parent.** A 3,056-session archive used to render as 3,056 flat cards; now it renders as ~2,400 top-level cards with subagents in a per-parent collapsed `<details>`. Massive visual de-clutter.
- **In-archive full-text search.** New `/search?q=...` route on the browse server. Hits the existing FTS5 index that powers `scripts/search.py` and renders highlighted snippets with project + date + role and a link to the session. The index page now carries a search form at the top.
- **Default-collapse pre-conversation metadata.** Claude Code sessions open with 4-12 operational records (file-history snapshots, attachment deltas, system reminders) before the first real turn. The viewer now collapses that run into a single closed `<details>` so the user lands on the first conversation block.
- **Default-collapse long tool results** (>600 chars). A 50-message session with verbose shell output used to dominate vertical scroll; now you see a preview and click to expand.
- **Dark mode** via `@media (prefers-color-scheme: dark)` — both index and session views.
- **`?raw=1` query param** on `/session/<id>` re-enables the verbatim-JSON `<details>` toggle the paper-bundle viewer ships by default but the browse viewer omits for size. Lets a curious user audit without leaving the UI.

### Manifest schema (v5)

- `manifest_version` bumped to **5**. Adds `first_user_message_excerpt` and `last_user_message_excerpt` — short, cleaned-of-wrapping-tags previews of the user's first/last message. Cleans `<environment_context>` (Codex), `<USER_REQUEST>`/`<USER_SETTINGS_CHANGE>`/`<ADDITIONAL_METADATA>` (Antigravity), and `<system-reminder>` (Claude Code) so the excerpt reads as plain user prose.
- The browse viewer's session cards surface this excerpt below the project name. Existing v4 manifests continue to render fine (no excerpt line, falls back to the info line); excerpts populate as the existing manifest-version-mismatch backfill reprocesses old deposits on the next ingest.

### Selftest

New assertions: manifest v5 builds correctly; first-user-message excerpt is captured; browse index has the browse-specific title (not paper-bundle), search form present, dark-mode media query present, subagent grouping rendered; `?raw=1` plumbing re-enables raw-JSON toggles; `/search` returns either results or a no-results note.

---

## [2.1.0] — 2026-05-23

### Added

- **`holotype/viewer.py`** — self-contained HTML rendering of a deposited transcript. Single file per session, no JS framework, no CDN, strict CSP (`default-src 'none'; img-src data:; script-src 'none'`). Base64 image attachments reconstruct inline as `data:` URIs so a reviewer sees what the agent saw. Per-source normalizers cover Claude Code, Codex, and Antigravity; unrecognized record shapes fall through to a raw-JSON `<details>` block rather than being silently dropped.

- **`scripts/paper_bundle.py` now ships HTML alongside JSONL.** Each bundled session gets a `view.html`; the bundle root gets an `index.html` linking them. The HTML is regenerated from the canonical JSONL and is not part of the hash chain — reviewers can verify rendering faithfulness via the per-block raw-JSON toggle. This is the "read what you're uploading to Zenodo before you upload it" feature.

- **`scripts/browse.py`** — `python scripts/browse.py` (or "ask Claude to show me my holotype archive") starts a stdlib HTTP server bound to `127.0.0.1` on an OS-assigned port and opens the user's default browser. The index lists every archived session; clicking one renders the transcript HTML on the fly. **Zero disk cache** — the canonical archive is the source, HTML is generated per request. Localhost-only by design; the archive contains every conversation written on this machine and must not be exposed to the LAN.

  Earlier prototype pre-rendered all sessions to disk and generated 6 GB of HTML for a 3040-session archive. The on-demand server scales to any archive size and leaves nothing behind when stopped.

### Changed

- **First-time setup wizard collapsed to two real questions** (where the archive lives; back up to GitHub yes/no). Everything else — compression, GPG signing, retention bump, scheduled-backup install, scheduled-backup interval, encryption-when-backup-is-on — silently defaults to the better choice and is surfaced in the post-setup summary with an undo command per item. Driven by cold-start testing with a non-programmer PhD physicist who bounced off the previous wizard's jargon and decision-cost surface.

  Underlying design principle, now part of `SKILL.md`: don't ask a question if one answer is strictly better — asking itself communicates "you have to weigh a tradeoff here" and makes non-experts manage imagined risk by picking smaller / safer-sounding options (even when those are wrong for their actual case). Applies to *parameters* too — exposing a knob counts as asking. The wizard interval and the encryption sub-question both used to be exposed; both are now defaulted.

- **Wizard opening rewritten to lead with universal value, not academic value.** "Claude Code automatically deletes every conversation after 30 days" is the hook. The paper-citation framing is now a parenthetical for the small slice of users who will ever cite a transcript, not the headline.

- **New auto-mode preflight at the top of setup.** Claude Code's auto-mode classifier evaluates each Bash call independently and mid-flow blocks legitimate setup commands even after the user verbally said yes. The wizard now asks once up front whether to toggle auto-mode off for setup.

- **Vocabulary calibration multi-select drives the rest of the conversation.** Users who don't check `git` never see "commit," "remote," "branch." Users who don't check `the terminal` never get a `python scripts/foo.py` to copy-paste — commands run via the host CLI's Bash tool, with a plain-English fallback when the classifier blocks something. Users who don't check `encryption keys` never see "GPG" or "signing" surfaced as a decision.

- **Privacy-cost framing is now symmetric.** The previous wizard loudly named the cost of "push to GitHub" (third party gets your data) while leaving the cost of "local only" unstated, which biased non-experts toward the strictly worse option for anyone with paper-relevant research. The new wizard names BOTH costs: local-only means a single disk failure or a lost laptop wipes every conversation, including any future paper-cited one.

- **README features section** picks up the in-browser viewer.

### Selftest

- New assertions: paper bundle ships `index.html` + per-session `view.html` with strict CSP and no `<script>` tags; `browse.collect_sessions` sees the archive; `browse.render_index_html` and `render_session_html` produce script-free HTML with server-route hrefs.

---

## [2.0.4] — 2026-05-23

### Added

- **`scripts/update_check.py`** — stdlib-only, user-invoked release-check utility. Hits `api.github.com/repos/PEEKPerformer/holotype/releases/latest`, compares to `__version__`, prints either a human-readable line or `--json` for machine consumption. Result is cached at `~/.cache/holotype/update_check.json` for 24h (cache miss costs one anonymous request inside GitHub's 60/hr/IP unauthenticated limit). Always exits 0 so a network failure cannot break the workflow that called it. `SKILL.md` now instructs the LLM to run `--json` once at the start of any holotype operation and surface "update available" notices to the user without blocking or pulling on their behalf.

  This stays within the "no silent network behavior" principle: the user invoking the skill is the consent signal, and launchd never calls this script. Manual updates remain `git -C <repo-root> pull`; PyPI is deliberately deferred until there's demonstrated demand.

---

## [2.0.3] — 2026-05-23

### Fixed

- **`scripts/ingest.py` no longer exits with status 1 when there is nothing new to deposit.** Previously the script returned `1` whenever a tick found `unchanged=N empty=0 new=0 updated=0`, treating "nothing to do" as failure. This was wrong semantically and surfaced as a misleading `last exit code = 1` on the `io.holotype.ingest` launchd job after every idle tick, risking interaction with launchd's throttle/keepalive policies. Idle ticks now exit `0`. Ingest still propagates non-zero exit codes from real failures (Source parse errors, git failures, etc.).

---

## [2.0.2] — 2026-05-22

Repository-level JOSS-standard scaffolding. No functional code changes.

### Added

- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `GOVERNANCE.md`
- `CITATION.cff` (machine-readable citation metadata; GitHub renders the "Cite this repository" button)
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request}.md` and `.github/PULL_REQUEST_TEMPLATE.md`
- `docs/ZENODO_SOFTWARE_DOI.md` — guide for the GitHub→Zenodo integration that mints a software DOI per release
- README: short Research application, How to cite, JOSS readiness, and AI assistance disclosure sections

---

## [2.0.1] — 2026-05-22

Two bug fixes surfaced by end-to-end integration testing of the v1.2 / v2.0 releases.

### Fixed

- **`ingest.py` auto-push now pushes each bulk-initial chunk individually instead of bundling them.** The v1.2 auto-chunking creates N local commits sized under `--max-pack-gib`, but the previous end-of-cycle `git push origin HEAD` packed ALL unpushed commits into one stream — `pack-objects` builds a single pack from all unpushed commits, so the wire pack size was the SUM of the local chunks. This defeated the chunking and reproduced the very failure mode (GitHub's 2 GiB pack rejection) the chunking was supposed to prevent. The fix pushes each chunk immediately after committing it; the first chunk creates/upgrades the tracking branch (`push -u origin main`), subsequent chunks fast-forward. The end-of-cycle push is skipped when per-chunk pushes already covered HEAD. If a chunk's push fails, subsequent chunks are NOT attempted (they would fail until the prior one lands); the user can re-run ingest or use `scripts/repush_chunked.py` to retry.

- **`init.py` "Next steps" output no longer contradicts `--auto-push`.** Previously printed `"only when you explicitly want to publish — never auto-pushed"` even when `--auto-push` was on. Now config-aware: when auto-push is enabled, says so explicitly and shows the manual command as an option rather than the only path. When auto-push is off, prints the manual command and a note to re-init with `--auto-push` to flip the default.

### Test coverage

- Selftest gains a per-chunk auto-push assertion: forces multiple chunks with `--max-pack-gib 0.000001` against a local bare-repo remote, then verifies the bare remote ends up with all chunk commits AND that local HEAD matches remote HEAD (no unpushed work).

---

## [2.0.0] — 2026-05-22

Parallel worker pool for per-session ingest. The architectural lift that the v1.x perf work pointed toward, shipped as a major version because the ingest pipeline's internal shape changed even though the public CLI contract didn't.

### Added

- **`ingest.py --workers N`** — parallel deposit workers. `--workers 0` (default) auto-selects `min(os.cpu_count(), 8)`. `--workers 1` forces serial mode for debugging. Each worker is an independent process that does one candidate's hash + compress + manifest-build + transcript-write. The coordinator drains worker results in submission order and runs the single-writer SQLite + git path serially. Workers don't share state with each other or with the coordinator; they each re-open zstd subprocesses, re-import Source classes, and apply live-file safety on their own slice.

- **`holotype.parallel`** module — exposes `process_candidate_worker()` (pickleable top-level function for `ProcessPoolExecutor`) and `candidate_to_dict()` helper. Workers re-hydrate `DepositCandidate` from the dict form, re-resolve the `Source` class by name, and call the existing `deposit_one()` — so the per-session contract is unchanged.

### Changed

- **`commit_deposit()` now adds files by explicit path, not by directory.** Required for the parallel path: workers write files concurrently, so by the time the coordinator's `commit_deposit` runs for a parent session, a child session's subagent files may already be on disk. A directory-level `git add sessions/<parent>/` would sweep the child's files into the parent's commit, leaving nothing for the child's own `commit_deposit` to record. Explicit `git add sessions/<sub>/manifest.json sessions/<sub>/transcript.jsonl[.zst]` isolates each session's commit to its own files regardless of file-write order.

- **Output volume**: parallel mode prints `holotype ingest: N candidate(s) across W worker(s)` at start of cycle. Per-session log lines (`new`, `updated`) still print in submission order via the coordinator. `--quiet` suppresses both.

### Performance

The parallel path is most impactful on workloads where per-session CPU + I/O dominates: encrypted-bulk-initial first-time backfill, large transcripts (multi-MB sessions with subagents), and zstd-compressed deposits. Expected speedup on Apple Silicon M-series with 8 cores: 4-6× on bulk-initial, 2-3× on incremental ingest of many small new sessions. Steady-state daily ingests of <10 sessions see negligible benefit (and waste a small fixed cost spinning up workers); for those, `--workers 1` is fine.

### Compatibility

- Public CLI contract is unchanged. `python scripts/ingest.py` (no args) still works exactly as it did in v1.x; `--workers` defaults to auto, which falls back to serial when `os.cpu_count()` returns 1.
- Archive format is unchanged. `manifest_version` stays at 4. Existing archives ingest without any migration.
- Selftest covers both paths: an explicit `--workers 4` run is asserted to produce the same per-session-commit shape, pass `verify.py`, and serve FTS queries.

---

## [1.2.0] — 2026-05-22

Bulk-ingest performance improvements. Three orthogonal optimizations land together because they target the same workload (first-time backfill of an existing host-CLI history) and share testing infrastructure.

### Added

- **`ingest.py` auto-chunks oversized `--bulk-initial` commits.** When the projected pack size for a single `bulk-initial:` commit would exceed `--max-pack-gib` (default 1.5 GiB, well under GitHub's undocumented 2.00 GiB single-push ceiling), ingest now bin-packs `sessions/<project-dir>/` subtrees into N chunks and emits one signed `bulk-initial part K/N:` commit per chunk. Under the threshold, the single-commit behavior is unchanged. Bin-packing keeps each project intact in one commit so per-project `git log sessions/<project>/` stays coherent. Eliminates the need for the post-hoc `scripts/repush_chunked.py` recovery path on first ingest.

- **`ingest.py --fast-compress`** — pairs with `--bulk-initial`. Switches the zstd encoder from the locked archival preset (`-19 --long=27`) to fast (`-3`). Typically 3-5× faster compression at <10% ratio cost. Doesn't affect verification: the on-disk file is still valid zstd, `sha256_compressed` is recorded for whatever was written, and decompression produces the canonical bytes regardless of encoder level. Steady-state per-session ingests stay on the archival preset.

- **`holotype.chunking`** module — factored `bin_pack_paths()` + `dir_size_bytes()` out of `scripts/repush_chunked.py` so both the proactive auto-chunking in `ingest.py` and the reactive recovery in `repush_chunked.py` share the same algorithm.

### Changed

- **Deferred FTS index build for `--bulk-initial`.** Under bulk-initial mode, `reindex_session()` now accumulates FTS rows into a per-cycle buffer instead of inserting per-session. End-of-cycle, one `executemany` writes all rows at once and FTS5's `'optimize'` command merges the resulting segments into a compact final form. Per-session FTS5 segment merges were a dominant cost in the v1.1.x bulk path; consolidating into one bulk-insert + one optimize amortizes that overhead.

- **`PRAGMA mmap_size=268435456`** (256 MiB) in `_connect()`. Speeds up scan-heavy queries (FTS rebuild, full archive search) when the DB fits comfortably. Safe to set above the actual DB size; SQLite mmaps lazily.

### Performance notes

The combined v1.2 changes are aimed at reducing first-time bulk-ingest wall time. Conservative estimate from the constituent improvements: ~1.3-2× on encrypted bulk-initial workloads where the FTS path was the dominant cost. Auto-chunking is orthogonal to wall time — it prevents push failures rather than speeding them up.

---

## [1.1.6] — 2026-05-22

Recovery tooling for first-push failures against GitHub when encrypted bulk-initial commits exceed GitHub's per-push pack-size limit. Encrypted blobs don't benefit from git's pack-zlib delta compression (encrypted bytes are high-entropy), so the wire pack ≈ the sum of the encrypted file sizes. For archives in the 2 GiB+ range this means the first push hits an undocumented server-side rejection.

### Added

- **`scripts/repush_chunked.py`** — automated recovery when the first push hits GitHub's 2.00 GiB pack-size limit. Refuses to run unless `HEAD` is a `bulk-initial:` commit (conservative — won't touch anything else). Soft-resets that commit, bin-packs `sessions/<project-dir>/` subtrees into N chunks of configurable target size (default 1.5 GiB, well under GitHub's 2 GiB ceiling), creates one signed commit per chunk with deterministic message `bulk-initial part K/N: chunked for github 2 GiB pack limit`, and pushes each sequentially. Pauses the launchd background tick during surgery so a scheduled ingest can't race the soft-reset. `--dry-run` shows the bin-pack plan without touching anything; `--no-push` creates the chunks but leaves the push to the user.

- **`scripts/install-launchd.py --pause` and `--resume`** — toggle the macOS background tick without uninstalling. `--pause` runs `launchctl unload` but keeps the plist on disk; `--resume` re-loads from the preserved plist. Used by `repush_chunked.py` and any other repo-surgery operation that needs to keep the tick out of the way.

### Changed

- **Wizard Step 9a ETA matrix recalibrated** against measured run data on a ~6000-session / ~8 GB archive (Apple Silicon M-series). Previous v1.1.5 numbers underestimated the encrypted configurations; observed times were ~50 min for encrypted-signed-bulk-initial (vs. the prior "~20-30 min" estimate). Updated:
  - Encrypted: ~45-60 min (per-session or bulk-initial)
  - Encrypted + sign: ~60-90 min per-session, ~50-70 min bulk-initial

- **Wizard Step 9a now warns about GitHub's 2.00 GiB single-push pack-size limit** and recommends SSH transport over HTTPS for encrypted setups. HTTPS surfaces the rejection as a cryptic HTTP 500; SSH gives the real `pack exceeds maximum allowed size` message that points to the resolution path. Links to `scripts/repush_chunked.py` for recovery.

### Investigated, confirmed

- **GitHub HTTPS sideband + 2 GiB pack rejection** is server-enforced and undocumented in the public GitHub docs, but reproducible. Not fixable from the client side. Chunked re-push is the recovery path; switching to a self-hosted remote (Gitea, Forgejo, GitLab CE) avoids the limit entirely.

---

## [1.1.5] — 2026-05-22

### Changed

- **Wizard Step 9a — first-ingest ETA matrix replaces the old "minutes" estimate.** Earlier copy was written before encryption-before-push was added and didn't account for git-crypt's per-file clean-filter forks at `git add` time. New ETA table is honest about the four common configurations (plain vs. encrypted × signed vs. unsigned × per-session vs. bulk-initial). Includes a short note on *why* encrypted bulk-initial doesn't save the encryption cost (filter forks happen at `git add`, regardless of how many commits the cycle produces).

### Added

- **`docs/ROADMAP.md`** — captures v1.2 and v2 candidate work with honest tradeoffs. v1.2: deferred FTS rebuild on bulk ingest (~1.3×), `--fast-compress` flag pairing with `--bulk-initial` (~1.1× overall, ~2-3× on the compress step), `PRAGMA mmap_size`. v2: parallelize per-session ingest via worker pool + serialized coordinator (~4-6×, needs design doc). Explicit non-goals: GPU acceleration (workload isn't GPU-shaped), in-process git-crypt format reimplementation (crypto surface-area expansion not worth the speedup for a forensic tool), auto-export of git-crypt key at init (creates false sense of "init handled it").

### Investigated, filed as not-available

- **git-crypt process-filter mode.** Identified as the theoretical fix for the dominant cost in encrypted bulk-initial (per-file `git-crypt clean` filter forks at `git add` time). **Confirmed git-crypt 0.8.0 does not implement git's long-running process-filter protocol** — verified via `git-crypt process` (rejected as not-a-command) and binary `strings` inspection (no protocol packet-line markers). Options recorded in ROADMAP: upstream PR vs. live-with-it. Decision: lean on the v2 parallel-workers design instead.

---

## [1.1.4] — 2026-05-22

### Changed

- **Wizard Step 9b — first ingest now drives a Monitor for live progress.** Previously the wizard kicked off `ingest.py` as a background bash task and went silent until the harness notified on completion. For a 5–30 min first ingest that's a bad UX — the user has no signal that anything is happening.

  The new spec: launch the ingest in the background AND immediately arm a Monitor that counts deposited manifests every 15 seconds, emitting one event per 500-deposit milestone plus a COMPLETE event when ingest exits. Counts *manifests* not commits so the progress signal works for both per-session ingests AND `--bulk-initial` (where commits are deferred to one combined commit at the end; manifests are written eagerly).

  Subsequent (steady-state, post-first-ingest) cycles don't need a Monitor — they finish fast enough that background-task-then-notify is fine. Launchd / systemd tick ingests log to disk and don't drive a Monitor either.

- **Wizard Step 9c — post-ingest validation.** New numbered sub-step. After the Monitor COMPLETE event fires, the wizard runs three sanity checks: per-source session counts, full `verify.py` hash-chain check, and a confirmation that the bulk-initial commit pushed to the remote (if auto-push is on). Closes the wizard loop with the canonical "setup done, archive healthy" moment.

---

## [1.1.3] — 2026-05-22

### Added

- **Wizard Step 7a — Back up the git-crypt key.** New numbered step between host-retention check and launchd opt-in. Fires only when `config.deposit.encrypt_transcripts` is true. Drives the key-backup conversation BEFORE launchd (which would auto-push) and BEFORE the first ingest — closing the window where encrypted blobs could ship to the remote while the only key copy still lives on a single disk.

  The step detects candidate destinations on the user's machine — iCloud Drive root, Dropbox, Google Drive, OneDrive, 1Password CLI (`op` binary), `~/Documents/` *only if* iCloud "Desktop & Documents" sync is detected — and offers them in priority order with a custom-path escape hatch. Recognized cloud-synced destinations are confirmed as "genuinely offsite"; same-disk paths are surfaced as "this is NOT a real backup" without ambiguity.

  Pinning this as a numbered wizard step (instead of leaving it for the host LLM to improvise mid-flow) ensures the correct `git-crypt export-key` syntax is used and that the destination is verified as genuinely offsite — not just on the same physical disk under a different folder name.

---

## [1.1.2] — 2026-05-22

Wizard recommendation reframe — no behavioral default change.

### Changed

- **Wizard Step 3 — privacy-decision options reordered + reframed.** The recommended option is now **GitHub private repo (encrypted)** rather than the historical "Local only (recommended for sensitive work)." Rationale: encrypted-GitHub is the natural 3-2-1-backup-rule answer for scientific work — local archive + offsite encrypted backup + key backed up separately. Local-only is still listed (third, with its own rationale: easiest install, safest from third-party-data-exposure concerns, single-disk-failure data loss risk noted). Self-hosted / Synology / Other URL still listed for non-GitHub workflows; the encryption decision at Step 4a continues to apply to them too.
- **Wizard Step 4a — encryption question skips redundancy.** When the user picks the "GitHub private repo (encrypted)" Step 3 option, encryption is already implied — Step 4a skips the y/N question and goes straight to the data-loss confirmation. For any other remote choice, Step 4a still independently asks.

### Why this is a recommendation shift, NOT a default change

The TECHNICAL default (what happens with `python scripts/init.py --path ... --remote-url ""`) is unchanged. The wizard's *suggested* path changed. Users who pick the recommendation are walked through `gh repo create` + `git-crypt` install + key generation + key-loss ack — none of which happens silently. Users who pick local-only still get the fast no-deps path.

The class of user who would have been bitten by a default-flip (data-loss-by-default for users who don't understand key backup, hard `gh`/`git-crypt` prerequisites, banner-blindness from loud warnings firing at every fresh install) is unaffected — they still see local-only as a first-class option and pick it if their threat model demands.

---

## [1.1.1] — 2026-05-22

Wizard polish + first-time-backfill perf.

### Added

- **`ingest.py --bulk-initial`** — first-time-only mode that bundles ALL transcript-changed deposits into a single `bulk-initial: N session(s) ingested` commit instead of one commit per session. Dramatically faster when `sign_commits=true` (one GPG signature vs. N — saves ~30 min on a 6000-session backfill with signing on). Loses per-session ordering INSIDE the initial backfill (bundled sessions share one commit); future ingests resume per-session commits as normal. The flag is meant to be passed exactly once at first ingest; the launchd / systemd tick never passes it.
- **SKILL.md wizard Step 4c — GPG-signed commits with key-generation offer.** Previously the signing decision was floating between the auto-push step and the confirm step, almost as an afterthought; now it's grouped with the other trust-model decisions (encryption, auto-push). Symmetric to the zstd / git-crypt install offers, the wizard now detects an empty `user.signingkey` and offers to generate an Ed25519 key (no-passphrase recommended for automated archives, passphrase optional), wires it into `git config --global`, tests it produces a signature, and optionally uploads to GitHub via `gh gpg-key add` for the "Verified" badge.
- **SKILL.md wizard Step 9a — First-ingest scale warning.** Before launching the first ingest, the wizard now re-runs `usage_estimate.py` and projects the realistic wall time + bytes-to-be-pushed, then asks per-session vs. bulk-initial. Catches first-time users by surprise less.
- **SKILL.md confirmation table format pinned.** The summary in Step 5 is now a structured table with columns for Setting / Value, recommended as Markdown or aligned text depending on what the host CLI renders.

### Changed

- **SKILL.md wizard Step 8 — launchd tick clarification.** Previously said "local-only and never pushes to a remote," which contradicted the v1.0 auto-push reality. Now correctly: push behavior follows the configured `auto_push` setting; the tick deposits AND pushes when auto-push is on. That's the whole point for "set it and forget it" users.
- **SKILL.md wizard Step 9 split into 9a (scale warning) and 9b (run ingest)**, so the user has a chance to pick `--bulk-initial` before kicking off a long-running command.

---

## [1.1.0] — 2026-05-22

### Added

- **Optional encryption-before-push via `git-crypt`** (`init.py --encrypt-transcripts`). Filters `transcript.jsonl` / `transcript.jsonl.zst` paths through git-crypt so the remote stores only encrypted blobs. Manifests stay plaintext (session IDs, timestamps, models, project paths, and token totals remain visible to the remote — metadata leakage is the documented tradeoff). Use case: institutional / shared / partially-trusted remotes where you want backup capacity without exposing transcript content.
- **Loud data-loss preflight.** init refuses to enable encryption without three preconditions: a remote configured (`--remote-url`), `git-crypt` on PATH, and an explicit `--i-understand-key-loss-means-data-loss` acknowledgment flag. The banner makes the failure mode unambiguous: lose the GPG key and every encrypted deposit is unrecoverable, including paper-cited sessions. Holotype cannot recover lost data.
- **`HOW_TO_BACK_UP_YOUR_KEY.md`** is dropped into the archive at init time when encryption is on. Documents `git-crypt export-key`, recovery testing via fresh clone + unlock, and the recommended backup destinations (password manager, offline USB, paper QR, trusted collaborator). Survives clones so the recovery plan travels with the encrypted artifact.
- SKILL.md wizard gains Step 4a (encryption decision) between the remote-URL step and the auto-push step, with the per-OS git-crypt install offer (`brew install git-crypt` / `apt install git-crypt` / `dnf` / `pacman`).
- `config.deposit.encrypt_transcripts: bool` records the choice. Locked for the archive's lifetime — toggling mid-stream would orphan the prior commits' transcript blobs.

### Verification tracks under encryption

- Reviewer **with the key**: Track A works — `git-crypt unlock`, then `shasum -a 256 transcript.jsonl` matches `manifest.sha256`.
- Reviewer **without the key**: Track B (`sha256_compressed` on the encrypted-blob-as-stored) is brittle because git-crypt's encrypted bytes aren't byte-stable across `git clone` paths. Documented limitation; encryption is a trust-the-key story, not a trust-nobody story.

---

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
- **No auto-invocation.** Depositing data into a scientific archive is a deliberate user decision, not an opportunistic background task. A separate full-mirror layer (e.g. an rsync `Stop` hook to `~/Documents/Claude-Backups/`) can handle "everything, automatically" if you want it; holotype is the curated layer on top.
- **Verify with stock Unix tools.** The `VERIFY.md` shipped inside every archive (and inside every `paper_bundle.py` deposit) uses only `shasum`, `jq`, `git`, and optionally `zstd`. A reviewer five years from now needs no holotype install.

### Known limitations

- The macOS launchd background tick has no Linux/Windows equivalent shipped (Linux: write a user systemd unit; Windows: Task Scheduler). The core skill works cross-platform; only the auto-tick is macOS-only.
- Antigravity Source's schema was captured from a single observed session — additional record types may emerge as the CLI matures. The "not gospel" note in `SKILL.md` encourages the host LLM to verify on-disk reality before trusting the documented schema.
- `claude_code` model identifiers depend on Anthropic's per-turn `message.model` field. If that field disappears in a future Claude Code release, `manifest.models` will be empty until the parser is updated.
