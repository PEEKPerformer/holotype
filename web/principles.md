# The six non-negotiables

These principles drove every architectural decision. They are not aspirations. They are the constraints a change must satisfy to be accepted. Each is stated with the reasoning behind it, because *why* a rule exists is what tells you when it's being violated.

## 1. Forensic completeness

Tool calls, tool results, thinking blocks, system reminders, hook outputs, image-attachment references. **Every byte preserved verbatim. No filtering. No "noise" removal.**

**Why.** The only difference between a search index and a forensic record is whether you keep the tool calls. The tool calls *are* the experimental record. The system reminders explain why the model behaved as it did. The hook outputs prove which guardrails were active. Discard them and you have a summary, not a specimen.

**In practice.** Compressing the bytes (zstd) is fine. Discarding them is not: no manifest, transcript, or index ever drops content "to save space."

## 2. Provenance via a real hash chain

Two layers:

- **(a) Per-file integrity.** Each session gets a SHA-256 manifest covering its content.
- **(b) Cross-deposit integrity.** An append-only, hash-chained ledger (`.holotype/ledger.jsonl`) records one link per *content event*, each committing to the previous link's hash. The chain **head** is a single 64-hex anchor you cite in a Data Availability Statement.

`verify.py` walks both layers. Per-file hashing alone can be fooled by a co-edited transcript+manifest (internally consistent, so each checks out against the other). The ledger catches it: such a pair appears as an **orphan**, and any insertion or deletion appears as a **broken chain**.

??? note "History: the chain used to be aspirational"
    Before v2.4.0, "hash chain" meant per-file manifests plus git's commit DAG. `verify.py` checked each file against its own manifest only. The gap: a `paper_bundle` tarball ships with no `.git`, so it carried no cross-deposit binding. v2.4.0 made the chain literal. Two deliberate design choices: **one global ledger** (not a per-manifest prev-hash, which would force rewriting every downstream manifest on each append, and hit git's ~2 GiB pack ceiling), and **links hash content identity only**, so a pure schema migration appends zero links.

**Why.** A reviewer five years out, reading a paper that cites session `3f1c4cf7`, must be able to (a) recompute the hashes and (b) walk the chain to the published head, *without trusting holotype itself.*

## 3. Git is the archive substrate

Not SQLite, not a tarball. Git's content-addressable storage, commit history, and signed tags *are* the provenance system. We don't reinvent them.

**Why.** Every alternative has worse audit properties. SQLite needs a custom verifier. A tarball has no history. S3 has no integrity guarantees the user controls. Git is the worst archive substrate except for all the others, and it's universal.

**In practice.** The SQLite index at `.holotype/index.sqlite` is *derived*: gitignored, rebuildable. That is the only acceptable role for non-git storage. Anything that reaches outside git for the *canonical* record is a design smell.

## 4. Environment capture

Each manifest records the host CLI version, the model IDs that appeared, OS/platform, the working directory, the parent project repo's git HEAD at deposit time, wall-clock duration, and aggregated token usage.

**Why.** "We ran Claude on this code" is incomplete. "We ran Claude X.Y.Z on commit `abc123` of repo R, with these tools enabled" is *reproducible*. A reviewer can re-checkout the same commit, install the same CLI version, and replay against the same tool surface.

**In practice.** A new Source captures the equivalent context for its CLI. The "boring" environmental signals are load-bearing: don't omit them.

## 5. Append-only by design

Once deposited, a session is immutable. Re-ingesting the same session is a no-op (SHA-256 dedup). A session that *grows* gets an `update` commit with a new manifest hash; git history preserves every prior state.

**Why.** Mutability is the enemy of provenance. If anyone can quietly edit a deposited transcript, the hash chain means nothing.

**In practice.** The live-file safety in `ingest.py` never deposits a file mid-write; it re-checks mtime after reading. Any pattern that requires modifying an existing manifest *in place* (rather than via a new git commit) is the wrong pattern.

## 6. Network behavior follows init-time consent, never silent defaults

Setup is an interactive conversation about where the archive lives, whether it has a remote, and whether to auto-push. Once those decisions are recorded in `config.json`, the runtime honors them, including auto-pushing after each ingest cycle when the user enabled it.

**Why (revised 2026-05-22).** The original rule was "never auto-push, ever." That was wrong for the common case: a user with a private GitHub repo who forgets to push, and loses everything to a single disk failure, a worse outcome than the privacy risk auto-push guards against. The privacy decision genuinely happens *once*, at remote-configuration time, where the wizard shows an explicit warning that transcripts will be pushed to the chosen remote. After that consent, auto-push is what the user expects.

What stays sacred:

- The remote URL is **never** set silently: always wizard-driven (or an explicit `--remote-url` flag).
- Auto-push requires a remote to exist. Local-only archives have nothing to push; `auto_push` is forced false.
- Auto-push fires **per ingest cycle, not per session**: one network call, not N.
- Push failures are warnings, not errors: the local deposit is always committed first.
- Embargo / IP-sensitive workflows choose `--no-auto-push` at init.

---

These six are also stated in the project's [`README.md`](https://github.com/PEEKPerformer/holotype/blob/main/README.md) and [`SKILL.md`](https://github.com/PEEKPerformer/holotype/blob/main/SKILL.md). This page is the long-form *why* behind each.

[:octicons-arrow-right-24: How the principles become machinery](how-it-works.md)
