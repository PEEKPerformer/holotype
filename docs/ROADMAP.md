# Holotype roadmap

Captured candidate work for future versions, with honest tradeoffs and rough impact estimates. Filed here (not in CHANGELOG — CHANGELOG is for shipped things) so the next contributor has a starting point.

## v1.2 candidates (incremental, no architectural risk)

### Deferred FTS rebuild on bulk ingest

Currently `reindex_session()` populates `messages_fts` via `executemany` per session. SQLite FTS5 supports an explicit `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` operation that batches segment merges across many sessions much faster than per-row inserts.

**Plan**: For `--bulk-initial` only, skip the `messages_fts` insert in `reindex_session` and call `rebuild` once at end-of-cycle.

**Expected impact**: ~1.3× on bulk-initial. Negligible on steady-state ingest.

**Risk**: Low. Behavior change is contained to bulk-initial path.

---

### `--fast-compress` flag for `--bulk-initial`

Compression is currently locked at `zstd -19 --long=27` — optimizing for ratio over speed, which is right for archival data but wasteful when the user is waiting in the foreground during first-time backfill.

**Plan**: Add `--fast-compress` (zstd -3) flag that pairs with `--bulk-initial`. Subsequent ingests stay at the archival level. Manifest's `compression` field would record `"zstd-fast"` vs `"zstd"` so reviewers know which level was used (verification still works — they hash the stored bytes either way).

**Expected impact**: ~2-3× on the compression step, ~1.1× overall during bulk-initial.

**Risk**: Low. Per-session compression level is allowed to vary because `sha256_compressed` records the actual stored bytes.

---

### `PRAGMA mmap_size=268435456` on the SQLite index

Our v4 index is ~160 MB on a 3000-session archive. Adding `mmap_size=256MB` gives SQLite room to map the whole DB into memory.

**Plan**: One line in `_connect()` in `holotype/index.py`.

**Expected impact**: ~1.05-1.1× on search-heavy workloads. Negligible on bulk-ingest (already disk-bound on writes).

**Risk**: Trivial.

---

### Investigate `git-crypt` long-running filter mode — INVESTIGATED, NOT AVAILABLE

Identified as the dominant cost in encrypted bulk-initial: per-file `git-crypt clean` filter forks at `git add` time (~50 ms each × N files = many minutes of pure subprocess startup on a multi-thousand-session archive). Git's process-filter protocol (since git 2.11) lets a filter program stay running across multiple files in one `git add`, eliminating the per-file fork cost.

**Status (checked 2026-05-22)**: git-crypt **0.8.0 does NOT implement** the process-filter protocol. Evidence:

- `git-crypt process` → `Error: 'process' is not a git-crypt command.`
- `strings $(which git-crypt)` shows `filter.git-crypt.smudge` and `filter.git-crypt.clean` references but no `filter.git-crypt.process` string. The protocol isn't compiled in.
- No `pkt-line` / `capability=` / `version=2` packet-protocol strings in the binary.

**Options going forward**:
- **Upstream PR**: implement the protocol in git-crypt's C++ ourselves. Well-specified (`Documentation/long-running-process-protocol.txt` in Git source) but real C++ work + we'd then depend on upstream merging + releasing.
- **Wrapper script** that speaks the process-filter protocol externally but calls `git-crypt clean` / `smudge` internally — still forks once per file internally, zero net win.
- **Live with it**: accept encrypted bulk-initial is ~1-2 hr for 6000 sessions on M-series, lean on the parallel-workers v2 design instead (parallel workers can each invoke git-crypt concurrently, partially amortizing the per-file fork cost).

**Decision**: Recommendation #3 (live with it + document honestly + lean on parallelism). Re-evaluate if/when git-crypt upstream merges process-filter support.

---

## v2 candidates (architectural)

### Parallelize per-session ingest

The hot path is currently single-process: hash → compress → encrypt → manifest write → SQLite upsert → git stage → commit. The first three are CPU-bound and independent per session; only the SQLite + git steps need serialization.

**Plan**: Stage pipeline:

```
[Pool of N workers, parallel] → [Single coordinator]
- read JSONL                     - write manifest.json + transcript
- compute sha256                 - git add + commit (or accumulate
- compress (zstd)                  for bulk-initial)
- (re)encrypt for git-crypt      - upsert sessions row + FTS insert
  if applicable
- parse Source metadata
- build manifest dict
```

The workers produce `(manifest, transcript_bytes, compressed_bytes)` tuples; the coordinator drains them in order and does serialized writes. On an M-series with 8 performance cores, plausibly 4-6× total throughput.

**Expected impact**: 4-6× on bulk-ingest workloads. Most user-visible improvement.

**Risk**: Architecturally invasive. Needs a real design doc before code:

- How does live-file safety (re-check mtime after read) interact with worker pool latency?
- How do per-session commits interleave when workers finish out of order?
- What happens to git-crypt filter forks under parallel `git add`?
- How does the SIGINT-during-ingest path stay clean?

This is the right v2 lift. Don't ship it as a v1.x patch.

---

### In-process git-crypt format implementation

Proposed by a separate analysis as a 10-50× speedup on encrypted bulk-initial: bypass git-crypt's filter entirely, encrypt in Python using the documented AES-256-CTR-with-HMAC-SHA1-IV format, stage via `git hash-object -w --no-filters`.

**Decision**: **Rejected for v1.x and likely v2.** Pushes crypto surface area into holotype that we'd own forever, ties us to a specific git-crypt format version (we'd have to refuse on upgrades), and makes "this archive is encrypted with stock git-crypt" subtly false. Forensic tools shouldn't fragment around standard formats for perf.

Reconsider only if:
- git-crypt upstream goes unmaintained (currently active as of 2026-05)
- The process-filter mode investigation above turns out to be impossible
- Encrypted bulk-initial becomes a frequent first-time blocker

---

## Explicit non-goals

- **GPU acceleration.** Workload is small-file I/O + serialized SQLite + AES-NI / SHA-NI hardware-accelerated crypto. GPU adds PCIe round-trip cost without benefit on this shape. See the analysis discussion in commit history.
- **`pip install` console scripts for `scripts/<name>.py`.** Some filenames have hyphens that can't be Python module names. Documented invocation stays `python scripts/<name>.py`.
- **Auto-export of git-crypt key at init.** Creates a sense of "init handled it for me" that the threat model doesn't support. The user must export to a backup destination *they* picked — see `HOW_TO_BACK_UP_YOUR_KEY.md`.

---

## How to use this file

- **Adding a new candidate**: open a section with "Plan / Expected impact / Risk." Be honest about tradeoffs.
- **Shipping a candidate**: move the section to `CHANGELOG.md` under the version it shipped in. Delete from here.
- **Killing a candidate**: move to `## Explicit non-goals` with the rationale.
