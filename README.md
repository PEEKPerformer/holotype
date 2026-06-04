# holotype

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20349311.svg)](https://doi.org/10.5281/zenodo.20349311)

Forensic-grade archival of LLM-driven agent CLI sessions for scientific reproducibility, packaged as an [agent skill](https://agentskills.io) with bundled Python scripts.

> *holotype, n. (taxonomy): the single physical specimen used when a species is formally described. Every later observation is compared against it.*

`holotype` deposits agent-CLI conversation transcripts (Claude Code, Codex, Google Antigravity, and any other CLI you wire in) into a content-addressable, hash-chained git archive suitable for citation in scientific publications. It backs up those transcripts and lets you search them, but it is more than a backup and more than a search index: a backup proves nothing about what it stored, and a search index drops the tool calls to go fast. `holotype` is a **provenance** tool.

## Why this exists

Most agent-CLI archival tools optimize for search, which means they filter "noise" (tool calls, system reminders, hook outputs) before storing. That is the wrong tradeoff for scientific reproducibility. The tool calls *are* the experimental record. The system reminders explain why the model behaved the way it did. The hook outputs prove which guardrails were active.

`holotype` preserves every byte. No filtering, no re-encoding, no clever normalization. The raw JSONL is the specimen.

## Research application

LLM-driven sessions that drive instruments, perform autonomous analysis, or execute multi-hour scientific workflows are research artifacts. Papers that cite them in a Data Availability Statement need a verbatim, hash-verifiable record, not the search-optimized summaries existing tools produce. `holotype` fills that gap: every byte preserved, hash-chained, citable via Zenodo DOI, verifiable with stock Unix tools.

## Deliberately overkill

No journal today requires provenance this strong, and that is the point. `holotype` captures at the ceiling: verbatim bytes, a tamper-evident hash chain, and verification that needs no trust in the tool. From there you can give any journal exactly the subset its Data Availability policy asks for. The reverse never works. You can always provide less than you kept, never more, and transcripts pruned by a default cleanup window are gone for good. Starting at the highest standard is the only hedge against a requirement that tightens after the experiment is over.

## Design philosophy (non-negotiable)

1. **Forensic completeness.** Tool calls, tool results, thinking blocks, system reminders, hook outputs, image attachments: every byte preserved verbatim.
2. **Provenance via a real hash chain.** Each session has a SHA-256 manifest (per-file content integrity). On top of that, an append-only hash-chained ledger (`.holotype/ledger.jsonl`) records one link per content event, each committing to the previous link's hash, so the *set and order* of deposits is tamper-evident, not just each file in isolation. The chain **head** is a single 64-hex anchor you cite in a Data Availability Statement. `scripts/verify.py` walks both layers; insertion, deletion, or a co-edited transcript+manifest is caught as a broken chain or an orphan.
3. **Git is the archive substrate.** Not SQLite, not a tarball. Git's content-addressable storage, commit history, and signed tags *are* the provenance system. We do not reinvent them.
4. **Environment capture.** Each manifest records the host CLI version, model IDs that appeared, OS/platform, the project repo's git state, wall-clock time, and aggregated LLM token usage.
5. **Append-only by design.** Once deposited, a session is immutable, and ingest is idempotent: re-ingesting the same session is a no-op (SHA-256 dedup), so a retry or a background-tick overlap can never double-deposit.
6. **Network behavior follows init-time consent.** Setup is an interactive conversation about where the archive lives, whether it has a remote, and whether to auto-push. The remote URL is never set silently. Once you consent at init, auto-push after each ingest cycle is honored; local-only archives have nothing to push and never do.
7. **Verifiable without the skill.** The archive is a plain git repo. A reviewer with no access to the host CLI can verify integrity using stock Unix tools. See [VERIFY.md](#verifying-an-archive-without-the-skill).

## Features

- **Three host CLIs supported out of the box**: Claude Code (with nested subagents), Codex (date-partitioned rollouts), Google Antigravity. New CLIs are one file under `holotype/sources/` against a documented [Source ABC](docs/ADDING_A_SOURCE.md).
- **Optional `zstd` compression** at deposit, with two-track verification (uncompressed canonical hash + compressed-as-stored hash) so reviewers can verify with or without `zstd` installed.
- **Optional `git-crypt` encryption** before push, for backup remotes that shouldn't see transcript content. Manifests stay plaintext; the data-loss risk of key loss is surfaced loudly at init.
- **Optional GPG-signed deposit commits** for tamper-evident provenance.
- **Reproducibility manifest** capturing project repo git state, wall-clock duration, model IDs, and per-session token totals.
- **Self-verifying paper bundles** via `scripts/paper_bundle.py`: extract a curated session subset for Zenodo deposit, with a master `BUNDLE_MANIFEST.json` (recording the chain head), the full `ledger.jsonl`, per-session self-contained `view.html`, top-level `index.html`, and tarball + SHA-256 sidecar. The shipped `VERIFY.md` walks a reviewer through per-file *and* hash-chain verification, to the published head, with no upstream repo required.
- **In-browser archive viewer** via `scripts/browse.py`: a stdlib HTTP server on localhost that renders the index and each session on demand. Zero disk cache. Session cards lead with project name + human date + first-user-message excerpt; subagents nest under their parent; the index has a full-text-search box backed by SQLite FTS5; dark mode follows the OS preference. Lets a non-programmer answer the first real post-setup question ("how do I look at my saved chats?") without touching the CLI.
- **Parallel ingest pipeline** with auto-chunking and per-chunk push so encrypted multi-GB archives don't trip GitHub's pack-size limit.
- **Background tick** (macOS launchd, Linux systemd unit template) for catch-up ingests on long-running sessions.

## Architecture

```
~/Git/holotype/                  # this repo: the skill source
├── SKILL.md                     # what the host LLM reads when invoking the skill
├── holotype/                    # Python library: sources, manifest, env, compression, index
├── scripts/                     # CLI entry points the skill invokes via Bash
└── tests/                       # fixtures + selftest

~/holotype-archive/    # the user's archive (created by the wizard)
├── .holotype/
│   ├── config.json              # archive config (portable, moves with the archive)
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

holotype is set-and-forget. An LLM reads the repo and sets it up once: it creates the archive and installs the background tick (launchd on macOS, systemd on Linux) that deposits new sessions on its own from then on. There is no daemon to babysit and nothing to relaunch. The clone *is* the install.

```bash
git clone https://github.com/PEEKPerformer/holotype.git ~/Git/holotype
```

Point your agent at that repo and tell it to set holotype up, or run `python ~/Git/holotype/scripts/init.py --help` yourself. The scripts are stdlib-only Python and run with no LLM, so the tick keeps depositing long after the first conversation ends. Keep the clone in place: the background tick runs it directly.

### Optional: install as a skill

You need this only if you want an agent to help with later one-off operations (search, cite, verify, browse) by invoking `/holotype`, rather than re-cloning or naming scripts. The core archiving never needs it, because setup already installed the background tick. If you do want it, symlink (or copy) the repo into your CLI's user-level skill directory so the agent can discover it. The skill format (SKILL.md + scripts/) is identical across Claude Code, Codex, Gemini CLI, GitHub Copilot, Cursor (manual), Goose, OpenCode, OpenHands, Roo, Amp, Junie, Kiro, Factory, Trae, Tabnine, Letta, Databricks Genie Code, Snowflake Cortex Code, and others; only the discovery path differs.

```bash
# Claude Code
ln -s ~/Git/holotype ~/.claude/skills/holotype

# Codex
ln -s ~/Git/holotype ~/.agents/skills/holotype
```

You can do both: the same skill source serves both CLIs. OpenAI-specific UI/policy metadata lives in `agents/openai.yaml`; Anthropic-specific frontmatter lives in `SKILL.md`. The instructions and scripts are shared.

Per-repo installs work too: drop the skill under `.claude/skills/` or `.agents/skills/` inside a project.

### Built Claude Code first

holotype is written Claude Code first, because that is what the maintainer uses. Every other CLI above shares the identical skill format but gets limited testing. Two properties keep that workable. The skill is **self-applying**: an LLM dropped into a CLI holotype doesn't recognize can read [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md) and write a working Source for its own host, validated by `scripts/selftest.py`. And ingest is **idempotent**: re-running is safe, so a half-working new Source can't corrupt an archive. [Issues and pull requests](https://github.com/PEEKPerformer/holotype/issues) for other CLIs are welcome.

### System dependencies

`holotype` is stdlib-only on the Python side. Required external binaries:

- **git** (the archive substrate)
- **shasum** / **sha256sum** (verification)
- **jq** (manifest parsing in VERIFY.md procedures)
- **zstd** (optional, needed only if you opt into compressed deposits; install via `brew install zstd` / `apt install zstd`)
- **gpg** (optional, only if you turn on `--sign-commits`)

Python 3.11+ (uses PEP 604 union syntax).

### Platform support

- **macOS**: fully supported, including the launchd background tick (`scripts/install-launchd.py`).
- **Linux**: fully supported. The background tick equivalent is a user systemd unit; see [docs/LINUX_SYSTEMD.md](docs/LINUX_SYSTEMD.md) for a template.
- **Windows**: the core skill works (WSL or native Python); the background tick equivalent is Task Scheduler (left to the user).

## Usage

`holotype` is **never auto-invoked** by phrases or keywords. You start it deliberately, because choosing to preserve data into a scientific archive is a decision a human should make consciously. That decision happens once, at setup. After that, the background tick can deposit new sessions on its own (see the background-tick feature above); the conscious choice is the setup, not every deposit.

### Inside a skills-supporting CLI

Invoke the skill explicitly:

```
/holotype    # Claude Code
$holotype    # Codex
```

The first invocation walks you through a two-question setup wizard (*where should the archive live?* and *back up to a private GitHub repo?*) followed by a tailored summary of everything else it set up on your behalf: compression, signing, retention bump, background ingest job, first deposit. The wizard refuses iCloud-synced paths (which silently break the background job) and adapts its vocabulary based on which technologies you've worked with. Subsequent invocations let you deposit, search, cite, verify, browse, or load context, and the host LLM will ask which operation you want.

holotype's users are working scientists, not software engineers. The first version assumed familiarity with hashing, git, git-crypt, and compression, because it was written for one engineer. It was then hardened by watching non-technical scientists run it and fixing whatever tripped them, which is why the wizard adapts its language to what you already know and never assumes you can read a stack trace.

### From any shell, without an LLM

Scripts are directly runnable. This is also how the macOS launchd background tick invokes them, with no LLM required:

```bash
python scripts/init.py --path ~/holotype-archive --remote-url "" --remote-kind none --compression auto
python scripts/usage_estimate.py            # storage projection from your source dirs
python scripts/ingest.py                    # deposit any new sessions
python scripts/search.py "ionic gel"        # FTS over the archive
python scripts/verify.py                    # per-file + hash-chain check
python scripts/build_ledger.py              # (re)build/seal the hash chain (rarely needed; ingest auto-bootstraps)
python scripts/cite.py 3f1c4cf7             # bundle one session for citation
python scripts/paper_bundle.py --sessions a,b,c --out ./zenodo-deposit/ --tarball
python scripts/install-launchd.py --archive ~/holotype-archive  # macOS only
```

The skill is convenience automation over the scripts. The scripts are the engine.

## Tutorial: citing an LLM session in your paper

The motivating use case is *citation-grade* preservation of LLM-driven experiments. Here's the full pipeline:

### 1. Set up the archive (once)

Invoke `/holotype` in a session-bearing CLI and follow the wizard, or run init directly:

```bash
python scripts/init.py \
    --path ~/holotype-archive \
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
- `project_git_state.commit`: the repo state the LLM operated on
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

Upload to Zenodo. The DOI Zenodo issues is what you cite in the paper's Data Availability Statement. See [docs/PUBLISHING_TO_ZENODO.md](docs/PUBLISHING_TO_ZENODO.md) for the full Zenodo-side workflow: access modes, license choice, metadata template, sample DAS paragraph, versioning model, and gotchas.

### 5. Cite in the paper

A Digital Discovery DAS section that satisfies the journal's LLM-input/output / model-identifier / generation-date requirement looks like:

> *All LLM sessions referenced in this work are deposited at [Zenodo DOI]. Each session bundle contains the verbatim host-CLI JSONL transcript, a SHA-256 manifest, and verification instructions. The deposited archive at submission corresponds to commit `<archive-commit>` of the holotype archive. Reviewers can verify any cited session via the procedure in the bundle's `VERIFY.md` using only stock Unix tools (`shasum`, `jq`).*

## Verifying an archive without the skill

The archive is a plain git repository. `VERIFY.md` ships inside the archive (and inside every `paper_bundle.py` deposit) with the standalone procedure. Two tracks:

- **Track A**: recompute SHA-256 of the uncompressed JSONL (decompressing the `.jsonl.zst` first if compression is on; requires `zstd`) and compare to `manifest.sha256`.
- **Track B**: for compressed deposits, hash the `.jsonl.zst` file as-stored and compare to `manifest.sha256_compressed`. No `zstd` required.

Either track is sufficient. Both must succeed for a valid deposit.

## Adding support for a new agent CLI

`holotype` is built around a small `Source` ABC. Each Source teaches the archive how to discover and parse one host CLI's session transcripts. Adding a new CLI is one file in `holotype/sources/` plus an entry in the registry.

See [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md) for the field guide. The guide is written for an LLM reader: when `/holotype` is invoked in an unrecognized CLI, the skill instructs the host LLM to read the guide and produce a working Source class for its own CLI, validated by `scripts/selftest.py`.

## Status

Active development. See [CHANGELOG.md](CHANGELOG.md) for the version history. Single-maintainer; see [GOVERNANCE.md](GOVERNANCE.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## How to cite

Cite `holotype` if your paper relies on it in a load-bearing way:

- You used it to produce the session transcripts that your paper's Data Availability Statement cites.
- You used `paper_bundle.py` to package transcripts for Zenodo deposit.
- You used `verify.py`, the manifest schema, or any other holotype output as evidence in your Methods.

You don't need to cite it for casual install / inspection.

[`CITATION.cff`](CITATION.cff) carries the machine-readable metadata (GitHub renders a "Cite this repository" button). Cite the **version DOI** of the release your work used, not the concept DOI which tracks latest. See [docs/PUBLISHING_TO_ZENODO.md](docs/PUBLISHING_TO_ZENODO.md) for citing individual session bundles from a DAS.

## AI assistance disclosure

`holotype` was developed with substantial AI assistance from Anthropic Claude (Opus 4.x via [Claude Code](https://claude.com/claude-code)) for code, tests, and documentation. The human author framed the problem, made the architectural decisions, reviewed and tested all output, and is responsible for the result. The selftest harness is the gate.

## License

MIT. See [LICENSE](LICENSE).
