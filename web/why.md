# Why holotype exists

holotype was not designed in the abstract. It was built in response to a specific data-loss event and a specific publication requirement. Both are worth telling, because together they define exactly what the tool must guarantee.

## The trigger

On the weekend of April 4–5, 2026, autonomous Claude Code sessions drove a 25-hour experiment on a thermoelectric measurement rig. The sessions produced the headline result of the project: **ionic-gel equilibration takes 41 minutes, not the 60–120 seconds the lab had been using.** That correction retroactively invalidated a chunk of the standard Seebeck-coefficient method for that material class. It is the kind of finding a paper is built around.

Weeks later, when it came time to cite the verbatim transcripts of those sessions in a planned *Digital Discovery* paper, **they were gone.** Claude Code's default `cleanupPeriodDays: 30` had pruned them. What survived was Claude's own saved memory notes: an *outline* of what happened, not the record.

## The realization

Memory notes and rsync backups together still don't deliver what a paper needs:

- **Memory** is a summary. It says *what was concluded*, not *what was said and done, byte for byte*.
- **rsync** preserves bytes but has no manifest, no hash chain, no semantic structure, and no way for a reviewer to prove the file they're reading is the file that was written.

For a forensic claim in peer review (*"we ran this LLM, with these tools, on this code, and it produced these decisions"*), an outline is not enough. You need a citable, hash-verifiable, immutable record. That record did not exist, and no existing tool produced it.

## The publication requirement

*Digital Discovery*'s Data Availability Statement requires LLM input/output logs, model identifiers, and generation dates. holotype is the substrate that makes such a statement *satisfiable*: not as a promise, but as a verifiable artifact a reviewer can check.

## Why not an existing tool?

We looked. Each was built for a different job:

| Tool | What it does | Why it's the wrong fit |
|---|---|---|
| **claude-vault** (Rust, SQLite + FTS5) | Fast full-text search over sessions | Explicitly **strips tool calls and system tags as "noise"** before indexing. The right call for search, the wrong call for provenance. The tool calls *are* the experimental record. |
| **claude-conversation-extractor** (Python) | Export sessions, with a `--detailed` flag | Closer, but search-first by design, not provenance-first. |
| **Manual rsync** | Copy the raw files | Preserves bytes, but no manifest, no hash chain, no semantic structure, no reviewer-facing verification. |

The single distinguishing line: **the difference between a search index and a forensic record is whether you keep the tool calls.** holotype fills the niche the search tools deliberately leave empty.

## The test every future change must pass

> Does this change strengthen or weaken the chain from a paper's citation back to a hash-verifiable transcript?

Backups, search, and convenience come second. The April 2026 incident is the load-bearing memory: any design move that re-opens that hole is rejected.

[:octicons-arrow-right-24: The principles that follow from this](principles.md)
