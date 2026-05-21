"""Pluggable sources for holotype ingest.

Each Source knows how to discover deposit candidates on disk for one
agent-CLI implementation (Claude Code, Codex, etc.) and how to extract
metadata + FTS content from that CLI's transcript format.

The Source ABC is the seam — adding a new agent CLI means adding one
file in this package, not touching ingest.py.
"""

from holotype.sources.base import Source, DepositCandidate, MessageInfo
from holotype.sources.claude_code import ClaudeCodeSource
from holotype.sources.codex import CodexSource

# Registry — order matters for default-source discovery. Add new sources
# in ingest priority order (cleanest formats first).
ALL_SOURCES: list[type[Source]] = [
    ClaudeCodeSource,
    CodexSource,
]


def source_by_name(name: str) -> type[Source]:
    for s in ALL_SOURCES:
        if s.name == name:
            return s
    raise KeyError(f"unknown source: {name}")


__all__ = [
    "Source",
    "DepositCandidate",
    "MessageInfo",
    "ClaudeCodeSource",
    "CodexSource",
    "ALL_SOURCES",
    "source_by_name",
]
