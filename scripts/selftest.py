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
    sub_id = "agent-aff5969"

    out_a = proj_a / f"{basic_uuid}.jsonl"
    out_b = proj_b / f"{mm_uuid}.jsonl"
    shutil.copy(FIXTURES / "synthetic-session-basic.jsonl", out_a)
    shutil.copy(FIXTURES / "synthetic-session-multi-model.jsonl", out_b)

    sub_dir = proj_a / basic_uuid / "subagents"
    sub_dir.mkdir(parents=True)
    out_sub = sub_dir / f"{sub_id}.jsonl"
    shutil.copy(FIXTURES / "synthetic-subagent.jsonl", out_sub)

    older = time.time() - 60
    for p in (out_a, out_b, out_sub):
        os.utime(p, (older, older))

    return src


def materialize_codex_source(tmp: Path) -> Path:
    """Build a fake `~/.codex/sessions/`-shaped source dir."""
    import os, time
    src = tmp / "codex-source"
    date_dir = src / "2026" / "01" / "20"
    date_dir.mkdir(parents=True)
    out = date_dir / "rollout-2026-01-20T15-00-00-deadbeef-cafe-4567-89ab-cdef01234567.jsonl"
    shutil.copy(FIXTURES / "synthetic-codex-rollout.jsonl", out)
    older = time.time() - 60
    os.utime(out, (older, older))
    return src


def materialize_antigravity_source(tmp: Path) -> tuple[Path, str]:
    """Build a fake `~/.gemini/antigravity-cli/brain/`-shaped source dir.

    Returns (source_root, session_uuid) so callers can assert against the
    UUID without re-deriving it.
    """
    import os, time
    src = tmp / "antigravity-source"
    uuid = "cccc1111-2222-3333-4444-555566667777"
    log_dir = src / uuid / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)
    out = log_dir / "transcript_full.jsonl"
    shutil.copy(FIXTURES / "synthetic-antigravity-conversation.jsonl", out)
    older = time.time() - 60
    os.utime(out, (older, older))
    return src, uuid


