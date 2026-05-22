# Publishing a holotype bundle to Zenodo

This document covers the Zenodo-side mechanics of depositing a holotype paper bundle for citation in a peer-reviewed paper. The holotype side of the workflow (generating the bundle) is documented in the [`Tutorial: citing an LLM session in your paper`](../README.md#tutorial-citing-an-llm-session-in-your-paper) section of the project README. This document picks up where that tutorial ends — at the moment you have a `paper_bundle.py`-generated directory or tarball and need to turn it into a DOI you can cite.

> **Caveat — Zenodo is a moving target.** Zenodo runs on InvenioRDM and ships features continuously: UI flows shift, API endpoints get deprecated and superseded, file/size quotas change, community identifiers come and go, license vocabularies evolve. Treat the specific numbers, endpoints, field names, and community IDs in this document as **directionally correct but verifiable** — confirm anything load-bearing for your paper against the current [`zenodo.org/help`](https://zenodo.org/help), [`developers.zenodo.org`](https://developers.zenodo.org/), and the [InvenioRDM REST API reference](https://inveniordm.docs.cern.ch/) before you click Publish. The workflow *shape* (generate bundle → upload → choose access + license → mint DOI → cite in paper → version-on-update) is stable. The mechanics around that shape are not. If you spot a discrepancy against Zenodo's live docs, open an issue on the holotype repo so the next user gets a corrected version.

## Prerequisites

- **Zenodo account** linked to your **ORCID iD**. Both are free. Linking ORCID at signup time (not after the fact) gives the cleanest author attribution on the published deposit.
- A holotype paper bundle. Either:
  - A directory: `~/Desktop/zenodo-deposit/` with per-session subdirectories + `BUNDLE_MANIFEST.json` + `VERIFY.md`, OR
  - A tarball: `zenodo-deposit.tar.gz` + `zenodo-deposit.tar.gz.sha256` sidecar (produced by `paper_bundle.py --tarball`).
- (Optional) The DOI of the paper this deposit supports, if it has already been assigned (most journals issue the paper DOI at acceptance, before final publication).

## 1. Choose the access mode before you upload

Zenodo's current (post-2023 InvenioRDM migration) model uses a binary **Public / Restricted** visibility, with **Embargo** as a date-bounded sub-option of Restricted. The legacy four-value `access_right` (`open` / `embargoed` / `restricted` / `closed`) still works via the legacy REST API and is what the API recipe in §4 uses.

In current Zenodo UI terms:

- **Public** — anyone can download. Use for sessions you're publishing alongside a paper. This is the default for paper-citation deposits.
- **Restricted (with Embargo)** — files are private until a chosen release date; metadata + DOI are public immediately. Use when the paper is under review and you want a DOI for the manuscript's DAS but don't want reviewers (other than the journal's) accessing the bundle until publication.
- **Restricted (indefinite)** — files are private indefinitely; access requests must be approved by you. Use for sessions covered by IP, regulated data, or institutional policy that bars open release. Note: journals like Digital Discovery may not accept Restricted-only DAS deposits — confirm with the journal first.

Visibility metadata can be edited after publish in either direction (Public ↔ Restricted, embargo dates can be adjusted). Files themselves are immutable once published; only the access setting changes. Pick the right one at publish time — relying on post-publish edits as a recovery path is fragile.

## 2. Choose a license for the bundle

The deposit's license is **separate** from the license of the holotype tool itself (MIT). For the deposit, the common choices are:

- **CC0 (Public Domain Dedication)** — recommended for paper-citable archives. Removes all reuse friction. Most data-archive best-practice guides (Force11, Mozilla Open Leaders, NSF DMP guidance) point at CC0.
- **CC-BY 4.0** — same as CC0 in practice for paper supplementary, with an attribution requirement. Acceptable if your institution prefers it.
- **All Rights Reserved** — only if Restricted access; never use this for an Open paper-citation deposit (defeats the purpose).

LLM-generated content sits in a not-fully-settled copyright space; CC0 sidesteps the question entirely.

## 3. Upload via the web UI

This is the most common path; the API is covered separately below.

1. Sign in at <https://zenodo.org>.
2. Click **New Upload**.
3. **Upload type**: select `Dataset` (paper-supporting archives) or `Other` if Zenodo's options don't fit. `Software` is for the tool itself, not the data it produced.
4. **Files**: drag in either the tarball + its `.sha256` sidecar, OR the unpacked bundle's contents (the UI supports multi-file drag-and-drop). The tarball is strongly recommended anyway: one DOI-referenced artifact rather than N files, and the sidecar gives the reviewer a single hash to verify.
5. **Communities** (optional): if the paper's journal has a Zenodo community (Digital Discovery, Journal of Open Source Software, etc.), add it now — the deposit will appear in their listing once approved.
6. **Basic information**:
   - **Title**: `[Paper title] — LLM session transcripts (holotype archive)`
   - **Authors**: yourself + co-authors who participated in the LLM-driven experiments. Link each to their ORCID if possible.
   - **Description** (Markdown supported):
     - One paragraph: what this deposit is, what paper it supports, how to verify.
     - Reference `VERIFY.md` inside the bundle for the standalone verification procedure.
     - Note the manifest schema version and the holotype version that produced the bundle (visible in `BUNDLE_MANIFEST.json`).
   - **Keywords**: `LLM`, `Claude Code` / `Codex` / `Antigravity` (whichever applies), `provenance`, `forensic archive`, plus any paper-specific terms.
   - **Publication date**: today, or the paper's acceptance date if known.
   - **License**: as chosen above.
   - **Access**: as chosen above.
7. **Related/alternate identifiers**: add the paper's DOI here (relation: `is supplement to`) once known. If unknown, you can add it later via versioning.
8. **Funding** (optional but recommended for scientific archives): link to the relevant grant.
9. Click **Preview**, sanity-check, then **Publish**.

Once published the DOI is **immutable** — files cannot be changed, only superseded by new versions (see §6).

## 4. Upload via the Zenodo API (for scripted workflows)

If you publish bundles regularly, the API removes UI friction.

> **Note on API versions**: Zenodo migrated to the InvenioRDM platform in September 2023. The recipe below uses the **legacy `/api/deposit/depositions` API**, which still functions as of mid-2026 but is officially deprecated. The recipe is retained here because it's shorter and well-documented; for new long-lived integrations, prefer the InvenioRDM-native `POST /api/records` flow ([InvenioRDM REST API reference](https://inveniordm.docs.cern.ch/reference/rest_api_drafts_records/)). If you're scripting this once for a single paper deposit, the legacy API below is fine.

```bash
# 1. Create a personal access token at https://zenodo.org/account/settings/applications/tokens/new/
#    Scopes: deposit:write, deposit:actions
export ZENODO_TOKEN=...

# 2. Create a deposition
curl -s -X POST -H "Authorization: Bearer $ZENODO_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' https://zenodo.org/api/deposit/depositions > deposition.json
DEPOSITION_ID=$(jq -r .id deposition.json)
BUCKET_URL=$(jq -r .links.bucket deposition.json)

# 3. Upload the tarball + sidecar
curl -s -X PUT -H "Authorization: Bearer $ZENODO_TOKEN" \
  --data-binary @zenodo-deposit.tar.gz \
  "$BUCKET_URL/zenodo-deposit.tar.gz"
curl -s -X PUT -H "Authorization: Bearer $ZENODO_TOKEN" \
  --data-binary @zenodo-deposit.tar.gz.sha256 \
  "$BUCKET_URL/zenodo-deposit.tar.gz.sha256"

# 4. Set metadata
curl -s -X PUT -H "Authorization: Bearer $ZENODO_TOKEN" \
  -H "Content-Type: application/json" \
  -d @metadata.json \
  "https://zenodo.org/api/deposit/depositions/$DEPOSITION_ID"

# 5. Publish (mints the DOI)
curl -s -X POST -H "Authorization: Bearer $ZENODO_TOKEN" \
  "https://zenodo.org/api/deposit/depositions/$DEPOSITION_ID/actions/publish"
```

A `metadata.json` template is included as [`zenodo_metadata_template.json`](zenodo_metadata_template.json) — copy, edit, post.

**Two caveats before you use the template:**

1. **It targets the legacy API.** Field names like `upload_type`, `access_right`, and the flat `related_identifiers[].resource_type` reflect the legacy `/api/deposit/depositions` schema. For the InvenioRDM-native `/api/records` flow the field names differ: `resource_type` replaces `upload_type` (with a different controlled vocabulary), and `access` becomes a nested `{record, files}` block instead of the flat `access_right` string. The legacy schema is shown here because the curl recipe above uses the legacy endpoint.

2. **The `communities` entry assumes a `digital-discovery` community ID exists** on Zenodo. **Verify by loading <https://zenodo.org/communities/digital-discovery> before submitting.** If it doesn't resolve, either drop the `communities` block from your metadata or contact the Royal Society of Chemistry's Digital Discovery editorial office for the canonical community identifier. Communities can also be requested in the Zenodo UI as a separate step after publish.

## 5. Get the DOI; cite in the paper

After publishing, the deposit has two DOIs:

- A **version DOI** for *this specific* deposit (e.g. `10.5281/zenodo.1234568`)
- A **concept DOI** for *the deposit lineage* — always resolves to the newest version (e.g. `10.5281/zenodo.1234567`)

For a paper's Data Availability Statement, **cite the version DOI**, not the concept DOI. The concept DOI is a moving target; a reviewer following the citation should see the exact bytes the paper relied on.

### Sample DAS paragraph

For Digital Discovery (adapt the wording for other journals):

> *All LLM sessions referenced in this work are archived in a holotype bundle at https://doi.org/[VERSION DOI]. The bundle contains the verbatim host-CLI JSONL transcripts, a per-session SHA-256 manifest, and a standalone verification procedure (`VERIFY.md`) that requires only stock Unix tools (`shasum`, `jq`). Reviewers can verify any cited session by recomputing its SHA-256 from `transcript.jsonl` and comparing against the manifest's `sha256` field. The bundle's master tarball SHA-256 is provided in the accompanying `.sha256` sidecar. The holotype tool that produced this archive is open-source under the MIT license at https://github.com/PEEKPerformer/holotype.*

Individual sessions inside the bundle can be cited by their session ID + SHA-256 short hash, e.g.:

> *Decision X is supported by holotype session `3f1c4cf7` (SHA-256: `a8b2…f0`) within the bundle at [DOI].*

## 6. Versioning: publishing v2, v3, …

Zenodo's versioning model is non-destructive: publishing a new version mints a new version DOI under the same concept DOI. The old version stays accessible forever at its own DOI; the concept DOI starts pointing at the new version.

When to mint a new version:

- **Adding sessions** to the bundle (paper revisions added new experiments).
- **Discovering an error** in a session — though you can't remove the old one, you can publish a v2 that excludes the corrupted session and add a note in the new version's description explaining the exclusion.
- **Improving documentation** (`VERIFY.md` updates, expanded `BUNDLE_MANIFEST.json` annotations).

When NOT to mint a new version:

- Small typo in the description — just **edit metadata** without versioning. The DOI is unchanged.
- License change — should be rare; if needed, publish a new version with a note.

### How to mint a new version

In the web UI, navigate to the published deposit, click **New version**. Zenodo clones the deposition; replace files / edit metadata; **Publish** to mint the new version DOI.

The paper's DAS, once published, points at the original version DOI. New versions don't retroactively appear in the paper. That's by design — readers see what the authors saw at submission, plus a concept-DOI breadcrumb to find later versions if they want.

## 7. Pre-publish checklist

Before clicking Publish, verify:

| Check | How |
|-------|-----|
| All cited sessions are in the bundle | `jq '.sessions | length' BUNDLE_MANIFEST.json` matches the paper's session count |
| Bundle's tarball hash matches the sidecar | `shasum -a 256 -c zenodo-deposit.tar.gz.sha256` returns OK |
| Each session's `transcript.jsonl` hashes to its `manifest.json:sha256` | Walk the bundle and recompute (the bundle's own `VERIFY.md` has the loop) |
| Access mode + license + embargo date are correct | Re-check on the Zenodo Preview page |
| Title + description name the supported paper | Easy to forget on a multi-deposit day |
| ORCID is linked + visible on your Zenodo profile | <https://zenodo.org/account/settings/profile/> |
| Related identifiers include the paper's DOI (if known) | Or plan to add via "Edit metadata" once the paper DOI is minted |

## 8. Gotchas

- **File-count limits**: Zenodo accepts up to **100 files per deposit** in the web UI; the API has no hard limit but performance degrades past a few hundred. Prefer the single-tarball deposit (one file, plus the sidecar).
- **Deposit size limit**: 50 GB per record is the default. An additional 150 GB pool is available across all your records, and one-time per-record increases up to 200 GB can be requested from Zenodo support ([quota docs](https://help.zenodo.org/docs/deposit/manage-quota/)). Encrypted holotype bundles compressed by zstd are typically well under the 50 GB default.
- **Filename normalization**: Zenodo doesn't rename your files but URL-encodes them in download links. Avoid spaces or special characters in the tarball filename.
- **Embargo end-date editing**: embargo dates are part of editable metadata; they can be shortened or extended after publish via the standard edit-metadata flow. Best practice when extending: add a note to the description explaining why, so the public record of the change is on the deposit itself rather than only in your inbox.
- **No deletion of published deposits**: Zenodo deposits are permanent by policy. If you discover that an unintentional credential leaked into a deposited transcript, contact Zenodo's GDPR/legal team — they have a documented process for serious cases but treat it as a last resort.
- **Encrypted-bundle considerations**: if your holotype archive is encrypted (git-crypt) and the paper bundle was extracted from it, the bundle itself is **plaintext** (paper_bundle.py decompresses + decrypts on extraction). Don't accidentally upload the encrypted `.jsonl.zst` files — that would defeat the verification path. Confirm: `file zenodo-deposit/*/transcript.jsonl` should report ASCII text, not zstd data.

## 9. After publication

- **Update the paper's DAS** with the version DOI.
- **Save the deposit's reservation link** (Zenodo emails this on publish) to your password manager — it's how you'll edit metadata or mint a new version later.
- **Add the deposit to your ORCID profile** automatically by linking ORCID under Zenodo's account settings; future deposits then populate your ORCID without manual entry.
- **Track citations**: Zenodo populates the deposit page's citation count from DataCite Event Data and Crossref Event Data (a joint Crossref/DataCite service that polls indexed publications for citation links). Counts appear once the citing work is itself indexed in one of those sources — typically days to weeks after the citing paper is published.

## When NOT to use Zenodo

Zenodo is the right call for paper-supporting archives in chemistry, materials science, ML, and most natural-science fields. It may be the wrong call when:

- Your institution mandates a specific archive (e.g. NIH Data Archive, EBI BioStudies). Use the mandated one; you can still cite holotype's bundle format inside their deposit.
- The data is genuinely too sensitive even for Restricted Zenodo access. Use a controlled-access repository (dbGaP, EGA) instead.
- The data volume exceeds Zenodo's 50 GB quota and you don't want to request an exception. Consider Open Science Framework (OSF) or your institutional repository.

For each of these, the holotype bundle's verification model (per-session SHA-256, stock-Unix-tool `VERIFY.md`) still applies — only the hosting platform changes.

---

## Quick-reference: minimal deposit workflow

```bash
# 1. Generate the bundle (see README tutorial for full context)
python scripts/paper_bundle.py \
    --sessions a,b,c --out ./zenodo-deposit/ \
    --paper-title "..." --paper-doi "..." \
    --tarball

# 2. Sanity-check the tarball
shasum -a 256 -c zenodo-deposit.tar.gz.sha256

# 3. Upload via web UI at https://zenodo.org/uploads/new
#    - Files: zenodo-deposit.tar.gz + zenodo-deposit.tar.gz.sha256
#    - Title, authors (ORCID-linked), description, license (CC0), access (Open)
#    - Publish → get DOI

# 4. Paste version DOI into paper's DAS
```
