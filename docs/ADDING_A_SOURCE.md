# Adding a Source to holotype

You are reading this because you are an LLM invoked through an agent CLI that holotype does not yet recognize. Your CLI stores session transcripts somewhere, in some format, and the user wants you to archive them with holotype's forensic guarantees alongside their other sessions.

This document is your contract. If you follow it, the LLM who reads the archive in 5 years can verify, search, and cite your transcripts the same way they verify, search, and cite Claude Code or Codex transcripts. The rest of holotype does not need to change to accommodate your CLI — only one new file in `holotype/sources/` does.

## The contract you're implementing

```python
from holotype.sources.base import Source, DepositCandidate, MessageInfo

class MyCliSource(Source):
    name = "my-cli"  # stable, lowercase, hyphen-separated

    @staticmethod
    def default_source_paths() -> list[Path]:
        # The deepest dirs that hold *only* session transcripts.
        # MUST NOT include parents containing auth, credentials, or
        # config. holotype will never walk above the paths you return.
        ...

    @staticmethod
    def discover(source_root: Path) -> Iterator[DepositCandidate]:
        # Yield one DepositCandidate per transcript file under source_root.
        # MUST refuse to traverse outside source_root (e.g. symlinks
        # that resolve elsewhere — see CodexSource.discover for the
        # pattern).
        ...

    @staticmethod
    def parse_line(line: bytes) -> MessageInfo | None:
        # Parse one transcript line. Return None for filler lines
        # (state markers, blank lines, etc.) so they don't inflate
        # the message_count. Return None for session-header pseudo-
        # records with flags={"header"} if your format has one.
        ...

    @staticmethod
    def session_id_from_filename(jsonl_path: Path) -> str:
        # Whatever uniquely identifies a session given its on-disk
        # filename. Usually a UUID extracted from the stem.
        ...
```

That's it. Implement these four methods, drop the file in `holotype/sources/`, register it in `holotype/sources/__init__.py`'s `ALL_SOURCES` list, and holotype's ingest / cite / verify / search / context all work with your CLI's transcripts.

## How to discover your CLI's transcript schema

Don't guess from documentation. Inspect actual files on the user's machine. The process:

1. **Find where the CLI stores transcripts.** Look in `~/.<cli-name>/`, `~/Library/Application Support/<CLI>/`, `~/.config/<cli-name>/`. Look for files whose names include UUIDs, timestamps, "session", "conversation", "thread", "rollout". Run `ls -la` and read what's there. Confirm with the user before assuming.

2. **Categorize the sensitivity of nearby files.** If `auth.json`, `credentials`, `*.key`, `*.token`, or `config.toml` with API keys live near the transcripts, your `default_source_paths()` MUST point at the deepest dir that contains only transcripts. holotype enforces no-walk-up at the ingest level — make that boundary explicit.

3. **Pick the largest transcript file you can find** (`find <dir> -name '*.jsonl' -o -name '*.json' | xargs wc -l | sort -rn | head -5`) and read its first ~20 lines. Most agent CLIs use JSONL where each line is one event, but some use a single JSON document with a `messages` array, and a few use SQLite. Figure out which.

