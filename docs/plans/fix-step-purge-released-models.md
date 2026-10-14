# Fix: artifact maintenance deletes the STEP model of a released revision

Status: draft for maintainer discussion. Implements item 1 ("Purge") of
section 9 of [catalog-git-library-sources.md](catalog-git-library-sources.md)
("Keeping existing projects on the library version they used").

## Problem

The daily catalog maintenance job deletes STEP files that a released revision
still uses. After that, placing the part from KiCad fails, and the
component's audit-chain check reports it as tampered.

`CatalogAssetFiles.purge_superseded_step_files` decides what to keep from
`components.current_revision_id` only. That pointer means "head of the
component", not "what placement serves":

- Any edit to a released component clones a new revision and moves
  `current_revision_id` to it right away.
- `released_revision_id` moves only when that draft is released.

So once a draft drops the released revision's model (Detach in the
component workspace, or replace by detach and attach), the next maintenance
run deletes the released bytes. The draft does not need to be released, or
even reviewed.

The function's docstring says it removes a file only when "no component
currently uses the old asset". The query actually tests "no component's
*head* links the asset", which is a different thing.

Even the purges the function was meant to make break a stated invariant.
After a later release, the STEP of the previous released revision is
deleted on purpose. `verify_component_audit_chain` then fails for that
component, because it re-hashes the file of every revision's asset.

## Evidence

Code (line numbers at `89a8a111`):

| Step | Location |
|---|---|
| Purge keeps only head models; everything else is "superseded" | `backend/app/services/catalog/asset_files.py:121-170` (`current_models` CTE at 130-136, unlink at 167) |
| Any asset/metadata edit clones and moves the head | `backend/app/services/catalog/revision_kernel.py:330-333` (`clone_revision`) |
| Released pointer moves only on release | `backend/app/services/catalog/release_workflow.py:171-174`, trusted import `:284-287` |
| Attaching a model is additive; detaching removes it from the draft | `backend/app/services/catalog/asset_links.py:167-255`, `asset_imports.py:338-378`, `detach_asset_by_id` in `asset_imports.py` (UI: `frontend/src/components/workspace/library-component-workspace.tsx:919-923`) |
| Placement reads the released revision's files | `component_catalog_domain.py:1134-1152` (`released_only=True`) → `catalog/placement.py:122-125` → `catalog/placement_payloads.py:180-181` (`path.read_bytes()`) |
| Audit verification re-hashes every revision's asset files | `catalog/component_history.py:169-190` (`asset_content_mismatch`) |
| Scheduled daily, on by default | `backend/app/prism_worker.py:482-496`, `CATALOG_RETENTION_ENABLED` default `True` (`app/core/config.py:579`); call site `catalog_worker_tasks.py:200-201` |

Reproduction: `backend/tests/test_catalog_step_purge.py` (uncommitted)
against PostgreSQL 16, using the scratch-database setup the catalog suites
use.

1. Create a part, import a symbol and a footprint, and attach `R_Purge.step`
   (rev A). Take it through in_progress → qa_review → done (QA) → released.
2. Attach a different `R_Purge.step` (rev B), then detach rev A's model by
   asset id. The head is now a draft without rev A. `released_only` still
   returns the released revision.
3. Call `purge_superseded_step_files()`.

Result on unmodified code:

```
AssertionError: '159d6be5-…' unexpectedly found in ['159d6be5-…']   # released model purged
```

A follow-up run of the same scenario showed both effects:

```
build_inline_bundle → FileNotFoundError: …/components/3dmodels/Prism_Purge/R_Purge.step
verify_component_audit_chain: before purge valid=True, after purge valid=False reason=asset_content_mismatch
```

A "replace model" through a Git or project import, as proposed in section 9,
would trigger the same thing on every run.

## What must be kept

| Case | Who still reads the STEP | Current rule |
|---|---|---|
| Released revision while a draft exists | placement, signed asset URLs, remote provider | **deleted** (bug) |
| Earlier released revision, superseded by a newer release | audit verification; `get_asset_by_id(revision_id=…)`; projects designed at that revision (section 9); System Builder pins (`catalog_revision_id`) | deleted by design |
| Released revision later archived (`released_revision_id` cleared, `release_workflow.py:175-179`) | audit verification, history | deleted by design |
| Retired part (`deactivate_component`, `is_active = 0`) | audit verification, history | kept while it is the head; otherwise as above |
| Draft in progress (head) | authoring, previews, GLB conversion (`catalog/models.py:132`) | kept |
| Old draft, never released and no longer the head | audit verification only. No revert exists, so it can never be released. | deleted |

There is one durable record of "this revision was ever released":
`component_release_records`. Both release paths write it
(`release_workflow.py:216`, `:290`), and it is append-only, enforced by a
trigger (`catalog_schema_migrations.py:137`). `release_status = 'released'`
stays set on superseded revisions, but archiving rewrites it, so it is not
enough on its own.

## Options

### A. Stop purging catalog STEP files (recommended)

Remove the purge from artifact maintenance, and remove
`purge_superseded_step_files` itself.

