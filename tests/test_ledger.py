#!/usr/bin/env python3
"""Unit tests for holotype.ledger — the append-only hash chain.

stdlib unittest only (holotype is zero-dependency). Run with:

    python -m unittest tests.test_ledger      # from repo root
    python tests/test_ledger.py               # direct

Covers the link function's determinism, genesis derivation, and that
``verify_chain`` detects every tamper shape: a flipped sha256, an inserted
link, a deleted link, a broken prev pointer, and an orphan on-disk deposit.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype import ledger  # noqa: E402


def _make_archive(tmp: Path, created_at: str = "2026-05-28T00:00:00+00:00") -> Path:
    """A minimal archive: .holotype/config.json + an empty sessions/ tree."""
    archive = tmp / "archive"
    (archive / ".holotype").mkdir(parents=True)
    (archive / "sessions").mkdir()
    (archive / ".holotype" / "config.json").write_text(
        json.dumps({"created_at": created_at}) + "\n"
    )
    return archive


def _deposit_session(
    archive: Path, session_id: str, sha256: str, *, source: str = "claude-code"
) -> None:
    """Write a minimal on-disk session (manifest + transcript) for the
    cross-check half of verify_chain."""
    sess = archive / "sessions" / source / session_id
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "transcript.jsonl").write_text("{}\n")
    (sess / "manifest.json").write_text(
        json.dumps({"session_id": session_id, "sha256": sha256, "source": source})
        + "\n"
    )


class LinkFunctionTests(unittest.TestCase):
    def test_compute_chain_hash_is_deterministic(self):
        kw = dict(
            seq=0,
            source="claude-code",
            session_id="abc",
            sha256="d" * 64,
            deposited_at="2026-05-28T00:00:00+00:00",
        )
        a = ledger.compute_chain_hash("p", **kw)
        b = ledger.compute_chain_hash("p", **kw)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_any_field_change_changes_hash(self):
        base = dict(
            seq=0,
            source="claude-code",
            session_id="abc",
            sha256="d" * 64,
            deposited_at="2026-05-28T00:00:00+00:00",
        )
        h0 = ledger.compute_chain_hash("p", **base)
        # prev differs
        self.assertNotEqual(h0, ledger.compute_chain_hash("q", **base))
        # each kwarg differs
        for field, newval in (
            ("seq", 1),
            ("source", "codex"),
            ("session_id", "xyz"),
            ("sha256", "e" * 64),
            ("deposited_at", "2026-05-29T00:00:00+00:00"),
        ):
            kw = dict(base)
            kw[field] = newval
            self.assertNotEqual(
                h0, ledger.compute_chain_hash("p", **kw), f"{field} didn't affect hash"
            )

    def test_genesis_ties_to_created_at(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            a1 = _make_archive(tmp / "a", created_at="2026-01-01T00:00:00+00:00")
            a2 = _make_archive(tmp / "b", created_at="2026-02-02T00:00:00+00:00")
            self.assertNotEqual(ledger.genesis_hash(a1), ledger.genesis_hash(a2))
            self.assertEqual(len(ledger.genesis_hash(a1)), 64)


class WriterAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.archive = _make_archive(self.tmp)

    def tearDown(self):
        self._td.cleanup()

    def _append_three(self):
        """Append three linked deposits + their on-disk sessions."""
        rows = [
            ("sess-a", "a" * 64),
            ("sess-b", "b" * 64),
            ("sess-c", "c" * 64),
        ]
        with ledger.LedgerWriter(self.archive) as w:
            for sid, sha in rows:
                w.append(
                    source="claude-code",
                    session_id=sid,
                    sha256=sha,
                    deposited_at="2026-05-28T00:00:00+00:00",
                )
        for sid, sha in rows:
            _deposit_session(self.archive, sid, sha)
        return rows

    def test_clean_chain_verifies(self):
        self._append_three()
        result = ledger.verify_chain(self.archive)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["length"], 3)
        self.assertIsNone(result["broken_at"])
        self.assertEqual(result["orphans"], [])
        self.assertEqual(result["missing"], [])

    def test_head_advances_and_first_prev_is_genesis(self):
        self._append_three()
        entries = ledger.read_entries(self.archive)
        self.assertEqual(entries[0]["prev"], ledger.genesis_hash(self.archive))
        self.assertEqual(entries[1]["prev"], entries[0]["chain_hash"])
        self.assertEqual(entries[2]["prev"], entries[1]["chain_hash"])
        self.assertEqual(ledger.head(self.archive), entries[-1]["chain_hash"])

    def test_writer_resumes_from_existing_head(self):
        self._append_three()
        head_before = ledger.head(self.archive)
        with ledger.LedgerWriter(self.archive) as w:
            entry = w.append(
                source="codex",
                session_id="sess-d",
                sha256="d" * 64,
                deposited_at="2026-05-28T01:00:00+00:00",
            )
        self.assertEqual(entry["seq"], 3)
        self.assertEqual(entry["prev"], head_before)
        self.assertEqual(ledger.read_entries(self.archive)[-1]["chain_hash"],
                         entry["chain_hash"])

    def test_detects_flipped_sha(self):
        self._append_three()
        entries = ledger.read_entries(self.archive)
        entries[1]["sha256"] = "f" * 64  # tamper without recomputing chain_hash
        ledger.ledger_path(self.archive).write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries)
        )
        result = ledger.verify_chain(self.archive)
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"], 1)

    def test_detects_broken_prev(self):
        self._append_three()
        entries = ledger.read_entries(self.archive)
        entries[2]["prev"] = "0" * 64
        ledger.ledger_path(self.archive).write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries)
        )
        result = ledger.verify_chain(self.archive)
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"], 2)

    def test_detects_deleted_link(self):
        self._append_three()
        entries = ledger.read_entries(self.archive)
        del entries[1]  # remove the middle link; seq/prev now inconsistent
        ledger.ledger_path(self.archive).write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries)
        )
        result = ledger.verify_chain(self.archive)
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"], 1)

    def test_detects_inserted_link(self):
        self._append_three()
        entries = ledger.read_entries(self.archive)
        forged = ledger.make_entry(
            entries[0]["chain_hash"],
            seq=1,
            event="deposit",
            source="claude-code",
            session_id="sess-evil",
            sha256="9" * 64,
            deposited_at="2026-05-28T00:30:00+00:00",
        )
        entries.insert(1, forged)  # valid link by itself, but shifts seq downstream
        ledger.ledger_path(self.archive).write_text(
            "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries)
        )
        result = ledger.verify_chain(self.archive)
        self.assertFalse(result["ok"])
        # entry now at index 2 (old sess-b) has prev pointing at the real
        # sess-a hash, not the forged link -> first failure there.
        self.assertEqual(result["broken_at"], 2)

    def test_detects_orphan_on_disk_session(self):
        self._append_three()
        # A deposit appears on disk that was never recorded in the ledger.
        _deposit_session(self.archive, "sess-smuggled", "7" * 64)
        result = ledger.verify_chain(self.archive)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["broken_at"])  # links themselves are fine
        self.assertIn("sess-smuggled", result["orphans"])

    def test_substituted_transcript_is_orphan(self):
        self._append_three()
        # Co-edit transcript+manifest to a new sha (passes per-file verify),
        # but the new sha was never chained -> caught as an orphan.
        _deposit_session(self.archive, "sess-b", "0" * 63 + "1")
        result = ledger.verify_chain(self.archive)
        self.assertFalse(result["ok"])
        self.assertIn("sess-b", result["orphans"])

    def test_empty_ledger_head_is_genesis(self):
        self.assertEqual(ledger.head(self.archive), ledger.genesis_hash(self.archive))
        result = ledger.verify_chain(self.archive)
        self.assertTrue(result["ok"])
        self.assertEqual(result["length"], 0)


if __name__ == "__main__":
    unittest.main()
