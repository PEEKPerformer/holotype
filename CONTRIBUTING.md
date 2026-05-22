# Contributing to holotype

Thanks for considering a contribution. `holotype` is a single-maintainer scientific tool with strong opinions about its non-negotiables, but it's deliberately built to be extended (new agent-CLI sources, new compression backends, new verification tracks). This document describes how to contribute in a way that's likely to land.

## Before you start

Read these three documents first:

- [README.md](README.md) — what holotype is, why it exists, the non-negotiables.
- [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md) — the field guide for adding support for a new agent CLI.
- [docs/ROADMAP.md](docs/ROADMAP.md) — currently in-scope ideas and explicit non-goals.

If your contribution doesn't fit any of those, open an issue first to discuss before writing code.

## Non-negotiables (won't merge a PR that breaks these)

These are documented in detail in `SKILL.md`'s "non-negotiables" section. The short version:

1. **Forensic completeness.** Tool calls, tool results, thinking blocks, system reminders, hook outputs, image attachments — every byte preserved verbatim. No filtering, no "noise" removal.
2. **Provenance via hash chain.** Every deposited session has a SHA-256 in its manifest. Verification must be runnable with stock Unix tools (`shasum`, `jq`, `git`, optionally `zstd`) — no holotype-specific binary required.
3. **Git is the archive substrate.** Not SQLite, not a tarball.
4. **Environment capture.** Each manifest records host-CLI version, OS, holotype version, project-repo git state.
5. **Append-only by design.** Re-ingesting the same session is a no-op (SHA-256 dedup). Manifests are never rewritten in place except when `manifest_version` bumps.
6. **No silent network behavior.** Setup is an explicit conversation about remote URL choice. Auto-push fires only when the user opted into it at init time.

A PR that softens any of these needs explicit prior agreement in an issue.

## Development workflow

### 1. Open an issue first (for non-trivial changes)

For typo fixes, single-file docs improvements, or obvious bugs with clear-cut fixes, skip the issue and open a PR directly. For anything else — new features, refactors, new sources, schema changes — open an issue first and let me weigh in on the approach before you invest implementation time.

### 2. Fork and clone

```bash
gh repo fork PEEKPerformer/holotype
git clone <your-fork-url>
cd holotype
```

### 3. Branch naming

Use one of these prefixes:

- `feat/<short-name>` — new functionality
- `fix/<short-name>` — bug fix
- `docs/<short-name>` — documentation-only
- `perf/<short-name>` — performance change with no functional impact
- `chore/<short-name>` — tooling, CI, gitignore, etc.

Keep branches focused. One conceptual change per PR.

### 4. Make your change

Standard Python development. The project is stdlib-only on the Python side; please don't introduce third-party Python dependencies without strong justification in the PR description. External binary dependencies (`zstd`, `git-crypt`, `gpg`) are case-by-case — `holotype` already requires `git` + `shasum`/`jq` for verification, and the bar for adding more is "this enables a non-trivial new capability that can't be done in stdlib."

### 5. Run the selftest

```bash
python scripts/selftest.py
```

The selftest is the integration gate. It exercises ~30 end-to-end paths (init, ingest, verify, search, cite, paper_bundle, all three sources, compression on/off, encryption refusal paths, parallel mode, auto-chunking, per-chunk push). A PR that doesn't pass selftest won't be merged.

If your change adds a feature, add a selftest assertion that exercises it. Look at the existing selftest for the pattern — most additions are 10-30 lines.

### 6. Commit message conventions

The current history uses neutral release-notes voice. New commits should match:

- Subject line: imperative mood, ≤72 chars, no trailing period. Optional `feat:`/`fix:`/`docs:`/`perf:`/`chore:` prefix matching your branch.
- Body: explain *why* this change is being made, not just *what* changed. Wrap at ~80 chars.
- Avoid first-person session-narrative voice. Avoid quoting internal conversations. Avoid dated personal asides. The CHANGELOG and commit history are public artifacts cited from papers.

Look at recent commits for the pattern.

### 7. Open the PR

- Title: same conventions as commit subject lines.
- Description: link the issue (if one exists), explain the change, note any breaking changes or schema bumps, describe how you tested it beyond the selftest.
- The CI workflow (`.github/workflows/selftest.yml`) runs `scripts/selftest.py` on Ubuntu + macOS against Python 3.11 and 3.12. CI must be green before merge.

## What changes are likely to land

- **New `Source` classes** for additional agent CLIs (Cline, Cursor, aider, Open Interpreter, etc.). See `docs/ADDING_A_SOURCE.md` — the contract is small and well-tested.
- **Performance improvements** that don't break the non-negotiables. See `docs/ROADMAP.md` for in-scope items.
- **Documentation improvements** — especially clearer error messages, better wizard text, additional `docs/` walkthroughs for specific journals or specific paper-deposit workflows.
- **Bug fixes** with selftest assertions that would have caught the bug.
- **Cross-platform fixes** — particularly for Linux and Windows, since those platforms are less rigorously tested.

## What changes are unlikely to land

- New SQLite columns that duplicate data already on disk in `transcript.jsonl` (we removed `messages.raw_json` in v1.2 for exactly this reason — see `docs/ROADMAP.md` "Explicit non-goals").
- Features that auto-trigger holotype on conversational cues (the skill is user-invocable only by design — see `SKILL.md`).
- Crypto reimplementations of upstream tools (we use stock `git-crypt`; we don't reimplement its format).
- GPU-acceleration scaffolding (the workload is I/O-bound, see ROADMAP "Explicit non-goals").
- Bundling proprietary tools or non-OSI licenses.

## Reporting bugs

Use the GitHub Issues tab. Bug reports should include:

- holotype version (`python -c "import holotype; print(holotype.__version__)"`)
- OS + Python version + relevant external binary versions (`zstd --version`, `git-crypt --version`, `gpg --version`)
- Exact command you ran
- What you expected vs. what happened
- A minimal reproduction (a fixture file or a description of the on-disk state)

A bug report that includes the output of `scripts/verify.py --json` on the affected archive is easier to triage than one that doesn't.

## Suggesting features

Use the GitHub Issues tab with the "feature request" template. Before opening, check `docs/ROADMAP.md` to see if the idea is already filed (in either the candidate list or the explicit non-goals section).

## Security disclosures

For security issues — credential leakage, hash-chain bypass, key-recovery attacks against the git-crypt deposit format — don't open a public issue. See [SECURITY.md](SECURITY.md) for the disclosure process.

## A note on AI-assisted contributions

`holotype` itself was built with substantial AI assistance (see the AI assistance disclosure in `README.md`). Contributions that use AI tools (Claude Code, Copilot, Cursor, etc.) are fine and don't need to be flagged — but the standard JOSS expectation applies: the human contributor is responsible for the correctness, originality, and licensing of the submitted code, and must have reviewed and tested all AI-assisted output. The selftest is the line: if it passes and the change is sensible on inspection, the source of the original draft doesn't matter.

## Code of Conduct

By contributing, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). The short version: be respectful, focus on the work, assume good faith.

## License

By contributing, you agree that your contributions will be licensed under the MIT License that covers the project. See [LICENSE](LICENSE).