- Restores the invariant that every `assets` row has its bytes. Audit
  verification and every revision's placement payload work again.
- Covers all four rows that are now unsafe, with no new rule to get wrong.
- The disk it gave back is mostly what the narrow rule (B) would have to keep
  anyway: models of released history. B would reclaim only abandoned draft
  models.
- No schema change.

### B. Purge only models of abandoned, never-released drafts

Keep the job, but delete an asset's file only when **every** revision that
links it:

- has no `component_release_records` row,
- has never been `released_revision_id` (implied by the above, but cheap to
  check), and
- is not any component's `current_revision_id`.

```sql
SELECT DISTINCT asset.id, asset.canonical_path
FROM assets asset
JOIN revision_assets link ON link.asset_id = asset.id AND link.asset_type = '3dmodel'
WHERE (lower(asset.canonical_path) LIKE '%.step' OR lower(asset.canonical_path) LIKE '%.stp')
  AND NOT EXISTS (
    SELECT 1
    FROM revision_assets keep
    JOIN component_revisions revision ON revision.id = keep.revision_id
    JOIN components component ON component.id = revision.component_id
    WHERE keep.asset_id = asset.id
      AND (
        revision.id = component.current_revision_id
        OR revision.id = component.released_revision_id
        OR revision.release_status = 'released'
        OR EXISTS (SELECT 1 FROM component_release_records record
                   WHERE record.revision_id = revision.id)
      )
  )
```

The check is per asset, not per component, so an asset shared by two
components is kept if either one needs it. On its own, B still breaks audit
verification for every purged draft model. To be consistent it also needs:

- a new catalog migration step with an append-only
  `catalog_asset_file_purges(asset_id, sha256, purged_at, job_id, reason)`
  table, written in the same transaction as the decision, with the unlink
  after commit;
- `verify_component_audit_chain` to report a recorded purge of a
  never-released asset as an expected state rather than
  `asset_content_mismatch`. That changes a contract the catalog skill names.

### C. Keep any model of a released or recorded revision, using usage too

`component_usage` matches by MPN and project Git revision, and records no
catalog revision (section 9, "No record of which catalog revision a project
uses"). So it cannot tell which revision's model a project needs, and adds
nothing over B. It becomes useful only after section 9 item 3
(`Prism_Revision`).

**Recommendation: A in this fix.** If disk use from abandoned drafts turns
out to matter, B can follow as its own change, with the purge ledger and the
verifier change.

## Changes (option A)

1. `backend/app/services/catalog_worker_tasks.py`: remove the "Purging
   superseded STEP payloads" step (`:200-201`). Progress steps stay
   monotonic. Keep `"superseded_steps_purged": 0` in the result for one
   release so result readers and dashboards do not break, or drop it. See
   open question 4. No test or frontend code reads the key today.
2. `backend/app/services/catalog/asset_files.py`: delete
   `purge_superseded_step_files` and `STEP_SUFFIXES` if nothing else uses it.
   Also delete the forwarder `component_catalog_domain.py:462-466`. That
   only shrinks the grandfathered facade, so lower its 1,222-line ceiling in
   `scripts/catalog_architecture_baseline.json`.
3. Read-only detection (`scripts/check_catalog_asset_files.py`, using the
   catalog's public `connection()` and `runtime`, or a small
   `catalog/asset_file_audit.py` collaborator). For each `assets` row linked
   to a revision, check that the file exists and its sha256 matches. Report
   it, grouped by impact:
   - **placement broken**: linked to some `released_revision_id`;
   - **history broken**: linked to a revision with a release record;
   - **draft-only**: everything else.

   Exit non-zero when the first group is not empty.
4. Repair (same script with `--restore`, dry run by default). For each
   missing file, look for bytes with the same sha256, in this order:
   1. the content-addressed object store: `catalog_artifacts` by `sha256`.
      `available` and `archived` rows go through
      `LocalArtifactStore.ensure_available`; `quarantined` rows can be read
      from `object_path` until `delete_expired_quarantine` runs. Folder
      imports and browser folder uploads land here (`artifact_kind='source'`,
      `library_folder_import_service.py:68`, `api/catalog_admin.py:380`);
   2. project-harvest staging, `store_root/imports/<session>/`, kept 30 days
      after the proposals are resolved (`catalog_worker_tasks.py:202-204`);
   3. directories the operator passes in (`--search DIR`, repeatable):
      backups of the catalog volume, original library checkouts, and users'
      `RemoteLibrary/remote_3d/`. Placement passes model bytes through
      unchanged, so those copies hash-match.

   Write a file only on an exact sha256 match. Write it to the row's
   existing `canonical_path`, so no `assets` update is needed (the integrity
   trigger would allow it anyway, `catalog/postgres_integrity.py:214`). Report
   anything still missing. No schema migration.
5. Docs: a `docs/releases/<next tag>.md` entry naming the data-loss window
   and the detection/repair command, and a line in `docs/UPGRADES.md`.

## Can already-purged files be restored?

Only from a copy kept outside the catalog store:

- **The canonical store has none.** `write_canonical_file` keeps an
  immutable copy under `revisions/<sha>/` only for the *newer* payload when
  a name collides. The purged file was the only copy.
- **Object store:** has it if the model arrived by folder import or folder
  upload, and catalog retention has not purged it yet. Retention drops
  snapshot references 30 days after review, quarantines them 30 days later,
  and deletes them 14 days after that (`local_artifact_store.py:461-469`).
  `catalog_artifacts.storage_state = 'purged'` records a confirmed loss.
- **Import staging:** project-harvest imports keep staged files for 30 days
  after resolution.
- **Outside Prism:** volume backups, the source library repository, and
  users' project checkouts (`RemoteLibrary/remote_3d/<basename>`, which is
  gitignored but usually present).
