# holotype

Forensic-grade archival of LLM-driven agent CLI sessions for scientific reproducibility — packaged as an [agent skill](https://agentskills.io) with bundled Python scripts.

> *holotype, n. (taxonomy) — The single physical specimen used when a species is formally described. Every later observation is compared against it.*

`holotype` deposits agent-CLI conversation transcripts (Claude Code, Codex, Google Antigravity, and any other CLI you wire in) into a content-addressable, hash-chained git archive suitable for citation in scientific publications. It is *not* a backup tool and it is *not* a search tool — both already exist. It is a **provenance** tool.

## Why this exists

Most agent-CLI archival tools optimize for search, which means they filter "noise" — tool calls, system reminders, hook outputs — before storing. That is the wrong tradeoff for scientific reproducibility. The tool calls *are* the experimental record. The system reminders explain why the model behaved the way it did. The hook outputs prove which guardrails were active.

`holotype` preserves every byte. No filtering, no re-encoding, no clever normalization. The raw JSONL is the specimen.

## Design philosophy (non-negotiable)

1. **Forensic completeness.** Tool calls, tool results, thinking blocks, system reminders, hook outputs, image attachments — every byte preserved verbatim.
2. **Provenance via hash chain.** Each session has a SHA-256 manifest. Manifests reference the git commit that introduced them. Modification after deposit breaks the chain and is detectable by `scripts/verify.py` or by stock Unix tools.
3. **Git is the archive substrate.** Not SQLite, not a tarball. Git's content-addressable storage, commit history, and signed tags *are* the provenance system. We do not reinvent them.
4. **Environment capture.** Each manifest records the host CLI version, model IDs that appeared, OS/platform, the project repo's git state, wall-clock time, and aggregated LLM token usage.
5. **Append-only by design.** Once deposited, a session is immutable. Re-ingesting the same session is a no-op (SHA-256 dedup).
6. **No silent network behavior.** Setup is an interactive conversation about where the archive lives and whether it has a remote. Push to remote is never automatic — always confirmed per-action.
7. **Verifiable without the skill.** The archive is a plain git repo. A reviewer with no access to the host CLI can verify integrity using stock Unix tools. See [VERIFY.md](#verifying-an-archive-without-the-skill).

## What's in v1.0.0

- **Three first-class sources**: Claude Code (with nested subagents), Codex (with date-partitioned rollouts), Google Antigravity (with the encrypted-blob-ignored plaintext side-channel).
- **Optional zstd compression at deposit** — manifests record both uncompressed and compressed SHA-256 so reviewers can verify with or without `zstd` installed. Locked per archive at init.
- **Reproducibility-grade manifest (v4)** — captures `project_git_state` (the repo state the LLM operated on, sourced from the session-start header when the host CLI provides one), `wall_clock_seconds`, and aggregated `total_input_tokens` / `total_output_tokens` / `total_cache_*_tokens`.
- **`paper_bundle.py`** — packages many sessions into one Zenodo-ready deposit with a master `BUNDLE_MANIFEST.json` and optional tarball + SHA-256 sidecar.
- **Two-track verification** — reviewer with `zstd` installed verifies the uncompressed canonical SHA-256; reviewer without `zstd` verifies the on-disk compressed SHA-256. Both succeed for valid deposits.
- **Optional GPG-signed deposit commits** — opt-in at init via `--sign-commits` for high-stakes archives.
- **LLM-adaptable extension** — when invoked from an unrecognized host CLI, the skill instructs the LLM to read `docs/ADDING_A_SOURCE.md` and write a new Source class against the documented contract, rather than silently dropping that CLI's sessions.

## Architecture

```
~/Git/holotype/                  # this repo — the skill source
├── SKILL.md                     # what the host LLM reads when invoking the skill
├── holotype/                    # Python library: sources, manifest, env, compression, index
├── scripts/                     # CLI entry points the skill invokes via Bash
└── tests/                       # fixtures + selftest

~/Documents/holotype-archive/    # the user's archive (created by the wizard)
├── .holotype/
│   ├── config.json              # archive config (portable — moves with the archive)
│   ├── index.sqlite             # derived FTS5 search index (gitignored)
│   └── cache/                   # decompressed transcript cache (gitignored)
├── README.md                    # archive identity
├── VERIFY.md                    # standalone verification procedure (no holotype needed)
└── sessions/
    ├── <project-dir>/<session-id>/             # Claude Code top-level session
    │   ├── transcript.jsonl  (or .jsonl.zst)
    │   └── manifest.json
    ├── <project-dir>/<parent>/subagents/<sid>/ # Claude Code subagent
    ├── codex/<YYYY>/<MM>/<DD>/<sid>/           # Codex date-partitioned
    └── antigravity/<sid>/                      # Google Antigravity
```

A pointer at `~/.config/holotype/archive-path` records the archive's location so future sessions can find it.

## Install

`holotype` is an [agent skill](https://agentskills.io) and works in any CLI that implements the standard — Claude Code, Codex, Gemini CLI, GitHub Copilot, Cursor (manual), Goose, OpenCode, OpenHands, Roo, Amp, Junie, Kiro, Factory, Trae, Tabnine, Letta, Databricks Genie Code, Snowflake Cortex Code, and others. The skill format (SKILL.md + scripts/) is identical across implementations; only the discovery path differs.

```bash
git clone https://github.com/PEEKPerformer/holotype.git ~/Git/holotype
```

Then symlink (or copy) into the host CLI's user-level skill directory:

```bash
# Claude Code
ln -s ~/Git/holotype ~/.claude/skills/holotype

# Codex
ln -s ~/Git/holotype ~/.agents/skills/holotype
```

You can do both — the same skill source serves both CLIs. OpenAI-specific UI/policy metadata lives in `agents/openai.yaml`; Anthropic-specific frontmatter lives in `SKILL.md`. The instructions and scripts are shared.

Per-repo installs work too: drop the skill under `.claude/skills/` or `.agents/skills/` inside a project.

### System dependencies

`holotype` is stdlib-only on the Python side. Required external binaries:

- **git** (the archive substrate)
- **shasum** / **sha256sum** (verification)
- **jq** (manifest parsing in VERIFY.md procedures)
- **zstd** (optional — needed only if you opt into compressed deposits; install via `brew install zstd` / `apt install zstd`)
- **gpg** (optional — only if you turn on `--sign-commits`)

Python 3.11+ (uses PEP 604 union syntax).

### Platform support

- **macOS** — fully supported, including the launchd background tick (`scripts/install-launchd.py`).
- **Linux** — fully supported. The background tick equivalent is a user systemd unit (left to the user; example in `docs/`).
- **Windows** — the core skill works (WSL or native Python); the background tick equivalent is Task Scheduler (left to the user).

## Usage

`holotype` is **deliberately manual**. It is never auto-triggered by phrases or keywords — every invocation is an explicit user action, because depositing data into a scientific archive is the kind of decision a human should make consciously.

### Inside a skills-supporting CLI

Invoke the skill explicitly:

```
/holotype    # Claude Code
$holotype    # Codex
```

The first invocation walks you through the interactive setup wizard (archive location → storage projection + compression choice → remote choice → host-CLI retention → background-tick opt-in → first ingest). Subsequent invocations let you deposit, search, cite, verify, or load context — the host LLM will ask which operation you want.

### From any shell, without an LLM

Scripts are directly runnable. This is also how the macOS launchd background tick invokes them — no LLM required:

```bash
python scripts/init.py --path ~/Documents/holotype-archive --remote-url "" --remote-kind none --compression auto
python scripts/usage_estimate.py            # storage projection from your source dirs
python scripts/ingest.py                    # deposit any new sessions
python scripts/search.py "ionic gel"        # FTS over the archive
python scripts/verify.py                    # hash-chain check
python scripts/cite.py 3f1c4cf7             # bundle one session for citation
python scripts/paper_bundle.py --sessions a,b,c --out ./zenodo-deposit/ --tarball
python scripts/install-launchd.py --archive ~/Documents/holotype-archive  # macOS only
```

The skill is convenience automation over the scripts. The scripts are the engine.

## Tutorial: citing an LLM session in your paper

The motivating use case is *citation-grade* preservation of LLM-driven experiments. Here's the full pipeline:

### 1. Set up the archive (once)

Invoke `/holotype` in a session-bearing CLI and follow the wizard, or run init directly:

```bash
python scripts/init.py \
    --path ~/Documents/holotype-archive \
    --remote-url "" --remote-kind none \
    --compression auto \
    --sign-commits           # optional, for high-stakes archives
```

`--compression auto` resolves to `zstd` if the binary is on PATH, otherwise `none`. The choice is locked for the life of the archive.

### 2. Deposit your sessions

Either let the background tick handle it, or run on demand:

```bash
python scripts/ingest.py
```

Every existing Claude Code / Codex / Antigravity session under the registered Sources' default paths becomes one commit in the archive, with a manifest carrying:

- SHA-256 of the uncompressed JSONL (the citation hash)
- model IDs that appeared
- `project_git_state.commit` — the repo state the LLM operated on
- `wall_clock_seconds`, `total_input_tokens`, `total_output_tokens`, `total_cache_*_tokens`
- host CLI version, OS, holotype version

### 3. Identify the sessions your paper cites

Use `search.py` to find them:

```bash
python scripts/search.py "ionic gel equilibration"
#   3f1c4cf7  2026-04-05  [assistant]  -Users-brendenferland-Git-ResistaMet-GUI
#       …with the 41-minute equilibration we measured today, the…
```

Or hand-pick session UUIDs from `git log` in the archive.

### 4. Bundle them for the paper's Zenodo deposit

```bash
python scripts/paper_bundle.py \
    --sessions 3f1c4cf7,a8b2,deadbeef \
    --out ~/Desktop/v2.0.0-zenodo-deposit/ \
    --paper-title "Ionic gel equilibration via LLM-driven instrument control" \
    --paper-doi "10.xxxx/yyyyyy" \
    --tarball
```

The output directory contains:
- one subdir per session with **plain** `transcript.jsonl` + `manifest.json` + `cite.txt` + `render.md` (a reviewer never needs `zstd`)
- a master `BUNDLE_MANIFEST.json` listing every session's SHA-256, tokens, git state, and paper metadata
- a `VERIFY.md` for reviewers
- `<bundle>.tar.gz` + `<bundle>.tar.gz.sha256` if `--tarball` is set

Upload to Zenodo. The DOI Zenodo issues is what you cite in the paper's Data Availability Statement.

### 5. Cite in the paper

A Digital Discovery DAS section that satisfies the journal's LLM-input/output / model-identifier / generation-date requirement looks like:

> *All LLM sessions referenced in this work are deposited at [Zenodo DOI]. Each session bundle contains the verbatim host-CLI JSONL transcript, a SHA-256 manifest, and verification instructions. The deposited archive at submission corresponds to commit `<archive-commit>` of the holotype archive. Reviewers can verify any cited session via the procedure in the bundle's `VERIFY.md` using only stock Unix tools (`shasum`, `jq`).*

## Verifying an archive without the skill

The archive is a plain git repository. `VERIFY.md` ships inside the archive (and inside every `paper_bundle.py` deposit) with the standalone procedure. Two tracks:

- **Track A** — recompute SHA-256 of the uncompressed JSONL (decompressing the `.jsonl.zst` first if compression is on; requires `zstd`) and compare to `manifest.sha256`.
- **Track B** — for compressed deposits, hash the `.jsonl.zst` file as-stored and compare to `manifest.sha256_compressed`. No `zstd` required.

Either track is sufficient. Both must succeed for a valid deposit.

## Adding support for a new agent CLI

`holotype` is built around a small `Source` ABC. Each Source teaches the archive how to discover and parse one host CLI's session transcripts. Adding a new CLI is one file in `holotype/sources/` plus an entry in the registry.

See [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md) for the field guide. The guide is written for an LLM reader — when `/holotype` is invoked in an unrecognized CLI, the skill instructs the host LLM to read the guide and produce a working Source class for its own CLI, validated by `scripts/selftest.py`.

## Status

**v1.0.0** — public-release ready. Three sources shipped (Claude Code, Codex, Antigravity), manifest v4 with reproducibility fields, optional zstd compression with two-track verify, multi-session paper bundles, optional GPG-signed commits, CI runs the full selftest on Ubuntu + macOS.

See [CHANGELOG.md](CHANGELOG.md) for design history.

## License

MIT. See [LICENSE](LICENSE).
