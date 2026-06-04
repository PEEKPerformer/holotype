# Holotype roadmap

Captured candidate work for future versions, with honest tradeoffs and rough impact estimates. Filed here (not in CHANGELOG: CHANGELOG is for shipped things) so the next contributor has a starting point.

## v1.2 candidates, SHIPPED in v1.2.0

The v1.2 list (deferred FTS, --fast-compress, mmap_size) shipped in `v1.2.0`. See `CHANGELOG.md` for the full set. Auto-chunking of oversized bulk-initial commits also landed there (was originally a v1.1.6 recovery-tool concept; promoted to ingest-side prevention).

### Investigate `git-crypt` long-running filter mode, INVESTIGATED, NOT AVAILABLE

Identified as the dominant cost in encrypted bulk-initial: per-file `git-crypt clean` filter forks at `git add` time (~50 ms each × N files = many minutes of pure subprocess startup on a multi-thousand-session archive). Git's process-filter protocol (since git 2.11) lets a filter program stay running across multiple files in one `git add`, eliminating the per-file fork cost.

**Status (checked 2026-05-22)**: git-crypt **0.8.0 does NOT implement** the process-filter protocol. Evidence:

- `git-crypt process` → `Error: 'process' is not a git-crypt command.`
- `strings $(which git-crypt)` shows `filter.git-crypt.smudge` and `filter.git-crypt.clean` references but no `filter.git-crypt.process` string. The protocol isn't compiled in.
- No `pkt-line` / `capability=` / `version=2` packet-protocol strings in the binary.

**Options going forward**:
- **Upstream PR**: implement the protocol in git-crypt's C++ ourselves. Well-specified (`Documentation/long-running-process-protocol.txt` in Git source) but real C++ work + we'd then depend on upstream merging + releasing.
- **Wrapper script** that speaks the process-filter protocol externally but calls `git-crypt clean` / `smudge` internally: still forks once per file internally, zero net win.
- **Live with it**: accept encrypted bulk-initial is ~1-2 hr for 6000 sessions on M-series, lean on the parallel-workers v2 design instead (parallel workers can each invoke git-crypt concurrently, partially amortizing the per-file fork cost).

**Decision**: Recommendation #3 (live with it + document honestly + lean on parallelism). Re-evaluate if/when git-crypt upstream merges process-filter support.

---

## v2 candidates, SHIPPED in v2.0.0

The v2 parallel-workers design shipped in `v2.0.0`. See `CHANGELOG.md` for the full release notes. Final shape:

- Workers do hash + compress + manifest-build + transcript-write each in their own process.
- Coordinator drains worker results in submission order; runs SQLite + git serially.
- Workers handle live-file safety themselves (each does its own stat/restat).
- `commit_deposit` was updated to add files by explicit path (not directory) so concurrent file writes don't conflate subagent commits into parent-session commits.
- Selftest covers both serial (`--workers 1`) and parallel (`--workers 4`) paths.

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
- **Auto-export of git-crypt key at init.** Creates a sense of "init handled it for me" that the threat model doesn't support. The user must export to a backup destination *they* picked. See `HOW_TO_BACK_UP_YOUR_KEY.md`.

---

## How to use this file

- **Adding a new candidate**: open a section with "Plan / Expected impact / Risk." Be honest about tradeoffs.
- **Shipping a candidate**: move the section to `CHANGELOG.md` under the version it shipped in. Delete from here.
- **Killing a candidate**: move to `## Explicit non-goals` with the rationale.