def run_script(script: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    # Prevent the selftest from clobbering the user's real ~/.config/holotype/archive-path.
    env["HOLOTYPE_NO_POINTER"] = "1"
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
        # Force plain JSONL so this branch exercises the uncompressed
        # deposit path end-to-end. The dedicated compression section
        # near the bottom of this file covers the zstd path.
        result = run_script(
            REPO_ROOT / "scripts" / "init.py",
            "--path", str(archive),
            "--remote-url", "",
            "--remote-kind", "none",
            "--compression", "none",
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

        step("first ingest deposits two top-level sessions and one subagent")
        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(archive),
            "--source", str(source),
        )
        if args.verbose:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
        expect(result.returncode == 0, f"ingest.py returncode={result.returncode}: {result.stderr}")
        expect("new=3" in result.stdout, f"expected new=3 in output:\n{result.stdout}")

        sessions_dir = archive / "sessions"
        deposits = sorted(p for p in sessions_dir.rglob("transcript.jsonl"))
        expect(len(deposits) == 3, f"expected 3 deposits (2 top-level + 1 subagent), got {len(deposits)}")

        step("subagent is nested under its parent's archive directory")
        sub_paths = sorted(sessions_dir.rglob("subagents/*/transcript.jsonl"))
        expect(len(sub_paths) == 1, f"expected 1 subagent deposit, got {len(sub_paths)}")
        sub_manifest = json.loads(sub_paths[0].with_name("manifest.json").read_text())
        expect(
            sub_manifest.get("parent_session_id") == "00000000-0000-4000-8000-000000000001",
            f"subagent manifest parent_session_id wrong: {sub_manifest.get('parent_session_id')}",
        )

        step("manifest SHA-256 matches the deposited transcript")
        for tr in deposits:
            manifest = json.loads((tr.parent / "manifest.json").read_text())
            recomputed = sha256_file(tr)
            expect(
                manifest["sha256"] == recomputed,
                f"sha256 mismatch for {tr}: manifest={manifest['sha256']}, recomputed={recomputed}",
            )
            expect(manifest["manifest_version"] == 4, "manifest_version != 4")
            expect(manifest.get("source") == "claude-code",
                   f"manifest.source wrong: {manifest.get('source')}")
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

        step("git history has three deposit commits")
        log = subprocess.run(
            ["git", "-C", str(archive), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        )
        deposit_lines = [ln for ln in log.stdout.strip().split("\n") if "deposit:" in ln]
        expect(len(deposit_lines) == 3, f"expected 3 deposit commits, got {len(deposit_lines)}\n{log.stdout}")
        sub_commits = [ln for ln in deposit_lines if "/subagents/" in ln]
        expect(len(sub_commits) == 1, f"expected 1 subagent commit, got {len(sub_commits)}")

        step("SQLite index has all three sessions and is FTS-queryable")
        index_path = archive / ".holotype" / "index.sqlite"
        expect(index_path.exists(), "index.sqlite not created")
        conn = sqlite3.connect(index_path)
        try:
            n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            expect(n_sessions == 3, f"expected 3 sessions in index, got {n_sessions}")

            n_with_parent = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE parent_session_id IS NOT NULL"
            ).fetchone()[0]
            expect(n_with_parent == 1, f"expected 1 row with parent_session_id, got {n_with_parent}")

            quasi = conn.execute(
                "SELECT session_id FROM messages_fts WHERE messages_fts MATCH 'quasicrystal' LIMIT 5"
            ).fetchall()
            expect(len(quasi) > 0, "FTS query for 'quasicrystal' (subagent content) returned no hits")

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
        expect("new=0" in result.stdout and "unchanged=3" in result.stdout,
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

        step("Codex source ingests a rollout into sessions/codex/...")
        codex_src = materialize_codex_source(tmp)
        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(archive),
            "--source", str(codex_src),
            "--source-name", "codex",
        )
        if args.verbose:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
        expect(result.returncode == 0, f"codex ingest returncode={result.returncode}: {result.stderr}")
        expect("new=1" in result.stdout, f"expected new=1 from codex ingest:\n{result.stdout}")

        codex_deposits = list((archive / "sessions" / "codex").rglob("transcript.jsonl"))
        expect(len(codex_deposits) == 1, f"expected 1 codex deposit, got {len(codex_deposits)}")

        codex_manifest = json.loads(codex_deposits[0].with_name("manifest.json").read_text())
        expect(codex_manifest["source"] == "codex", f"codex manifest source wrong: {codex_manifest.get('source')}")
        expect(codex_manifest["session_id"] == "deadbeef-cafe-4567-89ab-cdef01234567",
               f"codex session_id wrong: {codex_manifest.get('session_id')}")
        expect(codex_manifest["has_thinking"] is True, "codex reasoning block missed")
        expect(codex_manifest["has_tool_use"] is True, "codex function_call missed")
        expect(codex_manifest["message_count"] >= 2, "codex message count too low")
        # Manifest v4: codex fixture has a session_meta with git block + a
        # token_count event. Both must round-trip into the manifest.
        gs = codex_manifest.get("project_git_state")
        expect(isinstance(gs, dict) and gs.get("commit") == "abc123def456789",
               f"codex git_state not propagated from session_meta: {gs}")
        expect(gs.get("captured_at") == "session-start",
               f"codex git_state captured_at should be session-start: {gs}")
        expect(codex_manifest.get("total_input_tokens") == 4321,
               f"codex total_input_tokens wrong: {codex_manifest.get('total_input_tokens')}")
        expect(codex_manifest.get("total_output_tokens") == 127,
               f"codex total_output_tokens wrong: {codex_manifest.get('total_output_tokens')}")
        expect(codex_manifest.get("wall_clock_seconds") and codex_manifest["wall_clock_seconds"] > 0,
               f"codex wall_clock_seconds wrong: {codex_manifest.get('wall_clock_seconds')}")
        expect("gpt-5.5" in codex_manifest.get("models", []),
               f"codex models should include 'gpt-5.5' from turn_context: {codex_manifest.get('models')}")
        # And model_provider must NOT pollute the models list.
        expect("openai" not in codex_manifest.get("models", []),
               "codex models contains 'openai' — that's the provider, not a model id")

        step("Codex FTS hits work (reasoning + tool-call content)")
        result = run_script(
            REPO_ROOT / "scripts" / "search.py", "photoelectric",
            "--archive", str(archive), "--json",
        )
        parsed = json.loads(result.stdout)
        expect(len(parsed) > 0, "FTS query for codex content 'photoelectric' returned 0 hits")

        step("cite.py finds the codex session by prefix")
        bundle_dir = tmp / "codex-bundle"
        result = run_script(
            REPO_ROOT / "scripts" / "cite.py", "deadbeef-cafe-4567-89ab-cdef01234567",
            "--archive", str(archive),
            "--out", str(bundle_dir),
        )
        expect(result.returncode == 0, f"cite for codex session failed: {result.stderr}")
        expect((bundle_dir / "transcript.jsonl").exists(), "codex bundle missing transcript")

        step("verify.py still clean with mixed claude-code + codex sessions")
        result = run_script(REPO_ROOT / "scripts" / "verify.py", "--archive", str(archive))
        expect(result.returncode == 0, f"mixed-source verify failed: {result.stderr}")

        step("Antigravity source ingests a brain/<uuid>/.../transcript_full.jsonl")
        ag_src, ag_uuid = materialize_antigravity_source(tmp)
        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(archive),
            "--source", str(ag_src),
            "--source-name", "antigravity",
        )
        if args.verbose:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
        expect(result.returncode == 0, f"antigravity ingest returncode={result.returncode}: {result.stderr}")
        expect("new=1" in result.stdout, f"expected new=1 from antigravity ingest:\n{result.stdout}")

        ag_deposits = list((archive / "sessions" / "antigravity").rglob("transcript.jsonl"))
        expect(len(ag_deposits) == 1, f"expected 1 antigravity deposit, got {len(ag_deposits)}")
        ag_manifest = json.loads(ag_deposits[0].with_name("manifest.json").read_text())
        expect(ag_manifest["source"] == "antigravity",
               f"antigravity manifest source wrong: {ag_manifest.get('source')}")
        expect(ag_manifest["session_id"] == ag_uuid,
               f"antigravity session_id wrong: {ag_manifest.get('session_id')} (expected {ag_uuid})")
        expect(ag_manifest["has_tool_use"] is True, "antigravity tool_calls not detected")
        expect("Gemini 3.5 Flash (High)" in ag_manifest["models"],
               f"antigravity model extraction failed: {ag_manifest.get('models')}")
        expect(ag_manifest["message_count"] >= 4,
               f"antigravity message_count too low: {ag_manifest.get('message_count')}")

        step("Antigravity FTS hits work (tool-call content + planner response)")
        result = run_script(
            REPO_ROOT / "scripts" / "search.py", "equilibration",
            "--archive", str(archive), "--json",
        )
        parsed = json.loads(result.stdout)
        expect(any(h["session_id"] == ag_uuid for h in parsed),
               f"FTS query for antigravity content 'equilibration' didn't hit the antigravity session")

        step("Antigravity deposit verifies clean alongside other sources")
        result = run_script(REPO_ROOT / "scripts" / "verify.py", "--archive", str(archive))
        expect(result.returncode == 0, f"three-source verify failed: {result.stderr}")

        step("manifest-version migration is bundled into ONE combined commit")
        # Hand-edit one of the synthetic deposits' manifest to look stale
        # (manifest_version downgraded). Re-ingest. Confirm exactly one
        # `migrate:` commit covers it, not a per-session `update:` commit.
        mig_target = next(
            p for p in (archive / "sessions").rglob("manifest.json")
            if "antigravity" not in str(p) and "codex" not in str(p)
            and "/subagents/" not in str(p)
        )
        mig_data = json.loads(mig_target.read_text())
        mig_data["manifest_version"] = 3  # pretend it's a stale schema
        mig_target.write_text(json.dumps(mig_data, indent=2, sort_keys=True) + "\n")
        # Commit the rigged manifest so the next ingest sees it as the prior state.
        subprocess.run(["git", "-C", str(archive), "add", str(mig_target.relative_to(archive))],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(archive), "commit", "-q", "-m", "test: downgrade one manifest to v3"],
                       check=True, capture_output=True)

        before_log = subprocess.run(
            ["git", "-C", str(archive), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        before_count = len(before_log.strip().split("\n"))

        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(archive),
            "--source", str(source),
        )
        if args.verbose:
            print(result.stdout)
        expect(result.returncode == 0, f"migration ingest failed: {result.stderr}")

        after_log = subprocess.run(
            ["git", "-C", str(archive), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        new_commits = after_log.strip().split("\n")[: len(after_log.strip().split("\n")) - before_count]
        expect(len(new_commits) == 1,
               f"expected exactly 1 migration commit, got {len(new_commits)}:\n{chr(10).join(new_commits)}")
        expect("migrate:" in new_commits[0],
               f"expected `migrate:` prefix on combined commit: {new_commits[0]}")
        expect("schema v4" in new_commits[0],
               f"combined commit should mention target schema version: {new_commits[0]}")

        step("--bulk-initial bundles all new deposits into ONE combined commit")
        # Fresh archive + fresh source → ingest with --bulk-initial.
        # Expect: exactly one "bulk-initial:" commit covers all the
        # synthetic sessions, not N "deposit:" commits.
        bi_archive = tmp / "archive-bulk-initial"
        result = run_script(
            REPO_ROOT / "scripts" / "init.py",
            "--path", str(bi_archive),
            "--remote-url", "",
            "--remote-kind", "none",
            "--compression", "none",
        )
        expect(result.returncode == 0, f"bulk-initial init failed: {result.stderr}")
        bi_source = materialize_source(tmp / "bi-source-parent")
        before_log = subprocess.run(
            ["git", "-C", str(bi_archive), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        before_count = len(before_log.strip().split("\n"))
        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(bi_archive),
            "--source", str(bi_source),
            "--bulk-initial",
        )
        expect(result.returncode == 0, f"bulk-initial ingest failed: {result.stderr}\n{result.stdout}")
        expect("new=3" in result.stdout,
               f"expected new=3 deposits in bulk-initial: {result.stdout}")
        after_log = subprocess.run(
            ["git", "-C", str(bi_archive), "log", "--oneline"],
            capture_output=True, text=True, check=True,
        ).stdout
        new_commits = after_log.strip().split("\n")[: len(after_log.strip().split("\n")) - before_count]
        expect(len(new_commits) == 1,
               f"--bulk-initial should produce exactly 1 commit, got {len(new_commits)}:\n{chr(10).join(new_commits)}")
        expect("bulk-initial:" in new_commits[0],
               f"expected `bulk-initial:` prefix on combined commit: {new_commits[0]}")
        # And: all three sessions should be present in the archive AND
        # in the SQLite index.
        bi_deposits = list((bi_archive / "sessions").rglob("transcript.jsonl"))
        expect(len(bi_deposits) == 3,
               f"expected 3 deposits in bulk-initial archive, got {len(bi_deposits)}")

        step("encrypt-transcripts: refusal paths gate correctly")
        # Refuse without --remote-url.
        result = run_script(
            REPO_ROOT / "scripts" / "init.py",
            "--path", str(tmp / "should-not-exist"),
            "--remote-url", "",
            "--remote-kind", "none",
            "--compression", "none",
            "--encrypt-transcripts",
            "--i-understand-key-loss-means-data-loss",
        )
        expect(result.returncode != 0,
               f"--encrypt-transcripts without --remote-url should fail")
        expect("requires --remote-url" in result.stderr,
               f"expected --remote-url precondition message: {result.stderr}")

        # Refuse without the explicit data-loss ack.
        bare2 = tmp / "bare-encrypt-test.git"
        subprocess.run(["git", "init", "--bare", str(bare2)], check=True, capture_output=True)
        if shutil.which("git-crypt"):
            result = run_script(
                REPO_ROOT / "scripts" / "init.py",
                "--path", str(tmp / "encrypted-no-ack"),
                "--remote-url", f"file://{bare2}",
                "--remote-kind", "other",
                "--compression", "none",
                "--encrypt-transcripts",
            )
            expect(result.returncode != 0,
                   "missing --i-understand-key-loss-means-data-loss should fail")
            expect("DATA LOSS" in result.stderr or "data-loss" in result.stderr,
                   f"expected the data-loss banner in stderr: {result.stderr}")

            step("encrypt-transcripts: full init configures git-crypt + .gitattributes")
            enc_archive = tmp / "archive-encrypted"
            result = run_script(
                REPO_ROOT / "scripts" / "init.py",
                "--path", str(enc_archive),
                "--remote-url", f"file://{bare2}",
                "--remote-kind", "other",
                "--compression", "none",
                "--encrypt-transcripts",
                "--i-understand-key-loss-means-data-loss",
                "--no-auto-push",  # decouple from auto-push for this test
            )
            expect(result.returncode == 0, f"encrypted init failed: {result.stderr}")
            expect((enc_archive / ".gitattributes").exists(),
                   ".gitattributes not written")
            attrs = (enc_archive / ".gitattributes").read_text()
            expect("filter=git-crypt" in attrs,
                   f"git-crypt filter missing from .gitattributes: {attrs}")
            expect((enc_archive / ".git" / "git-crypt" / "keys" / "default").exists(),
                   "git-crypt key not initialized inside .git/")
            expect((enc_archive / "HOW_TO_BACK_UP_YOUR_KEY.md").exists(),
                   "key-backup how-to not dropped into archive")
            enc_cfg = json.loads((enc_archive / ".holotype" / "config.json").read_text())
            expect(enc_cfg["deposit"]["encrypt_transcripts"] is True,
                   f"config.deposit.encrypt_transcripts not set: {enc_cfg['deposit']}")
        else:
            print("    (skipped git-crypt setup checks — `git-crypt` not on PATH)")

        step("auto-push: init with a local bare-repo remote and confirm ingest pushes")
        bare_remote = tmp / "bare-remote.git"
        subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True,
                       capture_output=True)
        ap_archive = tmp / "archive-autopush"
        result = run_script(
            REPO_ROOT / "scripts" / "init.py",
            "--path", str(ap_archive),
            "--remote-url", f"file://{bare_remote}",
            "--remote-kind", "other",
            "--compression", "none",
            "--auto-push",
        )
        expect(result.returncode == 0, f"init --auto-push failed: {result.stderr}")
        ap_cfg = json.loads((ap_archive / ".holotype" / "config.json").read_text())
        expect(ap_cfg["deposit"]["auto_push"] is True,
               f"config.deposit.auto_push should be true: {ap_cfg['deposit']}")

        ap_src, _ = materialize_antigravity_source(tmp / "ap-source-parent")
        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(ap_archive),
            "--source", str(ap_src),
            "--source-name", "antigravity",
        )
        expect(result.returncode == 0, f"auto-push ingest failed: {result.stderr}\n{result.stdout}")
        expect("pushing to" in result.stdout, f"auto-push log line missing:\n{result.stdout}")
        expect("push OK" in result.stdout, f"auto-push didn't report success:\n{result.stdout}")

        # The bare remote should now have at least one ref under refs/heads/.
        remote_refs = list((bare_remote / "refs" / "heads").iterdir()) if (bare_remote / "refs" / "heads").exists() else []
        # Newer git stores refs in packed-refs by default for bare repos.
        packed_refs = (bare_remote / "packed-refs")
        has_ref = len(remote_refs) > 0 or packed_refs.exists()
        expect(has_ref, f"bare remote has no refs after auto-push (refs={remote_refs}, packed={packed_refs.exists()})")

        # Verify the bare remote actually contains the deposit commit by
        # cloning it and checking the manifest is there.
        clone_dir = tmp / "remote-clone"
        subprocess.run(["git", "clone", "--quiet", f"file://{bare_remote}", str(clone_dir)],
                       check=True, capture_output=True)
        cloned_manifests = list((clone_dir / "sessions").rglob("manifest.json"))
        expect(len(cloned_manifests) >= 1,
               f"auto-push didn't deliver any session to the remote (cloned manifests: {len(cloned_manifests)})")

        step("paper_bundle.py packages multiple sessions with a master manifest")
        # Pull a Claude Code session + the Codex one + the Antigravity one.
        bundle_out = tmp / "paper-bundle"
        cc_first = next(p for p in (archive / "sessions").rglob("manifest.json")
                        if "antigravity" not in str(p) and "codex" not in str(p)).parent.name
        # Use full UUIDs to disambiguate the synthetic fixtures (which
        # share the leading "00000000" prefix).
        result = run_script(
            REPO_ROOT / "scripts" / "paper_bundle.py",
            "--sessions", f"{cc_first},deadbeef,{ag_uuid}",
            "--archive", str(archive),
            "--out", str(bundle_out),
            "--paper-title", "Selftest paper",
        )
        expect(result.returncode == 0, f"paper_bundle failed: {result.stderr}\n{result.stdout}")
        bm_path = bundle_out / "BUNDLE_MANIFEST.json"
        expect(bm_path.exists(), "BUNDLE_MANIFEST.json not written")
        bm = json.loads(bm_path.read_text())
        expect(bm["session_count"] == 3, f"BUNDLE_MANIFEST session_count wrong: {bm['session_count']}")
        expect(bm["paper_title"] == "Selftest paper", "paper_title not stored")
        bundle_shas = {s["sha256"] for s in bm["sessions"]}
        expect(len(bundle_shas) == 3, f"expected 3 distinct sha256s in bundle, got {bundle_shas}")
        # Every bundled session must ship a PLAIN transcript.jsonl that
        # hashes to its recorded sha256 — that's the verify-without-zstd
        # property of paper_bundle.
        for sess in bm["sessions"]:
            sid = sess["session_id"]
            bundled = bundle_out / sid / "transcript.jsonl"
            expect(bundled.exists(), f"bundle missing transcript for {sid}")
            recomputed = sha256_file(bundled)
            expect(recomputed == sess["sha256"],
                   f"bundle transcript hash mismatch for {sid}: {recomputed} != {sess['sha256']}")
        expect((bundle_out / "VERIFY.md").exists(), "paper bundle missing VERIFY.md")

        step("usage_estimate.py emits parseable JSON from a synthetic source")
        # Monkey-patch the registered source's default paths to point at
        # the test source, then call collect() in-process.
        import sys as _sys2
        _sys2.path.insert(0, str(REPO_ROOT))
        from holotype.sources.claude_code import ClaudeCodeSource as _CC
        import importlib.util as _iu
        _saved_default_paths = _CC.default_source_paths
        try:
            _CC.default_source_paths = staticmethod(lambda: [source])
            _ue_spec = _iu.spec_from_file_location(
                "_ht_usage_for_test", str(REPO_ROOT / "scripts" / "usage_estimate.py")
            )
            ue_mod = _iu.module_from_spec(_ue_spec)
            _ue_spec.loader.exec_module(ue_mod)
            ue_data = ue_mod.collect()
            expect(ue_data["combined"]["file_count"] >= 2,
                   f"usage_estimate saw too few files: {ue_data['combined']['file_count']}")
            expect(ue_data["combined"]["total_bytes"] > 0,
                   "usage_estimate total_bytes is zero")
            expect(ue_data["combined"]["bytes_per_day"] >= 0,
                   "usage_estimate bytes_per_day went negative")
        finally:
            _CC.default_source_paths = _saved_default_paths

        step("compression: init + ingest + verify + cite end-to-end (zstd archive)")
        z_archive = tmp / "archive-zstd"
        result = run_script(
            REPO_ROOT / "scripts" / "init.py",
            "--path", str(z_archive),
            "--remote-url", "",
            "--remote-kind", "none",
            "--compression", "zstd",
        )
        expect(result.returncode == 0, f"init --compression zstd failed: {result.stderr}")
        z_cfg = json.loads((z_archive / ".holotype" / "config.json").read_text())
        expect(z_cfg["deposit"]["compression"] == "zstd",
               f"config compression wrong: {z_cfg['deposit'].get('compression')}")

        result = run_script(
            REPO_ROOT / "scripts" / "ingest.py",
            "--archive", str(z_archive),
            "--source", str(source),
        )
        expect(result.returncode == 0, f"compressed ingest failed: {result.stderr}")
        expect("new=3" in result.stdout, f"expected new=3 in compressed ingest:\n{result.stdout}")

        z_compressed = sorted((z_archive / "sessions").rglob("transcript.jsonl.zst"))
        z_plain = sorted((z_archive / "sessions").rglob("transcript.jsonl"))
        expect(len(z_compressed) == 3, f"expected 3 .jsonl.zst deposits, got {len(z_compressed)}")
        expect(len(z_plain) == 0, f"expected 0 plain .jsonl in compressed archive, got {len(z_plain)}")

        z_manifests = list((z_archive / "sessions").rglob("manifest.json"))
        for mp in z_manifests:
            m = json.loads(mp.read_text())
            expect(m["compression"] == "zstd", f"manifest compression wrong: {m.get('compression')}")
            expect(m["sha256_compressed"] is not None, "sha256_compressed not recorded")
            expect(m["sha256"] is not None, "sha256 (uncompressed) not recorded")
            expect(m["transcript_filename"] == "transcript.jsonl.zst",
                   f"transcript_filename wrong: {m.get('transcript_filename')}")

        result = run_script(REPO_ROOT / "scripts" / "verify.py", "--archive", str(z_archive))
        expect(result.returncode == 0, f"verify on compressed archive failed: {result.stderr}\n{result.stdout}")

        # Tamper detection on the compressed file should fire.
        z_first = z_compressed[0]
        original_bytes = z_first.read_bytes()
        with z_first.open("ab") as f:
            f.write(b"\x00\x00garbage\x00\x00")
        result = run_script(REPO_ROOT / "scripts" / "verify.py", "--archive", str(z_archive))
        expect(result.returncode == 1, "verify didn't catch tamper on .jsonl.zst")
        z_first.write_bytes(original_bytes)

        # Cite a compressed-archive session — bundle must contain plain .jsonl
        # so the reviewer doesn't need zstd to read the artifact.
        first_z_session = z_manifests[0].parent.name
        z_bundle = tmp / "z-bundle"
        result = run_script(
            REPO_ROOT / "scripts" / "cite.py", first_z_session,
            "--archive", str(z_archive),
            "--out", str(z_bundle),
        )
        expect(result.returncode == 0, f"cite from compressed archive failed: {result.stderr}")
        expect((z_bundle / "transcript.jsonl").exists(),
               "cite bundle from compressed archive is missing plain transcript.jsonl")
        expect(not (z_bundle / "transcript.jsonl.zst").exists(),
               "cite bundle should not ship .jsonl.zst — reviewer needs plain")
        # The plain transcript in the bundle should hash to the canonical sha256.
        bundle_manifest = json.loads((z_bundle / "manifest.json").read_text())
        bundle_hash = sha256_file(z_bundle / "transcript.jsonl")
        expect(bundle_hash == bundle_manifest["sha256"],
               f"cite bundle plain transcript hash != canonical sha256: {bundle_hash} vs {bundle_manifest['sha256']}")

        # context.py on a compressed archive should print a path to a plain
        # .jsonl cache file the caller can Read.
        result = run_script(
            REPO_ROOT / "scripts" / "context.py", first_z_session,
            "--archive", str(z_archive),
        )
        expect(result.returncode == 0, f"context on compressed archive failed: {result.stderr}")
        ctx_path = Path(result.stdout.strip())
        expect(ctx_path.exists() and ctx_path.suffix == ".jsonl",
               f"context didn't materialize a plain .jsonl: {ctx_path}")
        expect(b"{" in ctx_path.read_bytes()[:50],
               "context cache file doesn't look like JSONL")

        step("discover_candidates dedupes sessions across mirrored source paths")
        # Make a second project tree that mirrors the first — same session
        # IDs in a second location. Without dedup the same session yields
        # twice (once per path); with dedup, only once.
        mirror_src = tmp / "source-mirror"
        proj_mirror = mirror_src / "-Users-test-fixture"
        proj_mirror.mkdir(parents=True)
        basic_uuid_dup = "00000000-0000-4000-8000-000000000001"
        out_mirror = proj_mirror / f"{basic_uuid_dup}.jsonl"
        shutil.copy(FIXTURES / "synthetic-session-basic.jsonl", out_mirror)
        import os as _os, time as _time
        older = _time.time() - 60
        _os.utime(out_mirror, (older, older))

        # Run discover_candidates in-process against a Source that returns
        # BOTH source and mirror_src as default paths.
        import importlib, sys as _sys
        _sys.path.insert(0, str(REPO_ROOT))
        ingest_mod = importlib.import_module("scripts.ingest") if False else None
        # We can't easily import scripts/ingest.py as a module (hyphenated
        # filename works but the script's argparse sits at module top).
        # Instead, exercise the function via direct path manipulation.
        from holotype.sources import ALL_SOURCES
        from holotype.sources.claude_code import ClaudeCodeSource

        # Monkey-patch default_source_paths to point at our two mirrored
        # test trees, then call ingest.discover_candidates.
        saved_paths = ClaudeCodeSource.default_source_paths
        try:
            ClaudeCodeSource.default_source_paths = staticmethod(
                lambda: [source, mirror_src]
            )
            sys_path_added = str(REPO_ROOT / "scripts")
            _sys.path.insert(0, sys_path_added)
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_ht_ingest_for_test", str(REPO_ROOT / "scripts" / "ingest.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cands = mod.discover_candidates({}, None, None)
            cc_cands = [(cls, c) for cls, c in cands if cls is ClaudeCodeSource]
            basic_yields = [c for _, c in cc_cands
                            if c.session_id == basic_uuid_dup]
            expect(
                len(basic_yields) == 1,
                f"dedup failed: session yielded {len(basic_yields)} times across mirrored paths",
            )
        finally:
            ClaudeCodeSource.default_source_paths = saved_paths

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
