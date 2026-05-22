# Governance

`holotype` is currently a **solo-maintainer project**. This document describes how that works in practice and what to expect.

## Roles

- **Project lead / maintainer**: Brenden Ferland ([@PEEKPerformer](https://github.com/PEEKPerformer))
  - Sole final decision-maker on architectural questions, feature scope, security disclosures, and releases
  - Responds to issues, reviews PRs, cuts releases
  - Maintains the non-negotiables documented in `SKILL.md` and `CONTRIBUTING.md`

- **Contributors**: anyone who lands a PR, opens a substantive issue, or contributes documentation. Contributors are listed implicitly via git history and (for major contributions) acknowledged in `CHANGELOG.md`.

## How decisions are made

### Day-to-day technical decisions

The maintainer makes them. For most changes — bug fixes, perf improvements, new tests, documentation polish, new `Source` classes — a contributor opens a PR, the maintainer reviews, the PR lands or doesn't.

### Architectural decisions

These need explicit prior discussion in an issue before code is written. "Architectural" means anything that:

- Changes the manifest schema (`manifest_version` bump)
- Changes the on-disk archive layout
- Changes the verification path (e.g., hash algorithm, compression backend)
- Adds a third-party Python dependency (the project is stdlib-only by policy)
- Adds an external-binary dependency beyond the current set
- Softens any non-negotiable from `SKILL.md`
- Touches the security boundary (Source root permissions, key handling, network behavior)

Rationale for every architectural decision is captured either in `CHANGELOG.md` (for shipped changes), `docs/ROADMAP.md` (for candidates and rejected ideas), or `SKILL.md` (for the stable rules).

### Disagreement resolution

The current process is: file an issue, articulate the disagreement, the maintainer responds with reasoning. If the disagreement is substantial and the maintainer's reasoning is wrong, the contributor can fork — `holotype` is MIT, and forks are explicitly fine. There is no consensus-based veto right now because there is no second maintainer.

This will change if and when the project grows a multi-maintainer team (see "Succession and growth" below).

## Release cadence

- **Patch releases (x.y.Z)**: bug fixes, docs, small perf wins. Cut as needed, often same-day for bugs caught by external users.
- **Minor releases (x.Y.0)**: new features, schema-compatible additions. Cut when a batch of features is ready and selftest is green.
- **Major releases (X.0.0)**: backwards-incompatible changes, archive-format changes, internal architecture shifts. Cut deliberately, with prior notice in `docs/ROADMAP.md`.

Every release ships with a `CHANGELOG.md` entry, a git tag with annotated message, and a GitHub Release page using the tag annotation as the body. As of v2.0.2 onward, releases are mirrored to Zenodo via the GitHub-Zenodo integration, minting a software DOI per version.

## Conflict of interest policy

The current maintainer:

- Is affiliated with the Adamson Lab at the University of Connecticut, which is the original use case driving `holotype`'s design.
- Has no financial relationship with Anthropic (the maker of Claude Code, one of `holotype`'s supported sources), OpenAI (Codex), or Google (Antigravity).
- Plans to cite `holotype` in his own scientific publications. The tool was developed to support that workflow, but the design is general (any LLM-driven research workflow that needs a forensic-grade transcript archive).

External contributors should disclose any potential COI in PR descriptions if relevant — e.g., "I work on $VENDOR's agent CLI and this PR adds a Source for it." This isn't disqualifying; it just helps maintainers and reviewers evaluate the change.

## Succession and growth

If the project grows to the point where solo maintenance is a bottleneck or single-point-of-failure risk, the maintainer will document a transition plan:

- Adding co-maintainers from sustained contributors
- Adopting a more formal RFC process for architectural decisions
- Defining a deprecation policy for archive-format changes that affects existing users
- Publishing a maintainer-emeritus process for handing off long-term

These changes will be made via a documented governance amendment (PR to this file, discussed in an issue, merged after community feedback). Until then, the model is "solo maintainer with strong opinions, listen to contributors, fork if needed."

## Acknowledgements

`holotype`'s development has been substantially AI-assisted (see the AI assistance disclosure in `README.md`). Contributors should feel free to use AI tools in their own work on the project — see `CONTRIBUTING.md` for the standard expectations. The maintainer remains responsible for what merges, regardless of how it was originally drafted.
