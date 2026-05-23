#!/usr/bin/env python3
"""Browse the archive in your default browser.

Starts a tiny HTTP server on a random localhost port and opens the index in
your default browser. The index lists every archived session; clicking one
renders its transcript HTML on the fly. Nothing is written to disk — the
canonical archive is the source, HTML is generated per request.

Press Ctrl-C to stop the server.

Stdlib only. Localhost-only (binds to 127.0.0.1). No authentication; the
local user owns the machine.

Usage:
    python scripts/browse.py [--archive PATH] [--port N] [--no-open]
"""

from __future__ import annotations

import argparse
import html as _html
import http.server
import io
import json
import os
import platform
import socketserver
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.compression import read_transcript_bytes
from holotype.viewer import render_index, render_session


def find_archive(explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    env = os.environ.get("HOLOTYPE_ARCHIVE")
    if env:
        return Path(env).expanduser().resolve()
    pointer = Path.home() / ".config" / "holotype" / "archive-path"
    if pointer.exists():
        return Path(pointer.read_text().strip()).expanduser().resolve()
    return (Path.home() / "Documents" / "holotype-archive").resolve()


def open_in_browser(url: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", url], check=False)
        elif system == "Linux":
            subprocess.run(["xdg-open", url], check=False)
        elif system == "Windows":
            os.startfile(url)  # type: ignore[attr-defined]
    except OSError:
        pass


def collect_sessions(archive: Path) -> dict[str, tuple[Path, dict]]:
    """session_id -> (session_dir, manifest_dict). Last-write wins on dup ids."""
    out: dict[str, tuple[Path, dict]] = {}
    sessions_root = archive / "sessions"
    if not sessions_root.is_dir():
        return out
    for manifest_path in sessions_root.rglob("manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        sid = manifest.get("session_id") or manifest_path.parent.name
        out[sid] = (manifest_path.parent, manifest)
    return out


def render_index_html(archive: Path, sessions: dict[str, tuple[Path, dict]]) -> bytes:
    """Build an in-memory ``index.html`` whose session cards link to /session/<sid>."""
    bundle_manifest = {
        "produced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive_commit": "",
        "sessions": [
            {
                "session_id": sid,
                "models": m.get("models") or [],
                "message_count": m.get("message_count"),
                "first_timestamp": m.get("first_timestamp"),
                "last_timestamp": m.get("last_timestamp"),
                "project_git_state": m.get("project_git_state"),
            }
            for sid, (_, m) in sorted(
                sessions.items(),
                key=lambda x: x[1][1].get("last_timestamp") or "",
                reverse=True,
            )
        ],
    }
    buf = io.BytesIO()
    # render_index writes to a Path; do it via a NamedTemporaryFile.
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        render_index(archive, bundle_manifest, tmp_path)
        text = tmp_path.read_text(encoding="utf-8")
    finally:
        tmp_path.unlink(missing_ok=True)
    # Rewrite href="<sid>/view.html" → href="/session/<sid>"
    text = text.replace('href="', 'href="')  # noop, keeps mypy quiet
    # The viewer emits href=\"<sid>/view.html\"; rewrite to server route.
    for sid in sessions:
        text = text.replace(f'href="{sid}/view.html"', f'href="/session/{quote(sid)}"')
    return text.encode("utf-8")


def render_session_html(sess_dir: Path, manifest: dict) -> bytes | None:
    """Decompress + render one session to HTML bytes (no disk cache)."""
    transcript_bytes = read_transcript_bytes(sess_dir)
    if transcript_bytes is None:
        return None
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".jsonl", delete=False) as tmp:
        tmp.write(transcript_bytes)
        in_path = Path(tmp.name)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        render_session(manifest, in_path, out_path, include_raw=False)
        return out_path.read_bytes()
    finally:
        in_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


class BrowseHandler(http.server.BaseHTTPRequestHandler):
    # set by main() before server.serve_forever()
    archive: Path = Path("/")
    sessions: dict[str, tuple[Path, dict]] = {}

    def log_message(self, format: str, *args) -> None:  # quiet by default
        return

    def _ok(self, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, msg: str = "Not found") -> None:
        body = f"<!doctype html><body><h1>404</h1><p>{_html.escape(msg)}</p></body>".encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            # Reload session list on every index hit so newly-ingested sessions
            # show up without restarting the server.
            BrowseHandler.sessions = collect_sessions(BrowseHandler.archive)
            self._ok(render_index_html(BrowseHandler.archive, BrowseHandler.sessions))
            return
        if path.startswith("/session/"):
            sid = unquote(path[len("/session/"):])
            entry = BrowseHandler.sessions.get(sid)
            if entry is None:
                # Refresh in case the session was added since last index hit.
                BrowseHandler.sessions = collect_sessions(BrowseHandler.archive)
                entry = BrowseHandler.sessions.get(sid)
            if entry is None:
                self._not_found(f"session {sid} not in archive")
                return
            sess_dir, manifest = entry
            body = render_session_html(sess_dir, manifest)
            if body is None:
                self._not_found(f"session {sid}: transcript unreadable")
                return
            self._ok(body)
            return
        self._not_found(self.path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive", type=Path, default=None)
    ap.add_argument("--port", type=int, default=0,
                    help="Port to bind (default: OS-assigned random high port).")
    ap.add_argument("--no-open", action="store_true",
                    help="Don't auto-open the browser; just print the URL.")
    args = ap.parse_args(argv)

    archive = find_archive(args.archive)
    if not (archive / ".holotype" / "config.json").exists():
        sys.stderr.write(f"holotype: no archive at {archive}\n")
        sys.stderr.write("  set up first with /holotype (the skill) or python scripts/init.py\n")
        return 2

    sessions = collect_sessions(archive)
    if not sessions:
        sys.stderr.write(f"holotype: no sessions in {archive}/sessions yet\n")
        sys.stderr.write("  run python scripts/ingest.py to deposit your existing chats first\n")
        return 1

    BrowseHandler.archive = archive
    BrowseHandler.sessions = sessions

    # Bind to 127.0.0.1 only — the archive contains every conversation Claude
    # Code wrote on this machine. Don't expose to the LAN.
    server = socketserver.ThreadingTCPServer(("127.0.0.1", args.port), BrowseHandler)
    server.daemon_threads = True
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"

    print(f"holotype browse: {len(sessions)} sessions in {archive}")
    print(f"  url: {url}")
    print("  Ctrl-C to stop")

    if not args.no_open:
        threading.Thread(target=open_in_browser, args=(url,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
