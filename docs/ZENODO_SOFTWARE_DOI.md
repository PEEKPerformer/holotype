# Registering `holotype` itself on Zenodo for a software DOI

This is a separate workflow from `docs/PUBLISHING_TO_ZENODO.md`. That document is about depositing *paper bundles* (a curated subset of session transcripts that a paper cites). This document is about minting a DOI for the `holotype` tool itself, so the tool can be cited as software in papers that use it.

## Why a separate DOI

A paper that uses `holotype` should cite two things:

1. **The tool**: `holotype v2.0.x` at the Zenodo software DOI minted from the GitHub release. This goes in the Methods or Software section.
2. **The deposited session bundle**: a separate Zenodo deposit per paper, containing only the cited transcripts. This goes in the Data Availability Statement.

These deposits are independent. The tool's DOI tracks releases of `holotype` (one per tagged release on GitHub). The bundle's DOI tracks a specific paper's session subset.

## Setup (one-time)

### 1. Authorize Zenodo to watch the GitHub repo

1. Sign in to <https://zenodo.org> with your GitHub account (or link an existing Zenodo account to GitHub via Account → Linked Accounts).
2. Navigate to <https://zenodo.org/account/settings/github/>.
3. Find `PEEKPerformer/holotype` in the repository list. Toggle it ON.

This authorizes Zenodo to receive a webhook from GitHub on every new release event. The toggle is per-repo and per-user.

### 2. Verify the integration

Zenodo's GitHub integration only fires for **new releases created after the toggle was turned on.** Existing releases (`v1.0.0` through `v2.0.1` as of this writing) won't get DOIs retroactively. Two ways to handle:

- **Option A: accept the gap and start fresh from the next release.** The current v2.0.1 release stays on GitHub but doesn't get a Zenodo DOI. The next release (e.g., `v2.0.2` or any future release) is the first one with a DOI. This is the cleaner path for most projects.
- **Option B: delete + recreate the v2.0.1 release on GitHub.** This makes Zenodo see it as new. Costs a few seconds; risks losing the release's view count (negligible at submission time).

For `holotype`: **Option A is recommended**: accept the gap, start fresh from the next release.

## Cutting a release that mints a DOI

After the toggle in step 1 is on:

1. Land your changes and update `CHANGELOG.md`.
2. Bump the version in `holotype/__init__.py` AND `pyproject.toml`.
3. Update `version:` and `date-released:` in `CITATION.cff` to match.
4. Commit: `git commit -m "vX.Y.Z: ..."` and tag: `git tag -a vX.Y.Z -m "..."`.
5. Push: `git push origin main && git push origin vX.Y.Z`.
6. Create the GitHub Release: `gh release create vX.Y.Z --verify-tag --notes-from-tag --title vX.Y.Z`.

Zenodo's webhook fires immediately on release creation. Within ~5-30 minutes you should see the new deposit appear at <https://zenodo.org/account/settings/github/>. Open the deposit, click **Edit**, and:

- Fill in author affiliation if not auto-populated from CITATION.cff
- Choose a license (should match the repo's MIT)
- Confirm the "Reserved DOI" is the right version DOI (Zenodo assigns it pre-publish)
- Optionally add keywords beyond what CITATION.cff provides
- Click **Publish**

After publish:

- The version DOI is live (e.g., `10.5281/zenodo.NNNNNNN`)
- The concept DOI (one level up; same prefix, different suffix) is also live and always resolves to the latest version
- Both DOIs are listed on the deposit's Zenodo page

## Updating the README badge

After the first DOI mints, add a Zenodo DOI badge to the top of `README.md`:

```markdown
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.NNNNNNN.svg)](https://doi.org/10.5281/zenodo.NNNNNNN)
```

Use the **concept DOI** (not the version DOI) so the badge always points at the latest published version. The badge gets updated automatically as new versions mint.

## What if Zenodo's integration silently fails?

It happens occasionally: webhook gets lost, Zenodo's GitHub indexer hiccups, etc. Symptoms: a release is created on GitHub but doesn't appear in Zenodo's integration list.

Recovery:

1. Check <https://zenodo.org/account/settings/github/>. The repo should still show "ON."
2. On the repo's row, expand the dropdown. Recent releases should be listed. If the missing release is in the list but shows no DOI, click "Sync" or wait another 30 minutes.
3. If the release isn't in the list at all, toggle the repo OFF and back ON, then re-create the GitHub release (`gh release delete vX.Y.Z && gh release create vX.Y.Z --verify-tag --notes-from-tag`).
4. Last resort: manually create a Zenodo deposit, upload the release tarball (downloadable from `https://github.com/PEEKPerformer/holotype/archive/refs/tags/vX.Y.Z.tar.gz`), and link to the GitHub release in the description. This costs the auto-update of the badge but works.

Zenodo's GitHub integration is reliable enough in practice that recovery rarely needs more than a wait or a toggle.

