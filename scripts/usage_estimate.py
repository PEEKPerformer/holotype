#!/usr/bin/env python3
"""Estimate how much disk the holotype archive will consume per month.

Read-only. Walks each registered Source's default source paths, sums
JSONL byte counts, and projects future growth from the date span
(min/max file mtime) of what's there now.

Intended use:
  - In the setup wizard (Step 2: Storage projection) so the user can
    consent to the storage cost before init.
  - Anywhere a user wants to recheck "how much is my session traffic
    actually costing me?"

This script does NOT need an existing archive — it reads source
directories only (e.g. ``~/.claude/projects/``, ``~/.codex/sessions/``).
That's intentional: the wizard runs before init, so no archive exists
yet. For a post-setup growth measurement you'd look at git history of
the archive itself; that's a separate concern.

Usage:
    python scripts/usage_estimate.py            # human-readable
    python scripts/usage_estimate.py --json     # for the wizard
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from holotype.sources import ALL_SOURCES


def _gb(n: int) -> float:
    return n / (1024 ** 3)


def _mb(n: int) -> float:
    return n / (1024 ** 2)


def _human_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{_gb(n):.2f} GB"
    if n >= 1024 ** 2:
        return f"{_mb(n):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def walk_source(source_cls) -> dict:
    """Return per-source stats: total bytes, file count, mtime bookends, paths.

    Walks every default_source_path() the Source declares, collecting
    every ``*.jsonl`` under it. Deduplicates by resolved file path so a
    Source that lists two paths mirroring the same content (e.g. Claude
    Code's rsync mirror + live dir) doesn't double-count.
    """
    found_paths: list[str] = []
    seen: set[Path] = set()
    total_bytes = 0
    file_count = 0
    min_mtime: float | None = None
    max_mtime: float | None = None

    for root in source_cls.default_source_paths():
        root = root.expanduser()
        if not root.exists():
            continue
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        found_paths.append(str(root))

        for jsonl in root_resolved.rglob("*.jsonl"):
            if not jsonl.is_file():
                continue
            try:
                real = jsonl.resolve(strict=True)
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            try:
                st = jsonl.stat()
            except OSError:
                continue
            total_bytes += st.st_size
            file_count += 1
            mt = st.st_mtime
            if min_mtime is None or mt < min_mtime:
                min_mtime = mt
            if max_mtime is None or mt > max_mtime:
                max_mtime = mt

    return {
        "name": source_cls.name,
        "found_paths": found_paths,
        "total_bytes": total_bytes,
        "file_count": file_count,
        "earliest_mtime": min_mtime,
        "latest_mtime": max_mtime,
    }


def project(combined: dict) -> dict:
    """Compute bytes/day, projected MB/month, MB/year from combined stats.

    ``span_days`` is floored to 1 to avoid divide-by-zero when all files
    share an mtime. ``low_confidence`` flags spans under 7 days so the
    caller can warn the user.
    """
    total_bytes = combined["total_bytes"]
    min_mt = combined["earliest_mtime"]
    max_mt = combined["latest_mtime"]

    if min_mt is None or max_mt is None:
        return {
            "bytes_per_day": 0,
            "bytes_per_month": 0,
            "bytes_per_year": 0,
            "span_days": 0.0,
            "low_confidence": True,
            "no_data": True,
        }

    span_seconds = max(max_mt - min_mt, 0.0)
    span_days_raw = span_seconds / 86400.0
    span_days = max(span_days_raw, 1.0)

    bytes_per_day = total_bytes / span_days
    return {
        "bytes_per_day": int(bytes_per_day),
        "bytes_per_month": int(bytes_per_day * 30),
        "bytes_per_year": int(bytes_per_day * 365),
        "span_days": round(span_days_raw, 2),
        "low_confidence": span_days_raw < 7.0,
        "no_data": False,
    }


def collect() -> dict:
    per_source = [walk_source(cls) for cls in ALL_SOURCES]

    combined_bytes = sum(s["total_bytes"] for s in per_source)
    combined_files = sum(s["file_count"] for s in per_source)
    earliest = min((s["earliest_mtime"] for s in per_source if s["earliest_mtime"]), default=None)
    latest = max((s["latest_mtime"] for s in per_source if s["latest_mtime"]), default=None)

    combined = {
        "total_bytes": combined_bytes,
        "file_count": combined_files,
        "earliest_mtime": earliest,
        "latest_mtime": latest,
    }
    combined.update(project(combined))

    from datetime import datetime, timezone

    def iso(t: float | None) -> str | None:
        if t is None:
            return None
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds")

    combined["earliest_iso"] = iso(earliest)
    combined["latest_iso"] = iso(latest)

    sources_out = []
    for s in per_source:
        sources_out.append({
            "name": s["name"],
            "found_paths": s["found_paths"],
            "file_count": s["file_count"],
            "total_bytes": s["total_bytes"],
            "earliest_iso": iso(s["earliest_mtime"]),
            "latest_iso": iso(s["latest_mtime"]),
        })

    return {
        "sources": sources_out,
        "combined": combined,
    }


def render_human(data: dict) -> str:
    out: list[str] = []
    c = data["combined"]

    if c.get("no_data"):
        out.append("holotype usage estimate: no source data found")
        out.append("")
        out.append("  Looked for:")
        for s in data["sources"]:
            for p in s["found_paths"]:
                out.append(f"    - {p}")
            if not s["found_paths"]:
                out.append(f"    - (no default paths for {s['name']} exist)")
        out.append("")
        out.append("  Projection unavailable until you've used the host CLI for a few days.")
        return "\n".join(out) + "\n"

    out.append("holotype usage estimate")
    out.append("")
    out.append(f"  Sampled:        {c['file_count']} session file(s), {_human_bytes(c['total_bytes'])}")
    out.append(f"  Date span:      {c['span_days']:.1f} days"
                + ("  (low confidence — short window)" if c["low_confidence"] else ""))
    out.append(f"  Per day:        {_human_bytes(c['bytes_per_day'])}")
    out.append(f"  Per month:      {_human_bytes(c['bytes_per_month'])}  (uncompressed)")
    out.append(f"  Per year:       {_human_bytes(c['bytes_per_year'])}  (uncompressed)")
    out.append("")
    out.append("  Notes:")
    out.append("    - Uncompressed projection. Git-pack compression typically saves ~30-50%.")
    out.append("    - zstd at-deposit compression (optional in wizard) typically saves another")
    out.append("      30-60% on top — savings vary with content (small text-heavy turns compress")
    out.append("      best; sessions dominated by tool results with base64/random data less so).")
    out.append("    - These numbers are extrapolated from your current source-dir contents.")
    out.append("")
    out.append("  Per-source breakdown:")
    for s in data["sources"]:
        if s["file_count"] == 0:
            continue
        out.append(f"    {s['name']:>12s}:  {s['file_count']:>5d} files,"
                    f" {_human_bytes(s['total_bytes']):>10s}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Estimate holotype's per-month disk cost from existing source data.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of formatted text.")
    args = p.parse_args(argv)

    data = collect()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        sys.stdout.write(render_human(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
