"""Append-only hash-chained ledger — the archive's tamper-evidence layer.

Each transcript carries the SHA-256 of its own bytes in ``manifest.json``;
that proves *content* integrity but binds nothing to anything else. A
coordinated edit of a transcript and its manifest is internally consistent
and would pass per-file verification. What detects insertion, deletion, or
substitution of whole deposits is *ordering* — and until now the only thing
recording that order was git's commit DAG, which a self-contained
``paper_bundle`` ``.tar.gz`` (no ``.git``) doesn't carry.

This module is that ordering, made self-contained. ``ledger.jsonl`` is an
append-only transparency log: one line per *content event* (a new deposit or
a content update), each line committing to the previous line's hash. The tip
of the chain — ``head()`` — is a single 64-hex value that summarizes the
entire deposit history. Publish it (in a Data Availability Statement / Zenodo
record) and a reviewer can later walk the shipped ledger, confirm it reaches
that head, and confirm every transcript they hold is accounted for — without
the upstream repo.

Design choices (see the project plan for rationale):

- **One global ledger**, not a prev-hash field inside each manifest. Sessions
  grow (append-only logs) and manifests are re-derived on schema bumps; an
  in-manifest prev-hash would force rewriting every downstream manifest on any
  append. A separate log appends a new link per event and never rewrites.
- **Links hash content identity only** (``prev``, ``seq``, ``source``,
  ``session_id``, ``sha256``, ``deposited_at``) — never volatile/derived
  manifest fields. So a pure schema migration (transcript bytes unchanged)
  produces no new link and never perturbs the chain.
- **Stdlib only.** Mirrors ``holotype.hashing``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

LEDGER_VERSION = 1
LEDGER_RELPATH = ".holotype/ledger.jsonl"
GENESIS_DOMAIN = "holotype-ledger-v1"
ZERO_HASH = "0" * 64

# Canonical field order for the link pre-image. NEVER reorder or insert —
# doing so changes every chain_hash and breaks verification of existing
# ledgers. Append-only at the end is also unsafe (it would change hashes);
# a new ledger format would bump LEDGER_VERSION and live behind a migration.
_LINK_FIELDS = ("prev", "seq", "source", "session_id", "sha256", "deposited_at")


def ledger_path(archive: Path) -> Path:
    return archive / LEDGER_RELPATH


def genesis_hash(archive: Path) -> str:
    """Domain-separated genesis hash, tied to this archive's identity.

    Anchoring genesis to the archive's ``created_at`` means a chain lifted
    from a different archive can't be grafted onto this one and still verify.
    Falls back to all-zeros if the config can't be read (a fresh archive that
    hasn't written config yet — the backfill will re-derive once it exists).
    """
    config_path = archive / ".holotype" / "config.json"
    created_at = ""
    try:
        config = json.loads(config_path.read_text())
        created_at = str(config.get("created_at") or "")
    except (OSError, json.JSONDecodeError):
        created_at = ""
    if not created_at:
        return ZERO_HASH
    return hashlib.sha256(f"{GENESIS_DOMAIN}\n{created_at}".encode("utf-8")).hexdigest()


def compute_chain_hash(
    prev: str,
    *,
    seq: int,
    source: str,
    session_id: str,
    sha256: str,
    deposited_at: str,
) -> str:
    """The one canonical link function — used by both writer and verifier.

    Pre-image is the ``_LINK_FIELDS`` values joined by newlines, in fixed
    order. Stable across compression mode and manifest schema because none of
    those fields is part of the pre-image.
    """
    values = {
        "prev": prev,
        "seq": str(seq),
        "source": source,
        "session_id": session_id,
        "sha256": sha256,
        "deposited_at": deposited_at,
    }
    preimage = "\n".join(values[f] for f in _LINK_FIELDS)
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def read_entries(archive: Path) -> list[dict]:
    """Parse ledger.jsonl into a list of entry dicts (empty if absent)."""
    path = ledger_path(archive)
    if not path.exists():
        return []
    entries: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def head(archive: Path, *, entries: list[dict] | None = None) -> str:
    """Chain tip: the last entry's chain_hash, or genesis if the log is empty.

    Pass ``entries`` to avoid a re-read when the caller already has them.
    """
    if entries is None:
        entries = read_entries(archive)
    if not entries:
        return genesis_hash(archive)
    return entries[-1]["chain_hash"]


def make_entry(
    prev: str,
    *,
    seq: int,
    event: str,
    source: str,
    session_id: str,
    sha256: str,
    deposited_at: str,
) -> dict:
    """Build a complete entry dict (including its computed chain_hash)."""
    chain_hash = compute_chain_hash(
        prev,
        seq=seq,
        source=source,
        session_id=session_id,
        sha256=sha256,
        deposited_at=deposited_at,
    )
    return {
        "seq": seq,
        "event": event,
        "source": source,
        "session_id": session_id,
        "sha256": sha256,
        "deposited_at": deposited_at,
        "prev": prev,
        "chain_hash": chain_hash,
    }


def _entry_line(entry: dict) -> str:
    # sort_keys for byte-stable lines; the chain_hash itself does not depend
    # on this serialization (it's computed from the fixed pre-image), so the
    # on-disk JSON layout is free to be whatever's most diff-friendly.
    return json.dumps(entry, sort_keys=True) + "\n"


class LedgerWriter:
    """Serialized appender. Construct under the ingest flock, before the
    drain loop; reuse across all appends in one ingest cycle so the bulk
    path doesn't re-read the file per session.

    Not thread-safe and not meant to be — ingest's commit/drain step is
    single-threaded by design (the parallel workers only hash/write files;
    the coordinator drains results and commits in order).
    """

    def __init__(self, archive: Path):
        self.archive = archive
        existing = read_entries(archive)
        self._seq = len(existing)
        self._prev = head(archive, entries=existing)
        path = ledger_path(archive)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def append(
        self,
        *,
        source: str,
        session_id: str,
        sha256: str,
        deposited_at: str,
        event: str = "deposit",
    ) -> dict:
        entry = make_entry(
            self._prev,
            seq=self._seq,
            event=event,
            source=source,
            session_id=session_id,
            sha256=sha256,
            deposited_at=deposited_at,
        )
        self._fh.write(_entry_line(entry))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._prev = entry["chain_hash"]
        self._seq += 1
        return entry

    @property
    def head(self) -> str:
        return self._prev

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "LedgerWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def write_ledger(archive: Path, entries: list[dict]) -> Path:
    """Atomically (re)write the whole ledger from a list of entries.

    Used by the backfill / bootstrap path. Steady-state ingest appends via
    ``LedgerWriter`` instead. Writes through a ``.partial`` temp + rename so
    a crash never leaves a truncated ledger.
    """
    path = ledger_path(archive)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.partial")
    tmp.write_text("".join(_entry_line(e) for e in entries), encoding="utf-8")
    os.replace(tmp, path)
    return path


def backfill_entries(archive: Path) -> list[dict]:
    """Build a complete chain from the archive's current on-disk deposits.

    Deterministic order: ``(deposited_at, session_id)``. The true historical
    deposit order already lives in git's commit DAG; this is a reproducible
    *seal checkpoint*, not a reconstruction of original event order. One link
    per on-disk session (the session's current sha256) — historical update
    links aren't recoverable from current state, which is why a healthy live
    ledger is never rebuilt (the caller no-ops when it already verifies).
    """
    from holotype.archive import iter_all_sessions

    rows = []
    for sess_dir, manifest in iter_all_sessions(archive):
        rows.append(
            (
                manifest.get("deposited_at") or "",
                manifest.get("session_id") or sess_dir.name,
                manifest.get("source") or "",
                manifest.get("sha256") or "",
            )
        )
    rows.sort(key=lambda r: (r[0], r[1]))

    entries: list[dict] = []
    prev = genesis_hash(archive)
    for seq, (deposited_at, sid, source, sha) in enumerate(rows):
        entry = make_entry(
            prev,
            seq=seq,
            event="deposit",
            source=source,
            session_id=sid,
            sha256=sha,
            deposited_at=deposited_at,
        )
        entries.append(entry)
        prev = entry["chain_hash"]
    return entries


def verify_chain(archive: Path) -> dict:
    """Walk the ledger and cross-check it against the on-disk archive.

    Returns a dict:
      ok          bool   — chain links intact AND no orphan deposits
      length      int    — number of links
      head        str    — chain tip
      broken_at   int|None — seq of the first link that fails recomputation
      orphans     list   — on-disk session_ids whose current sha256 is not
                           recorded in the ledger (silent insert/substitute)
      missing     list   — session_ids recorded in the ledger with no
                           corresponding on-disk session (informational —
                           git history would show a deletion; not a hard fail
                           on its own since a bundle is a deliberate subset)

    Read-only. Never writes the archive.
    """
    entries = read_entries(archive)
    genesis = genesis_hash(archive)

    broken_at: int | None = None
    prev = genesis
    # session_id -> set of all sha256 ever recorded for it (a session can
    # appear multiple times as it grows; any recorded sha counts as covered).
    ledger_shas: dict[str, set] = {}
    for i, entry in enumerate(entries):
        ledger_shas.setdefault(entry.get("session_id", ""), set()).add(
            entry.get("sha256", "")
        )
        if broken_at is None:
            recomputed = compute_chain_hash(
                prev,
                seq=i,
                source=entry.get("source", ""),
                session_id=entry.get("session_id", ""),
                sha256=entry.get("sha256", ""),
                deposited_at=entry.get("deposited_at", ""),
            )
            if (
                entry.get("seq") != i
                or entry.get("prev") != prev
                or entry.get("chain_hash") != recomputed
            ):
                broken_at = i
        prev = entry.get("chain_hash", "")

    chain_head = entries[-1]["chain_hash"] if entries else genesis

    # Cross-check against on-disk deposits. Imported here to avoid any import
    # cycle (archive.py has no dependency on this module).
    from holotype.archive import iter_all_sessions

    orphans: list[str] = []
    on_disk_ids: set = set()
    for _sess_dir, manifest in iter_all_sessions(archive):
        sid = manifest.get("session_id", "")
        sha = manifest.get("sha256", "")
        on_disk_ids.add(sid)
        if sha not in ledger_shas.get(sid, set()):
            orphans.append(sid)

    missing = sorted(set(ledger_shas) - on_disk_ids)

    ok = broken_at is None and not orphans
    return {
        "ok": ok,
        "length": len(entries),
        "head": chain_head,
        "broken_at": broken_at,
        "orphans": sorted(orphans),
        "missing": missing,
    }