4. **Map your format's roles to the `MessageInfo` fields:**
   - `role` — `"user"` / `"assistant"` / `"tool_result"` / etc. Whatever your CLI calls these.
   - `timestamp` — ISO-8601 string preferred. If your CLI uses Unix epoch, convert.
   - `model` — the model ID for assistant turns. None for user turns. Don't put the *provider* name here (e.g. "openai", "anthropic") — those aren't model IDs and they pollute `manifest.models`.
   - `has_tool_use` — True if this turn contains a tool call.
   - `has_thinking` — True if this turn contains a reasoning / thinking block.
   - `fts_content` — flat text of EVERYTHING in the turn that's searchable. **Do not filter for "noise".** Include tool inputs, tool outputs, thinking blocks, system reminders. Holotype's pitch is forensic completeness; a search index that drops content violates it.
   - `flags` — set of source-specific markers. `{"compaction"}` if this line is a context-compaction summary. `{"header"}` if it's a header / control / telemetry pseudo-record that shouldn't count toward `message_count` (the timestamps on header-flagged lines still update `first_timestamp`/`last_timestamp` and `session_metadata`/`usage` on them are still harvested — header just means "not a user-visible message").
   - `usage` — *Optional, manifest v4.* When the host CLI exposes a per-turn token usage block (Claude Code's `message.usage`, Codex's `event_msg.token_count`), put the raw dict here. The manifest scanner aggregates `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` / `cached_input_tokens` automatically. Leave `None` if your CLI doesn't report usage.
   - `token_count_kind` — *Optional, manifest v4.* `"cumulative"` if the `usage` dict is a running total (Codex's `total_token_usage`), `"delta"` if it's per-turn billing (Claude Code's per-message `usage`), or `None` if no usage was attached. The aggregator prefers a final cumulative reading over summed deltas.
   - `session_metadata` — *Optional, manifest v4.* For a header-flagged line that carries session-start metadata (Codex's `session_meta` carries the git block at session-start), put a dict here. Recognized keys: `cwd`, `cli_version`, `originator`, `model_provider`, `git_state: {commit, commit_short, branch, dirty, remote, captured_at}`. When `git_state.captured_at == "session-start"`, the manifest will prefer your header-derived state over the deposit-time probe — more accurate, because it was captured by the host CLI at the moment the session ran.

5. **Decide your archive layout** in `DepositCandidate.archive_subpath`. The convention is `<source-name>/<project-or-date>/<session-id>/`. Examples:
   - `claude-code/<project-dir>/<session-uuid>/`
   - `claude-code/<project-dir>/<parent-uuid>/subagents/<sub-id>/` (nested)
   - `codex/<YYYY>/<MM>/<DD>/<session-uuid>/` (date-partitioned)

   Whatever's natural for your CLI's storage. The shape doesn't matter for correctness — `holotype/archive.py`'s `iter_all_sessions` walks arbitrary depth — but a layout that mirrors how the user thinks about their sessions makes manual inspection easier.

## How to verify your implementation works

Before declaring done:

1. **Run the selftest** with your source registered:
   ```bash
   python scripts/selftest.py --verbose
   ```
   It tests the contract end-to-end with synthetic fixtures. Failure modes you'll see:
   - `dedup failed` → your `default_source_paths` returns paths that overlap. Either trim them, or accept that holotype will dedup by `(source.name, session_id)`.
   - `manifest source wrong` → you forgot to set `Source.name`.
   - `FTS query returned 0 hits` → your `parse_line` is returning empty `fts_content`. Forensic completeness violated.
   - `MISSING_HASH` in verify → your manifest isn't being written. Check that `parse_line` returns a non-None `MessageInfo` for at least one line per file.

2. **Add a synthetic fixture** at `tests/fixtures/synthetic-<your-cli>-session.<ext>`. Mirror what a real session from your CLI looks like, with at least one user turn, one assistant turn, one tool call if your CLI supports them, and one thinking block if it supports them. Then extend `scripts/selftest.py` with assertions specific to your source. Look at how the Codex source's assertions are written for the pattern.

3. **Hash check by hand** against a real session: run `python scripts/ingest.py --source <real-source-dir> --source-name <your-name>` against the user's actual transcripts, then `python scripts/verify.py`. The verify pass must be clean.

## What NOT to do

- **Do not** filter content for "noise" in `parse_line`. Tool calls, system reminders, hook outputs — all of it. The whole point of holotype is preserving what other tools throw away.
- **Do not** modify transcripts in place. Read-only with stable-mtime checks (see `read_with_stable_check` in `scripts/ingest.py` for the pattern). If your CLI's transcripts can be modified between reads, your source is automatically incompatible with holotype's hash-chain — flag it to the user and stop.
- **Do not** invent the on-disk schema. If your CLI doesn't document its transcript format and you can't infer it confidently from a real file, **defer**. A wrong source class will deposit garbage that passes hash checks but is unparseable to a reviewer 5 years from now.
- **Do not** widen `default_source_paths` to include directories with credentials. Codex's `~/.codex/` contains `auth.json` next to `sessions/`; we point at `sessions/` specifically. Do the analogous thing for your CLI.
- **Do not** push the archive to a remote without explicit user consent on this session, *every time*. The `--no-auto-push` discipline applies regardless of how the Source was added.

## When to give up and tell the user

If you can't confidently answer any of these, stop and tell the user honestly:

- Where does my CLI store transcripts on this user's filesystem?
- What is the per-line schema?
- Does my CLI ever modify already-written transcripts (overwrite vs append-only)?
- Are there credentials adjacent to the transcript dir that I might accidentally read?

A missing Source is recoverable later. A wrong Source corrupts the archive's forensic claim.

## Registration

Once your file at `holotype/sources/<your_cli>.py` exists and selftest passes, register it:

```python
# holotype/sources/__init__.py
from holotype.sources.my_cli import MyCliSource

ALL_SOURCES: list[type[Source]] = [
    ClaudeCodeSource,
    CodexSource,
    AntigravitySource,
    MyCliSource,  # <-- add here
]
```

That's the only file outside `holotype/sources/` that you need to touch.

## A note on schema drift

Agent CLIs evolve fast. The Source classes shipped here capture the schemas observed at the time they were written; nothing prevents a vendor from renaming fields, adding new record types, or moving directory paths in the next release. If this guide and the actual filesystem disagree, **trust the filesystem**. The non-negotiables (forensic completeness, append-only, hash-chain integrity) are stable; the mechanics of "where the bytes live and how they're shaped" are not. Patch the Source, update the fixture, re-run selftest.
