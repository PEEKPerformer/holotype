# What it won't do

A tool earns trust as much by what it refuses to do as by what it promises. Stating the boundaries plainly is part of not being a black box: if a guarantee isn't on this list, don't assume it.

## It is not a general-purpose backup (though it does back up your transcripts)

Be honest: holotype copies your transcripts into a separate git archive and, if you ask, pushes them off-site. That is a backup. For the data it covers it is a better one than rsync, because the copy is immutable, deduplicated, and caught up automatically so a long session can't slip through.

Two boundaries keep it from being your whole backup story:

- It backs up agent-CLI transcripts, not your photos, code, or documents. Keep a general backup for those.
- A local-only archive lives on the same disk as the original. Set a remote, and keep your normal backups too, so a single disk failure can't take both.

Why "backup" still undersells it: a backup's job ends at "you can get the bytes back." holotype's job is "you can prove these exact bytes are the ones the experiment produced, and a reviewer can confirm it without trusting you." The copy is necessary. The provenance is the point.

## holotype is not a search tool

There is a search box (FTS5 over a derived SQLite index), and it's genuinely useful for *finding* the session you want to cite. But it is a convenience layer, not the product. The product is the verbatim, hash-chained archive. If we ever had to choose between a better search experience and forensic completeness, completeness wins. Every time.

## It does not filter, summarize, or normalize

No "noise" removal, no re-encoding, no clever canonicalization of the JSONL. The raw bytes are the specimen. This is the single deliberate difference from the search-first tools, and it's non-negotiable. (See [principle 1](principles.md#1-forensic-completeness).)

## It does not trigger itself

holotype is **deliberately manual**. It is never fired by phrases, keywords, or background heuristics. Every deposit is an explicit human action, because putting data into a scientific archive is a decision a person should make consciously. The background tick only *catches up* sessions for an archive you already set up by hand.

## It does not push anywhere you didn't configure

No remote is ever set silently. Auto-push, when on, only ever targets the remote you explicitly configured at init, after an explicit warning that transcripts will be sent there. (See [principle 6](principles.md#6-network-behavior-follows-init-time-consent-never-silent-defaults).)

## The encryption boundary, stated honestly

Optional `git-crypt` encryption protects transcript *content* on a backup remote that shouldn't see it. What it is **not**:

- **Manifests stay plaintext.** They hold hashes and environment metadata, by design, so verification can happen without decrypting.
- It is **opt-in**, with a loud data-loss warning at init: lose the key and you lose the content. There is no recovery path holotype can offer for a lost key.
- It guards the *remote*, not your local disk. Your working archive is unencrypted on the machine that created it.

This protects against "I want an off-site copy but my backup host shouldn't read my transcripts." It does **not** claim to be a defense against an attacker with access to your local machine, and we don't pretend otherwise.

## Things we have deliberately *not* built (yet)

- Sources for Cline, Cursor (automatic), and aider are **deferred**, not refused: they're a documented one-file extension away.
- The Windows background tick is left to the user's Task Scheduler; the core skill works there, but we don't ship the scheduling glue.
- No hosted service, no telemetry, no account. The archive is yours, on git, full stop.

---

If a future change would quietly cross one of these lines, that's the signal to stop and reconsider, not to ship it.

[:octicons-arrow-right-24: Now prove it: verify an archive yourself](verify.md)
