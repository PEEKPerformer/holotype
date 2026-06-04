# Cite & install

## Install

holotype is set-and-forget. An LLM reads the repo and sets it up once: it creates the archive and installs the background tick (launchd on macOS, systemd on Linux) that deposits new sessions on its own from then on. There is no daemon to babysit and nothing to keep launching. The clone *is* the install.

```bash
git clone https://github.com/PEEKPerformer/holotype.git ~/Git/holotype
```

Point your agent at that repo and tell it to set holotype up. The scripts under `scripts/` are stdlib-only Python and run with no LLM, so the tick keeps depositing long after that first conversation ends. Keep the clone in place: the background tick runs it directly.

### Optional: install as a skill

You need this only if you want an agent to help with later one-off operations (search, cite, verify, browse) by invoking `/holotype`, instead of re-cloning or naming scripts. The core archiving never needs it, because setup already installed the background tick. If you do want it, symlink the repo into your CLI's skill directory so the agent can discover it. The same `SKILL.md` + `scripts/` works across Claude Code, Codex, Gemini CLI, GitHub Copilot, Cursor (manual), Goose, OpenCode, OpenHands, and 30+ other clients; only the discovery path differs.

```bash
# Claude Code
ln -s ~/Git/holotype ~/.claude/skills/holotype

# Codex
ln -s ~/Git/holotype ~/.agents/skills/holotype
```

You can do both: one source serves both CLIs. Per-repo installs work too (drop it under `.claude/skills/` or `.agents/skills/` inside a project).

### Dependencies

The Python side is **stdlib-only**. External binaries:

- **git**: required (the archive substrate)
- **shasum** / **sha256sum**, **jq**: required for verification
- **zstd**: optional, only for compressed deposits
- **gpg**: optional, only for signed commits

Python 3.11+. macOS and Linux fully supported (incl. background tick); Windows core works, scheduling left to Task Scheduler.

### First run

The engine is flag-driven, no LLM required:

```bash
python scripts/init.py --path ~/holotype-archive --remote-url "" --remote-kind none --compression auto
python scripts/ingest.py
```

If you installed the skill, invoke it instead and it runs a conversational two-question wizard (*where should the archive live?* and *back up to a private GitHub repo?*) that calls init for you, then sets up compression, signing, retention, the background tick, and your first deposit:

```
/holotype     # Claude Code
$holotype     # Codex
```

Either way, setup refuses iCloud-synced paths (they silently break the background job).

### Built Claude Code first

holotype is written Claude Code first, because that is what the maintainer uses. It runs the same scripts everywhere, but other CLIs get limited testing. Two properties keep that honest:

- **It is self-applying.** Drop an LLM into a CLI holotype doesn't recognize, point it at [`docs/ADDING_A_SOURCE.md`](https://github.com/PEEKPerformer/holotype/blob/main/docs/ADDING_A_SOURCE.md), and it can read the contract and write a working Source for its own host, validated by `scripts/selftest.py`. The docs are written for an LLM reader on purpose. The honest test of this design is that you should be able to hand the repo to an LLM and have it figure out how to apply holotype to its own CLI without a human translating.
- **It is idempotent.** Running ingest again is safe. An unchanged session dedups to a no-op, so a retry, a background-tick overlap, or a misfire can't double-deposit or corrupt anything.

[Issues and pull requests](https://github.com/PEEKPerformer/holotype/issues) for other CLIs are welcome.

## Cite

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20349311.svg)](https://doi.org/10.5281/zenodo.20349311)

Cite holotype if your paper relies on it in a load-bearing way:

- you used it to produce the transcripts your Data Availability Statement cites;
- you used `paper_bundle.py` to package transcripts for a Zenodo deposit;
- you used `verify.py`, the manifest schema, or any holotype output as evidence in your Methods.

Casual install/inspection doesn't need a citation.

!!! tip "Cite the version DOI, not the concept DOI"
    The concept DOI tracks *latest*; the version DOI pins the exact release your work used. Reproducibility wants the version. [`CITATION.cff`](https://github.com/PEEKPerformer/holotype/blob/main/CITATION.cff) carries the machine-readable metadata, and GitHub renders a "Cite this repository" button from it.

### A Data Availability Statement that satisfies the requirement

A *Digital Discovery*–style DAS naming LLM input/output, model identifiers, and generation date:

> *All LLM sessions referenced in this work are deposited at [Zenodo DOI]. Each session bundle contains the verbatim host-CLI JSONL transcript, a SHA-256 manifest, and verification instructions. The deposited archive at submission corresponds to commit `<archive-commit>` (hash-chain head `<64-hex head>`) of the holotype archive. Reviewers can verify any cited session via the procedure in the bundle's `VERIFY.md` using only stock Unix tools (`shasum`, `jq`, `python3`).*

The full Zenodo-side workflow (access modes, license, metadata template, sample DAS, versioning model, gotchas) is in [`docs/PUBLISHING_TO_ZENODO.md`](https://github.com/PEEKPerformer/holotype/blob/main/docs/PUBLISHING_TO_ZENODO.md).

## Go deeper

- [README](https://github.com/PEEKPerformer/holotype/blob/main/README.md): features, the full citation tutorial, platform notes
- [SKILL.md](https://github.com/PEEKPerformer/holotype/blob/main/SKILL.md): what the host LLM reads when you invoke the skill
- [Adding a source](https://github.com/PEEKPerformer/holotype/blob/main/docs/ADDING_A_SOURCE.md): wire in a new CLI (written for an LLM reader)
- [CHANGELOG](https://github.com/PEEKPerformer/holotype/blob/main/CHANGELOG.md) · [GOVERNANCE](https://github.com/PEEKPerformer/holotype/blob/main/GOVERNANCE.md) · [CONTRIBUTING](https://github.com/PEEKPerformer/holotype/blob/main/CONTRIBUTING.md)

---

...or, as the [front page](index.md) said: clone the repo and have Claude read it. This site was for the human. The code is the specimen.
