# Security policy

## Reporting a vulnerability

If you discover a vulnerability in `holotype` — particularly one that could affect the forensic integrity of an archive cited from a published paper — please report it privately rather than opening a public issue.

**Contact**: brendenferland@gmail.com with the subject line `[holotype security]`.

Please include:

- A description of the vulnerability and its impact
- Affected versions (run `python -c "import holotype; print(holotype.__version__)"` if you can)
- Reproduction steps or a proof-of-concept
- Your suggested fix, if you have one

You should receive an acknowledgement within 7 days. If you don't, please send a follow-up — email filters happen.

I'll work with you to confirm the issue, prepare a fix, and coordinate disclosure timing. For non-critical issues, a typical timeline is:

1. Acknowledgement (within ~7 days)
2. Reproduction + impact assessment (within ~14 days)
3. Patch developed + reviewed (timing depends on severity)
4. Patched version released with a public CHANGELOG entry crediting the reporter (unless they prefer otherwise)
5. Public disclosure of the vulnerability details after the patched version has been out for a reasonable interval (typically 30 days, longer for severe issues that may affect already-published paper archives)

For severe issues — particularly anything that could let an attacker silently alter a deposited transcript while leaving the SHA-256 chain "verifying clean" — I'll prioritize a fix and may release a hotfix outside the normal release cadence.

## Threat model

`holotype` makes specific security claims and explicitly does not make others. Knowing the difference matters for what counts as a vulnerability.

### Claims `holotype` does make

- **Per-session SHA-256 integrity.** Modifying a deposited `transcript.jsonl` after the fact should cause `scripts/verify.py` to report `TAMPER` for that session. A way to silently alter a transcript while keeping verify clean is a vulnerability.
- **Hash chain verifiable with stock Unix tools.** A reviewer with `shasum`, `jq`, `git`, and optionally `zstd` should be able to verify any deposit without installing `holotype`. A change that breaks this verifier-independence is a vulnerability.
- **Append-only manifests in git history.** Even when a manifest is "updated" (schema bump, session bytes grew), the prior manifest is preserved in git history. Loss of prior manifest state is a vulnerability.
- **Sensitivity boundaries on Source roots.** Each `Source.default_source_paths()` returns the deepest dir that holds only transcripts — `holotype` must not read outside those boundaries (e.g., Codex's `~/.codex/auth.json` sits next to `sessions/`; reading it would be a vulnerability).
- **`auto_push` only fires after explicit init-time consent.** A path where `holotype` pushes to a remote that was not explicitly configured at init is a vulnerability.
- **Encryption locks at init time.** If `config.deposit.encrypt_transcripts: true`, transcripts must be filtered through `git-crypt` before reaching the git object store. A path where an encrypted-archive deposit lands on the remote in plaintext is a vulnerability.

### Claims `holotype` does NOT make

- **Encryption of metadata.** With `--encrypt-transcripts` on, only transcript file contents are encrypted. Manifests stay plaintext on the remote (session IDs, timestamps, models, project paths, token totals are visible). This is the documented tradeoff. Metadata visibility on the remote is not a vulnerability.
- **Protection against compromised host CLIs.** If the host CLI (Claude Code, Codex, Antigravity) itself is malicious or compromised and writes false data to the source JSONL, `holotype` will faithfully deposit that false data. The chain is "what the host CLI wrote, byte-for-byte" — not "what actually happened in the LLM session."
- **Protection against the user's own machine.** A keylogger or filesystem attacker with read access to the user's `~/.claude/projects/` (or equivalent) sees the transcripts before `holotype` ever touches them.
- **Quantum-resistance.** SHA-256 is the chain hash. If SHA-256 is broken in the future, the integrity claim weakens (but the archive's git history + git-crypt encryption layers provide defense in depth).
- **Recovery from lost git-crypt keys.** Losing the GPG key makes encrypted deposits unrecoverable. This is the documented data-loss tradeoff of opting into encryption. See `HOW_TO_BACK_UP_YOUR_KEY.md`.

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 2.0.x   | ✅ active          |
| 1.2.x   | ⚠️ security fixes only |
| 1.1.x   | ❌ not supported   |
| 1.0.x   | ❌ not supported   |
| < 1.0   | ❌ never released  |

Security fixes are backported to the most recent v1.2.x patch line for archives that haven't migrated to v2.0 yet, but only for the threat-model-relevant claims listed above. Performance regressions, UX polish, and non-security bug fixes are not backported.

## Past advisories

None to date.
