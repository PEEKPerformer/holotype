---
name: Bug report
about: Something is broken or behaving unexpectedly
title: ''
labels: bug
assignees: PEEKPerformer
---

## Summary

One sentence on what's wrong.

## Environment

- **holotype version**: `python -c "import holotype; print(holotype.__version__)"`
- **OS**: macOS 14.x / Ubuntu 22.04 / etc.
- **Python**: `python3 --version`
- **Host agent CLI(s) involved**: Claude Code x.y.z / Codex x.y.z / Antigravity x.y.z
- **External binaries**:
  - `git --version`
  - `zstd --version` (if compression is on)
  - `git-crypt --version` (if encryption is on)
  - `gpg --version` (if signing is on)

## Steps to reproduce

1. ...
2. ...
3. ...

## Expected behavior

What you expected to happen.

## Actual behavior

What actually happened. Include exact error output if any.

## Archive state (if applicable)

If the bug involves an existing archive, paste the output of:

```bash
python scripts/verify.py --json --archive <path-to-archive> | head -50
```

Plus the relevant `manifest.json` (with any sensitive content redacted) if the issue is per-session.

## Selftest

Did you run `python scripts/selftest.py`? Does it pass on its own?

- [ ] Yes, selftest passes
- [ ] No, selftest also fails — paste failure output below
- [ ] N/A — bug is in setup, before selftest is meaningful

```
(selftest output if relevant)
```

## Additional context

Anything else that might help — config.json contents (with secrets redacted), git log of the affected session's deposit, the specific source-CLI session that triggered it, etc.
