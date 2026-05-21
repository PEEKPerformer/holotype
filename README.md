# holotype

Forensic-grade archival of Claude Code sessions for scientific reproducibility — packaged as a Claude Code skill with bundled Python scripts.

> *holotype, n. (taxonomy) — The single physical specimen used when a species is formally described. Every later observation is compared against it.*

This skill deposits Claude Code conversation transcripts into a content-addressable, hash-chained git archive suitable for citation in scientific publications. It is *not* a backup tool and it is *not* a search tool — both already exist. It is a **provenance** tool.

## Why this exists

Most Claude Code archival tools optimize for search, which means they filter "noise" — tool calls, system reminders, hook outputs — before storing. That is the wrong tradeoff for scientific reproducibility. The tool calls *are* the experimental record. The system reminders explain why the model behaved the way it did. The hook outputs prove which guardrails were active.

`holotype` preserves every byte. No filtering, no re-encoding, no clever normalization. The raw JSONL is the specimen.

## Design philosophy (non-negotiable)

1. **Forensic completeness.** Tool calls, tool results, thinking blocks, system reminders, hook outputs, image attachments — every byte preserved verbatim.
2. **Provenance via hash chain.** Each session has a SHA-256 manifest. Manifests reference the git commit that introduced them. Modification after deposit breaks the chain and is detectable.
3. **Git is the archive substrate.** Not SQLite, not a tarball. Git's content-addressable storage, commit history, and signed tags *are* the provenance system. We do not reinvent them.
4. **Environment capture.** Each session manifest records the Claude Code version, model IDs that appeared in the transcript, enabled plugins/skills, the working directory, the parent project's git HEAD at deposit time, and the OS/platform.
5. **Append-only by design.** Once deposited, a session is immutable. Re-ingesting the same session is a no-op (UUID deduplication).
6. **No silent network behavior.** Setup is an interactive conversation about where the archive lives and whether it has a remote. Push to remote is never automatic — always confirmed.
7. **Verifiable without the skill.** The archive is a plain git repo. A reviewer with no access to Claude Code can verify integrity using stock Unix tools. See the [VERIFY.md](#) that ships inside each archive.

## Architecture

```
~/Git/holotype/                  # this repo — the skill source
├── SKILL.md                     # what Claude reads when invoking the skill
├── holotype/                    # Python library: hashing, manifest, env capture
└── scripts/                     # CLI entry points the skill invokes via Bash

~/Documents/holotype-archive/    # the user's archive (created by the wizard)
├── .holotype/config.json        # archive config (portable: moves with the archive)
├── README.md                    # archive identity
├── VERIFY.md                    # standalone verification (no Claude needed)
└── sessions/
    └── <project-dir>/<session-id>/
        ├── transcript.jsonl     # raw, byte-for-byte
        └── manifest.json        # SHA-256, env capture, model IDs, timestamps
```

A pointer at `~/.config/holotype/archive-path` records the archive's location so future sessions can find it.

## Status

Pre-alpha. Skill metadata + init wizard implemented. Ingest, search, cite, verify, and context-load scripts are next.

## Install

`holotype` is an [agent skill](https://agentskills.io) and works in any CLI that implements the standard — Claude Code, Codex, and others. The skill format (SKILL.md + scripts/) is identical across implementations; only the discovery path differs.

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

## Usage

`holotype` is **deliberately manual**. It is never auto-triggered by phrases or keywords — every invocation is an explicit user action, because depositing data into a scientific archive is the kind of decision a human should make consciously.

### Inside a skills-supporting CLI

Invoke the skill explicitly:

```
/holotype    # Claude Code
$holotype    # Codex
```

The first invocation walks you through the interactive setup wizard (archive location, remote choice, background-tick opt-in). Subsequent invocations let you deposit, search, cite, verify, or load context — the host LLM will ask which operation you want.

### From any shell, without an LLM

Scripts are directly runnable. This is also how the macOS launchd background tick invokes them — no LLM required:

```bash
python scripts/init.py --path ~/Documents/holotype-archive --remote-url "" --remote-kind none
python scripts/ingest.py
python scripts/install-launchd.py --archive ~/Documents/holotype-archive
```

The skill is convenience automation over the scripts. The scripts are the engine.

## License

MIT. See [LICENSE](LICENSE).
