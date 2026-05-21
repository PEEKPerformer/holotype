"""SQLite + FTS5 index for fast search over the archive.

The index is **derived data**. The git-tracked JSONLs are the source of
truth; this database can be wiped and rebuilt from them at any time with
``scripts/reindex.py``. Nothing in the archive's provenance chain
depends on the index existing or being current.

Schema version is recorded both in the database (`PRAGMA user_version`)
and in ``.holotype/index-version``. On version mismatch the index is
rebuilt — never migrated. We can afford that because the source of truth
is intact.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

INDEX_SCHEMA_VERSION = 2


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return row[0] if row else 0


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id        TEXT PRIMARY KEY,
            parent_session_id TEXT,
            project_dir       TEXT,
            first_ts          TEXT,
            last_ts           TEXT,
            message_count     INTEGER,
            sha256            TEXT,
            deposited_at      TEXT,
            git_commit        TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_parent
            ON sessions(parent_session_id);

        CREATE TABLE IF NOT EXISTS messages (
            session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            sequence    INTEGER NOT NULL,
            uuid        TEXT,
            parent_uuid TEXT,
            role        TEXT,
            timestamp   TEXT,
            model       TEXT,
            has_tool_use    INTEGER,
            has_thinking    INTEGER,
            raw_json    TEXT,
            PRIMARY KEY (session_id, sequence)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_uuid ON messages(uuid);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            role UNINDEXED,
            session_id UNINDEXED,
            sequence UNINDEXED,
            tokenize = 'porter unicode61'
        );
        """
    )
    conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
    conn.commit()


@contextmanager
def open_index(db_path: Path):
    """Open the index, validating schema version. Rebuilds if mismatched."""
    if db_path.exists():
        conn = _connect(db_path)
        try:
            v = _current_schema_version(conn)
        except sqlite3.DatabaseError:
            v = -1
        if v != INDEX_SCHEMA_VERSION:
            conn.close()
            db_path.unlink(missing_ok=True)
            conn = _connect(db_path)
            _initialize_schema(conn)
    else:
        conn = _connect(db_path)
        _initialize_schema(conn)

    try:
        yield conn
    finally:
        conn.close()


def _extract_text_for_fts(msg_obj: dict) -> str:
    """Flatten a message's content array into a single searchable string.

    Includes user text, assistant text, thinking blocks, tool inputs, and
    tool outputs. Forensic completeness: we want a single FTS hit to
    surface ANY mention of a term, regardless of what kind of message
    contained it.
    """
    parts: list[str] = []

    msg = msg_obj.get("message") if isinstance(msg_obj.get("message"), dict) else None
    if not msg:
        return ""

    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text":
            parts.append(item.get("text") or "")
        elif t == "thinking":
            parts.append(item.get("thinking") or "")
        elif t == "tool_use":
            parts.append(f"[tool_use:{item.get('name','?')}]")
            inp = item.get("input")
            if isinstance(inp, dict):
                parts.append(json.dumps(inp, ensure_ascii=False))
        elif t == "tool_result":
            inner = item.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for sub in inner:
                    if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                        parts.append(sub["text"])
    return "\n".join(parts)


def upsert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    project_dir: str,
    first_ts: str | None,
    last_ts: str | None,
    message_count: int,
    sha256: str,
    deposited_at: str,
    git_commit: str | None,
    parent_session_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions(session_id, parent_session_id, project_dir,
                             first_ts, last_ts, message_count, sha256,
                             deposited_at, git_commit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            parent_session_id = excluded.parent_session_id,
            project_dir       = excluded.project_dir,
            first_ts          = excluded.first_ts,
            last_ts           = excluded.last_ts,
            message_count     = excluded.message_count,
            sha256            = excluded.sha256,
            deposited_at      = excluded.deposited_at,
            git_commit        = excluded.git_commit
        """,
        (session_id, parent_session_id, project_dir, first_ts, last_ts,
         message_count, sha256, deposited_at, git_commit),
    )


def reindex_session(conn: sqlite3.Connection, session_id: str, jsonl_path: Path) -> int:
    """Drop and rewrite all message rows + FTS entries for a single session.

    Returns the number of messages indexed.
    """
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))

    n = 0
    with jsonl_path.open("rb") as f:
        for sequence, line in enumerate(f):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            role = (msg.get("role") if isinstance(msg, dict) else None) or obj.get("type")
            model = msg.get("model") if isinstance(msg, dict) else None
            timestamp = obj.get("timestamp")

            has_tool_use = 0
            has_thinking = 0
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                for c in msg["content"]:
                    if isinstance(c, dict):
                        if c.get("type") == "tool_use":
                            has_tool_use = 1
                        elif c.get("type") == "thinking":
                            has_thinking = 1

            conn.execute(
                """INSERT INTO messages(session_id, sequence, uuid, parent_uuid,
                                        role, timestamp, model, has_tool_use,
                                        has_thinking, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    sequence,
                    obj.get("uuid"),
                    obj.get("parentUuid"),
                    role,
                    timestamp,
                    model,
                    has_tool_use,
                    has_thinking,
                    line.decode("utf-8", errors="replace").rstrip("\n"),
                ),
            )

            fts_content = _extract_text_for_fts(obj)
            if fts_content:
                conn.execute(
                    """INSERT INTO messages_fts(content, role, session_id, sequence)
                       VALUES (?, ?, ?, ?)""",
                    (fts_content, role, session_id, sequence),
                )
            n += 1
    return n


def search(conn: sqlite3.Connection, query: str, *, limit: int = 25) -> list[dict]:
    """Run an FTS5 query, return matching messages with snippets."""
    rows = conn.execute(
        """
        SELECT
            f.session_id,
            f.sequence,
            f.role,
            snippet(messages_fts, 0, '«', '»', '…', 20) AS snippet,
            s.first_ts AS first_ts,
            s.project_dir AS project_dir
        FROM messages_fts f
        JOIN sessions s ON s.session_id = f.session_id
        WHERE messages_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]
