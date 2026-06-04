---
hide:
  - navigation
---

# holotype

> *holotype, n. (taxonomy): the single physical specimen used when a species is formally described. Every later observation is compared against it.*

**The best way to understand holotype is to have Claude clone the repo, read it, and ask it questions.**

```bash
git clone https://github.com/PEEKPerformer/holotype.git
# then, in Claude Code / Codex / any skills-capable CLI:
#   "read this whole repo and explain what it does and why"
```

This site exists for the other case: when a *human* wants to fully understand what we're doing and why, before trusting it with the record of a scientific experiment.

A tool that preserves scientific provenance has no business being a black box. Everything below is the *why* behind the code. None of it asks you to take our word for anything. The last page shows you how to verify an archive with stock Unix tools and no holotype installed at all.

---

## What it is, in one breath

holotype deposits agent-CLI conversation transcripts (Claude Code, Codex, Google Antigravity, and any other CLI you wire in) into a content-addressable, **hash-chained git archive** suitable for citation in scientific publications.

It backs up your transcripts and it lets you search them, but it is **more than a backup and more than a search box.** A plain backup proves nothing about what it stored. A search index throws away the tool calls to go fast. holotype keeps every byte, binds the set and order of deposits with an append-only hash chain, and gives you a single 64-hex anchor you can cite in a Data Availability Statement.

!!! info "This is deliberately overkill"
    No journal today requires provenance this strong. That is the point. holotype captures at the ceiling: verbatim bytes, a tamper-evident chain, verification that needs no trust. From there you can give any journal exactly the subset it asks for. The reverse never works: you can provide less than you kept, never more. The April 2026 transcripts are gone because they were kept to the standard of the day, which was nothing.

!!! note "Built for scientists, not engineers"
    You do not need to know what a hash chain, git, git-crypt, or compression is to use holotype. Setup is a conversation that asks which technologies you are familiar with and adapts its language to match. The first version assumed all of that, because it was written for one engineer. It was then hardened by watching non-technical scientists run it and fixing whatever tripped them.

<div class="grid cards" markdown>

-   :material-history:{ .lg .middle } **Why it exists**

    ---

    A 25-hour autonomous experiment. A headline scientific result. The transcripts, gone. Pruned by a default setting. The hole holotype was built to close.

    [:octicons-arrow-right-24: The origin story](why.md)

-   :material-scale-balance:{ .lg .middle } **The six non-negotiables**

    ---

    Forensic completeness. A real hash chain. Git as substrate. Environment capture. Append-only. Consent-driven networking. The principles, and the reasoning.

    [:octicons-arrow-right-24: Design philosophy](principles.md)

-   :material-cog-outline:{ .lg .middle } **How it works**

    ---

    Ingest, manifest, the append-only ledger, per-source layouts, dedup. The machinery, plainly.

    [:octicons-arrow-right-24: Architecture](how-it-works.md)

-   :material-shield-check:{ .lg .middle } **Verify it yourself**

    ---

    The archive is a plain git repo. A reviewer five years out can check every hash with `shasum`, `jq`, and stock `python3`. No holotype required.

    [:octicons-arrow-right-24: Verification](verify.md)

</div>

---

!!! quote "The thesis, restated"
    holotype assumes you will eventually distrust it, and is built so that distrust costs you nothing. The transcripts are verbatim. The chain is walkable without us. The substrate is git. We are not asking you to believe; we are handing you the means to check.
