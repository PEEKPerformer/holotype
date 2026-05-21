#!/usr/bin/env python3
"""End-to-end smoke test for the holotype skill.

Simulates the cold-start path:
  1. Materialize a synthetic source directory from the fixtures
  2. Run init.py against a tmpdir archive (no remote, no prompts)
  3. Run ingest.py against the synthetic source
  4. Assert: archive layout is correct, manifests recover the right SHA-256,
     the SQLite index has the expected sessions and is FTS-queryable
  5. Run ingest.py a second time and assert it is a no-op (idempotency)
  6. Mutate a fixture and assert the next ingest is an 'updated' deposit

This is the "0-shot" validation: if this passes from a clean machine,
the skill is ready for Claude to invoke without iteration.

Usage:
    python scripts/selftest.py [--keep] [--verbose]

`--keep` leaves the tmpdir on disk for inspection. Otherwise it is
cleaned up on success and left on failure (so you can poke at it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"


class TestFailure(AssertionError):
    pass


def step(label: str) -> None:
    print(f"  • {label}")


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise TestFailure(message)


def materialize_source(tmp: Path) -> Path:
    """Build a fake `~/.claude/projects/`-shaped source dir from the fixtures.

    The mtime of each fixture is back-dated so it is older than the
    live-file grace window in ingest.py — otherwise ingest would treat
    every fixture as "actively being written" and skip it.
    """
    import os, time
    src = tmp / "source"
    proj_a = src / "-Users-test-fixture"
    proj_b = src / "-Users-test-fixture-mm"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)

    basic_uuid = "00000000-0000-4000-8000-000000000001"
    mm_uuid = "00000000-0000-4000-8000-000000000002"

    out_a = proj_a / f"{basic_uuid}.jsonl"
    out_b = proj_b / f"{mm_uuid}.jsonl"
    shutil.copy(FIXTURES / "synthetic-session-basic.jsonl", out_a)
    shutil.copy(FIXTURES / "synthetic-session-multi-model.jsonl", out_b)

    older = time.time() - 60
    for p in (out_a, out_b):
        os.utime(p, (older, older))

    return src


def run_script(script: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="Keep tmpdir after success.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="holotype-selftest-"))
    print(f"holotype selftest in {tmp}")
    success = False
    try:
        archive = tmp / "archive"

        step("init.py creates a fresh local-only archive")
        result = run_script(
            REPO_ROOT / "scripts" / "init.py",
            "--path", str(archive),
            "--remote-url", "",
            "--remote-kind", "none",
        )
        if args.verbose:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
        expect(result.returncode == 0, f"init.py failed: {result.stderr}")
        expect((archive / ".holotype" / "config.json").exists(), "config.json missing")
        expect((archive / "VERIFY.md").exists(), "VERIFY.md missing")
        expect((archive / "README.md").exists(), "archive README.md missing")
        expect((archive / ".git").is_dir(), "archive is not a git repo")

        step("materialize synthetic source")
        source = materialize_source(tmp)

        step("first ingest deposits both fixture sessions")
        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(archive),
            "--source", str(source),
        )
        if args.verbose:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
        expect(result.returncode == 0, f"ingest.py returncode={result.returncode}: {result.stderr}")
        expect("new=2" in result.stdout, f"expected new=2 in output:\n{result.stdout}")

        sessions_dir = archive / "sessions"
        deposits = sorted(p for p in sessions_dir.rglob("transcript.jsonl"))
        expect(len(deposits) == 2, f"expected 2 deposits, got {len(deposits)}")

        step("manifest SHA-256 matches the deposited transcript")
        for tr in deposits:
            manifest = json.loads((tr.parent / "manifest.json").read_text())
            recomputed = sha256_file(tr)
            expect(
                manifest["sha256"] == recomputed,
                f"sha256 mismatch for {tr}: manifest={manifest['sha256']}, recomputed={recomputed}",
            )
            expect(manifest["manifest_version"] == 1, "manifest_version != 1")
            expect(manifest["message_count"] > 0, "message_count is zero")
            expect(len(manifest["models"]) > 0, "no models recorded")

        step("multi-model fixture records both models")
        mm_manifest_path = next(
            p for p in sessions_dir.rglob("manifest.json") if "fixture-mm" in str(p)
        )
        mm_manifest = json.loads(mm_manifest_path.read_text())
        expect(
            "claude-sonnet-4-6" in mm_manifest["models"] and "claude-opus-4-7" in mm_manifest["models"],
            f"multi-model session missed a model: {mm_manifest['models']}",
        )
        expect(mm_manifest["has_compaction"] is True, "compaction marker not detected")
        expect(mm_manifest["has_thinking"] is True, "thinking block not detected")

        step("basic fixture flags tool use and thinking")
        basic_manifest_path = next(
            p for p in sessions_dir.rglob("manifest.json") if "fixture-mm" not in str(p)
        )
        basic_manifest = json.loads(basic_manifest_path.read_text())
        expect(basic_manifest["has_tool_use"] is True, "tool_use not detected in basic fixture")
        expect(basic_manifest["has_thinking"] is True, "thinking not detected in basic fixture")

        step("git history has two deposit commits")
        log = subprocess.run(
            ["git", "-C", str(archive), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        )
        deposit_lines = [ln for ln in log.stdout.strip().split("\n") if "deposit:" in ln]
        expect(len(deposit_lines) == 2, f"expected 2 deposit commits, got {len(deposit_lines)}\n{log.stdout}")

        step("SQLite index has both sessions and is FTS-queryable")
        index_path = archive / ".holotype" / "index.sqlite"
        expect(index_path.exists(), "index.sqlite not created")
        conn = sqlite3.connect(index_path)
        try:
            n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            expect(n_sessions == 2, f"expected 2 sessions in index, got {n_sessions}")

            n_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            expect(n_msgs > 0, "no messages in index")

            n_fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            expect(n_fts > 0, "no FTS rows")

            hits = conn.execute(
                "SELECT session_id FROM messages_fts WHERE messages_fts MATCH 'entanglement' LIMIT 5"
            ).fetchall()
            expect(len(hits) > 0, "FTS query for 'entanglement' returned no hits")

            hits_tool = conn.execute(
                "SELECT session_id FROM messages_fts WHERE messages_fts MATCH '\"example.txt\"' LIMIT 5"
            ).fetchall()
            expect(len(hits_tool) > 0, "FTS query for 'example.txt' (tool input) returned no hits")
        finally:
            conn.close()

        step("second ingest is a no-op (idempotent)")
        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(archive),
            "--source", str(source),
        )
        if args.verbose:
            print(result.stdout)
        expect("new=0" in result.stdout and "unchanged=2" in result.stdout,
               f"second ingest was not a no-op:\n{result.stdout}")
        expect(result.returncode == 1, "no-op ingest should exit 1 (no changes)")

        step("mutating a fixture causes an 'updated' deposit on next ingest")
        import os, time
        basic_in_source = next(source.rglob("*.jsonl"))
        with basic_in_source.open("ab") as f:
            f.write(b'{"type":"user","message":{"role":"user","content":[{"type":"text","text":"appended"}]},"uuid":"77777777-7777-7777-7777-777777777777","timestamp":"2026-01-15T10:01:00.000Z"}\n')
        older = time.time() - 60
        os.utime(basic_in_source, (older, older))

        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(archive),
            "--source", str(source),
        )
        if args.verbose:
            print(result.stdout)
        expect("updated=1" in result.stdout, f"expected updated=1:\n{result.stdout}")

        step("verify.py reports all sessions clean")
        result = run_script(REPO_ROOT / "scripts" / "verify.py", "--archive", str(archive))
        if args.verbose:
            print(result.stdout)
        expect(result.returncode == 0, f"verify.py returncode={result.returncode}:\n{result.stderr}")
        expect("sessions verified clean" in result.stdout, f"missing summary line:\n{result.stdout}")

        step("verify.py catches a tampered transcript")
        deposits_again = sorted(p for p in (archive / "sessions").rglob("transcript.jsonl"))
        tampered = deposits_again[0]
        original_bytes = tampered.read_bytes()
        with tampered.open("ab") as f:
            f.write(b'{"tampered":true}\n')
        result = run_script(REPO_ROOT / "scripts" / "verify.py", "--archive", str(archive))
        expect(result.returncode == 1, "verify.py did not exit 1 on tamper")
        expect("TAMPER" in result.stdout, f"verify.py missed tamper:\n{result.stdout}")
        tampered.write_bytes(original_bytes)
        result = run_script(REPO_ROOT / "scripts" / "verify.py", "--archive", str(archive))
        expect(result.returncode == 0, "verify.py still failing after restore")

        step("search.py returns matches for a known term")
        result = run_script(
            REPO_ROOT / "scripts" / "search.py", "entanglement",
            "--archive", str(archive),
        )
        if args.verbose:
            print(result.stdout)
        expect(result.returncode == 0, f"search.py returncode={result.returncode}: {result.stderr}")
        expect("match" in result.stdout, f"search.py output unexpected:\n{result.stdout}")

        step("search.py with --json returns parseable output")
        result = run_script(
            REPO_ROOT / "scripts" / "search.py", "entanglement", "--json",
            "--archive", str(archive),
        )
        parsed = json.loads(result.stdout)
        expect(isinstance(parsed, list) and len(parsed) > 0, "search.py --json empty or malformed")

        step("cite.py produces a bundle with all four files")
        first_session = next((archive / "sessions").rglob("manifest.json")).parent.name
        bundle_dir = tmp / "bundle"
        result = run_script(
            REPO_ROOT / "scripts" / "cite.py", first_session,
            "--archive", str(archive),
            "--out", str(bundle_dir),
        )
        if args.verbose:
            print(result.stdout)
        expect(result.returncode == 0, f"cite.py returncode={result.returncode}: {result.stderr}")
        for fname in ("transcript.jsonl", "manifest.json", "cite.txt", "render.md"):
            expect((bundle_dir / fname).exists(), f"bundle missing {fname}")
        expect("SHA-256" in (bundle_dir / "cite.txt").read_text(),
               "cite.txt missing SHA-256 line")

        step("cite.py --citation-only emits only the citation")
        result = run_script(
            REPO_ROOT / "scripts" / "cite.py", first_session,
            "--archive", str(archive),
            "--citation-only",
        )
        expect("Holotype session" in result.stdout and "SHA-256" in result.stdout,
               f"--citation-only output unexpected:\n{result.stdout}")

        step("context.py prints the absolute transcript path")
        result = run_script(
            REPO_ROOT / "scripts" / "context.py", first_session,
            "--archive", str(archive),
        )
        expect(result.returncode == 0, f"context.py returncode={result.returncode}")
        printed_path = Path(result.stdout.strip())
        expect(printed_path.exists() and printed_path.name == "transcript.jsonl",
               f"context.py path invalid: {printed_path}")

        step("reindex.py rebuilds the SQLite from scratch")
        index_path = archive / ".holotype" / "index.sqlite"
        expect(index_path.exists(), "index missing before reindex")
        index_path.unlink()
        expect(not index_path.exists(), "index didn't actually delete")
        result = run_script(REPO_ROOT / "scripts" / "reindex.py", "--archive", str(archive))
        if args.verbose:
            print(result.stdout)
        expect(result.returncode == 0, f"reindex.py returncode={result.returncode}: {result.stderr}")
        expect(index_path.exists(), "reindex.py didn't recreate the index")
        # Search should work again
        result = run_script(
            REPO_ROOT / "scripts" / "search.py", "entanglement",
            "--archive", str(archive), "--json",
        )
        parsed = json.loads(result.stdout)
        expect(len(parsed) > 0, "search returned 0 hits after reindex")

        print("\nALL CHECKS PASSED")
        success = True
        return 0

    except TestFailure as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        print(f"  tmpdir preserved: {tmp}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        print(f"  tmpdir preserved: {tmp}", file=sys.stderr)
        return 2
    finally:
        if success and not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
