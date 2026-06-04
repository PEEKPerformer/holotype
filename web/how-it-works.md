# How it works

Five moving parts: **ingest**, the **manifest**, the **ledger**, **per-source layouts**, and **dedup**. None of them is clever in a way that hides what it's doing. That's deliberate.

## The shape of an archive

```
~/holotype-archive/                # a plain git repo: the whole product
├── .holotype/
│   ├── config.json                # archive config (portable, moves with the archive)
│   ├── ledger.jsonl               # the append-only hash chain
│   ├── index.sqlite               # derived FTS5 search index (gitignored, rebuildable)
│   └── cache/                     # decompressed-transcript cache (gitignored)
├── README.md                      # archive identity
├── VERIFY.md                      # standalone verification procedure (no holotype needed)
└── sessions/
    ├── <project-dir>/<session-id>/             # Claude Code top-level session
    │   ├── transcript.jsonl   (or .jsonl.zst)
    │   └── manifest.json
    ├── <project-dir>/<parent>/subagents/<sid>/ # Claude Code subagent, nested under parent
    ├── codex/<YYYY>/<MM>/<DD>/<sid>/           # Codex, date-partitioned
    └── antigravity/<sid>/                      # Google Antigravity
```

A pointer at `~/.config/holotype/archive-path` records the location so future sessions find it.

## 1. Ingest

`scripts/ingest.py` walks each registered Source's default directories, finds sessions that aren't already deposited, and writes one git commit per session. For each:

1. Read the raw transcript, **never mid-write.** A live-file safety re-checks mtime after the read; a file still being written is skipped this cycle and caught on the next.
2. Compute the canonical SHA-256 of the **uncompressed** bytes (the citation hash).
3. Optionally compress with `zstd`, recording a *second* hash of the bytes as-stored.
4. Capture the environment (see [principle 4](principles.md#4-environment-capture)).
5. Write `transcript.jsonl[.zst]` + `manifest.json`, commit, append a ledger link.

A background tick (macOS launchd, Linux systemd) can run ingest on a schedule so long-running sessions are caught up without manual action.

!!! warning "The background tick runs the live working tree"
    On macOS the launchd job runs the checked-out repo directly. Any branch or uncommitted edit goes live on the real archive at the next tick. Pause the job before working on the forensic core.

## 2. The manifest

One `manifest.json` per session, carrying everything a reviewer needs to reproduce and verify:

- `sha256`: uncompressed JSONL, **the citation hash**
- `sha256_compressed`: the `.jsonl.zst` as stored (present only when compression is on)
- model IDs that appeared in the session
- `project_git_state.commit`: the repo state the LLM operated on
- `wall_clock_seconds`, `total_input_tokens`, `total_output_tokens`, `total_cache_*_tokens`
- host CLI version, OS/platform, holotype version, manifest schema version

??? note "Why two hashes when compression is on"
    The uncompressed hash is the *canonical* identity: it's what you cite, and it's stable whether or not compression was ever enabled. The compressed hash lets a reviewer who doesn't have `zstd` installed still verify the file as-stored. Either track is sufficient; both must succeed for a valid deposit. See [Verify it yourself](verify.md).

??? note "Schema bumps are manifest-only"
    Manifests carry a `manifest_version`. When it bumps, the next ingest backfills old deposits, with no migration script. Schema migrations rewrite *manifests only*, never transcripts: rewriting transcripts would re-pack history and blow past git's ~2 GiB pack limit, making the archive unpushable.

## 3. The ledger: the hash chain made literal

`.holotype/ledger.jsonl` is an append-only file. Each line is one **link** committing to the previous link's `chain_hash`:

```
link[n].chain_hash = SHA-256( link[n].content_identity ‖ link[n-1].chain_hash )
```

The final link's `chain_hash` is the **head**: a single 64-hex string. That's what you cite.

What this buys you that per-file hashing alone cannot:

- **Insertion or deletion** of a deposit breaks the chain: every downstream `chain_hash` stops matching.
- A **co-edited transcript+manifest** (internally consistent, so per-file checks pass) appears as an **orphan**: content with no link, or a link with no content.

Two design choices worth knowing:

- **One global ledger**, not a prev-hash baked into each manifest. A per-manifest chain would force rewriting every downstream manifest on each append, re-packing history toward the 2 GiB ceiling.
- **Links commit to content identity only.** A pure schema migration changes manifests but not content, so it appends **zero** links. The chain tracks *what was deposited and in what order*, not bookkeeping.

The ledger is bootstrapped automatically by ingest; `scripts/build_ledger.py` can rebuild or re-seal it, which is rarely needed.

## 4. Per-source layouts

Each host CLI stores sessions differently, so each [Source](https://github.com/PEEKPerformer/holotype/blob/main/docs/ADDING_A_SOURCE.md) maps its native shape into the archive:

- **Claude Code** nests subagents under their parent session (`<parent>/subagents/<sid>/`), keeping the call hierarchy intact.
- **Codex** date-partitions rollouts (`codex/<YYYY>/<MM>/<DD>/<sid>/`).
- **Antigravity** uses a flat per-session layout.

Adding a CLI is one file in `holotype/sources/` against a documented `Source` ABC, validated by `scripts/selftest.py`. The field guide is written for an *LLM* reader: invoke the skill in an unrecognized CLI and it will read the guide and write a working Source for its own host.

## 5. Dedup

Ingest is idempotent. Re-ingesting an unchanged session is a no-op: dedup is by `(source, session_id)` with a prior-SHA check; first occurrence wins. A session that has *grown* since last deposit gets an `update` commit with a new manifest hash, and git history preserves every prior state. Nothing is ever overwritten in place.

---

## Two front ends, one engine

The Python scripts under `scripts/` are the engine and run fine with no LLM at all, which is exactly how the background tick invokes them:

```bash
python scripts/init.py --path ~/holotype-archive --remote-url "" --remote-kind none --compression auto
python scripts/ingest.py            # deposit any new sessions
python scripts/search.py "ionic gel"
python scripts/verify.py            # per-file + hash-chain check
python scripts/browse.py            # local-only in-browser viewer, zero disk cache
python scripts/paper_bundle.py --sessions a,b,c --out ./zenodo-deposit/ --tarball
```

The skill (`SKILL.md` + the wizard) is convenience automation *over* those scripts. The scripts are the engine, and they're what a reviewer or a skeptic actually runs.

[:octicons-arrow-right-24: What we deliberately do *not* do](non-goals.md)
