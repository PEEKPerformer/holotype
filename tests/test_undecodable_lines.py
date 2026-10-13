#!/usr/bin/env python3
"""Regression tests: a transcript line that isn't decodable text must not
abort a deposit.

``json.loads(bytes)`` sniffs the encoding from the first bytes, so a line
that starts with two NUL bytes is decoded as UTF-32-BE and raises
``UnicodeDecodeError`` instead of ``JSONDecodeError``. Before the fix that
escaped every ``parse_line`` and failed the whole session on every tick
(seen on a live 72 MB Claude Code transcript, 2026-10-12/13).

stdlib unittest only. Run with:

    python -m unittest tests.test_undecodable_lines
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype import index  # noqa: E402
from holotype.env import models_in_transcript  # noqa: E402
from holotype.manifest import build_manifest  # noqa: E402
from holotype.sources import ALL_SOURCES  # noqa: E402
from holotype.sources.claude_code import ClaudeCodeSource  # noqa: E402

GOOD = (
    json.dumps({
        "type": "user", "uuid": "u1", "timestamp": "2026-10-12T00:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }) + "\n"
).encode()
GOOD_ASSISTANT = (
    json.dumps({
        "type": "assistant", "uuid": "u2", "parentUuid": "u1",
        "timestamp": "2026-10-12T00:00:01Z",
        "message": {"role": "assistant", "model": "claude-test", "content": "hi"},
    }) + "\n"
).encode()
# Leading NULs -> json picks utf-32-be; odd length -> "truncated data".
NUL_LED = b"\x00\x00" + b'{"type": "user"}' + b"x" + b"\n"
# Not valid UTF-8 at all.
BAD_UTF8 = b'{"type": "user", "x": "\xff\xfe"}\n'

TRANSCRIPT = GOOD + NUL_LED + BAD_UTF8 + GOOD_ASSISTANT


class ParseLineTolerance(unittest.TestCase):
    def test_every_source_returns_none_for_undecodable_lines(self):
        for source_cls in ALL_SOURCES:
            for line in (NUL_LED, BAD_UTF8):
                with self.subTest(source=source_cls.name, line=line[:8]):
                    self.assertIsNone(source_cls.parse_line(line))


class DepositPathTolerance(unittest.TestCase):
    def test_build_manifest_skips_bad_lines_and_hashes_all_bytes(self):
        m = build_manifest(
            TRANSCRIPT,
            ClaudeCodeSource,
            "-tmp-project",
            session_id="s1",
            on_disk_filename="transcript.jsonl",
            holotype_version="test",
            source_path="/tmp/s1.jsonl",
        )
        d = json.loads(m.to_json())
        self.assertEqual(d["message_count"], 2)
        # The archived bytes, bad lines included, are what gets hashed.
        import hashlib
        self.assertEqual(d["sha256"], hashlib.sha256(TRANSCRIPT).hexdigest())

    def test_models_in_transcript_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s1.jsonl"
            p.write_bytes(TRANSCRIPT)
            self.assertEqual(models_in_transcript(p), ["claude-test"])

    def test_reindex_session_skips_bad_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s1.jsonl"
            p.write_bytes(TRANSCRIPT)
            with index.open_index(Path(tmp) / "index.sqlite") as conn:
                index.upsert_session(
                    conn, session_id="s1", project_dir="/tmp/project",
                    first_ts=None, last_ts=None, message_count=2, sha256="",
                    deposited_at="2026-10-13T00:00:00+00:00", git_commit=None,
                )
                index.reindex_session(conn, "s1", p, ClaudeCodeSource)
                n = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = 's1'"
                ).fetchone()[0]
            self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
