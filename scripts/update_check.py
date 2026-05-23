#!/usr/bin/env python3
"""Report whether a newer holotype release is available on GitHub.

User-invokable only. Hits the GitHub releases API anonymously (60 req/hr/IP
limit; cached for 24h at ~/.cache/holotype/update_check.json) so the launchd
tick is not affected. Always exits 0 — a network failure must not break
whatever workflow called this.

Output: human-readable line on stdout, or `--json` for machine consumption.
The skill prompt instructs the LLM to run `--json` at the start of any
holotype operation and surface the result to the user.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "PEEKPerformer/holotype"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT_SECONDS = 5
CACHE_TTL_SECONDS = 24 * 3600


def local_version() -> str:
    init_py = Path(__file__).resolve().parent.parent / "holotype" / "__init__.py"
    try:
        for line in init_py.read_text().splitlines():
            if line.startswith("__version__"):
                return line.split('"')[1]
    except OSError:
        pass
    return "unknown"


def cache_path() -> Path:
    return Path.home() / ".cache" / "holotype" / "update_check.json"


def read_cache(max_age: float) -> str | None:
    p = cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(data.get("fetched_at", 0)) > max_age:
        return None
    tag = data.get("latest_tag")
    return tag if isinstance(tag, str) else None


def write_cache(latest_tag: str) -> None:
    p = cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"latest_tag": latest_tag, "fetched_at": time.time()}))
    except OSError:
        pass


def fetch_latest() -> str | None:
    req = urllib.request.Request(
        API_URL,
        headers={
            "User-Agent": f"holotype-update-check/{local_version()}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    tag = payload.get("tag_name")
    return tag if isinstance(tag, str) else None


def parse_version(v: str) -> tuple[int, ...]:
    parts = v.lstrip("v").split(".")
    out: list[int] = []
    for p in parts[:3]:
        try:
            out.append(int(p))
        except ValueError:
            return (0, 0, 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-cache", action="store_true", help="bypass 24h cache")
    args = ap.parse_args()

    local = local_version()

    latest: str | None = None
    cache_hit = False
    if not args.no_cache:
        latest = read_cache(CACHE_TTL_SECONDS)
        cache_hit = latest is not None
    if latest is None:
        latest = fetch_latest()
        if latest:
            write_cache(latest)

    if latest is None:
        result = {"status": "unreachable", "local": local}
    else:
        cmp_local = parse_version(local)
        cmp_latest = parse_version(latest)
        if cmp_latest > cmp_local:
            result = {"status": "update-available", "local": local, "latest": latest}
        elif cmp_latest < cmp_local:
            result = {"status": "ahead", "local": local, "latest": latest}
        else:
            result = {"status": "current", "local": local, "latest": latest}
    result["cache_hit"] = cache_hit

    if args.json:
        print(json.dumps(result))
    else:
        s = result["status"]
        if s == "update-available":
            repo_root = Path(__file__).resolve().parent.parent
            print(f"holotype: update available {local} -> {result['latest']}")
            print(f"  to update: git -C {repo_root} pull")
        elif s == "current":
            print(f"holotype: up to date ({local})")
        elif s == "ahead":
            print(f"holotype: local {local} is newer than published {result['latest']} (running from main?)")
        else:
            print(f"holotype: could not check for updates (network or GitHub unreachable)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