- **Not usable:** `catalog_model_glb` holds a GLB derived from the STEP,
  not the STEP itself.

Lost bytes for old drafts that were never released are acceptable to leave
missing: only the audit check sees them. Report them, but treat them as
low priority.

## Tests

- `backend/tests/test_catalog_step_purge.py` (written, uncommitted, fails
  today as shown above). Under option A it moves from calling the removed
  method to running `catalog_worker_tasks.run_artifact_maintenance` with a
  fake `JobContext`. The non-catalog collaborators (`artifact_store`,
  `job_artifacts`, `context_jobs`, `git_read_cache`) are patched, since none
  of them has a test seam today. The real PostgreSQL catalog service gets the
  released-plus-draft fixture. Assertions:
  - the released revision's STEP still exists;
  - `build_inline_bundle` succeeds and includes the model;
  - `verify_component_audit_chain` stays valid.
- A second case: rev1 released, rev2 with a new model released. After
  maintenance, rev1's STEP still exists and the audit chain is valid. This
  locks in the "released history" rows of the table.
- Detection/repair script: missing file in each impact group; restore from
  an object-store artifact, from a `--search` directory, refusal on sha256
  mismatch, and a dry run that writes nothing.
- If B is chosen instead: a released model shared by two components, the
  archived-after-release case, the current-head draft case, and the verifier
  treating a recorded purge as expected.

Gate: `prism-catalog-change` quick checks, the full backend suite, and the
dedicated PostgreSQL integration gate with zero skips.

## Catalog contracts touched

| Contract | Option A | Option B |
|---|---|---|
| Component payload, revision manifest hashes, placement manifests, inline bundles, signed URLs | unchanged (they work again for affected parts) | unchanged |
| Audit-chain validity | unchanged code; becomes true again for affected parts | **changed**: verifier learns a recorded-purge state |
| Schema | none | new catalog ledger step |
| `artifact_maintenance` job result | `superseded_steps_purged` removed or pinned to 0 | unchanged |
| Facade surface | shrinks by one forwarder (ceiling decreases) | unchanged |
| DBL export, CSV, imports | untouched | untouched |

## Risks

- **Disk growth (A).** Every STEP ever attached stays on disk, as symbols and
  footprints already do. The upper bound is the sum of all models ever
  uploaded. For abandoned drafts this is new; for released history it is the
  intended cost.
- **Repair restores wrong bytes.** Prevented by writing only on an exact
  sha256 match, with dry run as the default.
- **Ordering with other section 9 work.** The `--overwrite-assets` fix and
  the DBL-export fix ([fix-dbl-export-models-and-versioning.md](fix-dbl-export-models-and-versioning.md))
  both assume old released bytes stay on disk. This fix should land first.
- **Hidden purge elsewhere.** The other retention paths (`run_retention`,
  `collect_unreferenced_objects`) act on the object store, not on
  `store_root/3dmodels`. This was checked by reading the code and should be
  confirmed in review.

## Not in this change

- `catalog_health` counts `missing_files` from the head's availability
  (`catalog/health.py:42-61`), so it would not have seen this loss. Making it
  check the released revision too is a separate improvement.
- System Builder resolves models from `current_revision_id`
  (`catalog/models.py:132-146`), so a pinned `catalog_revision_id` renders
  the head's model. That is a separate issue.
- Pinning models in projects and recording `Prism_Revision` (section 9, items
  2 and 3).

## Open questions for maintainers

1. **Should the catalog purge STEP files at all?** The recommendation is A:
   stop. Choose B only if there is evidence that abandoned-draft models use
   meaningful disk.
2. **Audit semantics.** Is "every revision's asset bytes are present and
   hash-match" a hard invariant, as `verify_component_audit_chain` implies?
   If yes, B needs the purge ledger and verifier change. If no, the verifier
   should be relaxed explicitly, not by side effect.
3. **Repair delivery.** Ship detection and sha256-verified restore as a
   supported script with release notes, or only document manual recovery
   for operators? Should detection also run inside `artifact_maintenance`
   and flag released-revision loss in `catalog_health`?
4. `superseded_steps_purged` in the maintenance job result: drop it, or keep
   it pinned at 0 for one release?
5. Should this ship in `v4.0.0-alpha` as a data-loss fix, ahead of the rest
   of section 9?
