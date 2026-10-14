# Fix: artifact maintenance deletes the STEP model of a released revision

Status: draft for maintainer discussion. PRs: #684 (keep released files), #691 (purge ledger, step 9), #696 (per-sha256 lock), #688 (check and restore script), #687 (health count), #685 (audit panel). The grace period, quarantine and `--record-legacy` are not implemented yet; see section 11 of the main plan. Implements item 1 ("Purge") of
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

Code (line numbers at `89a8a111`, the current `upstream/dev` tip):

| Step | Location |
|---|---|
| Purge keeps only head models; everything else is "superseded" | `backend/app/services/catalog/asset_files.py:121-170` (`current_models` CTE at 130-136, unlink at 167) |
| Any asset/metadata edit clones and moves the head | `backend/app/services/catalog/revision_kernel.py:330-333` (`clone_revision`) |
| Released pointer moves only on release | `backend/app/services/catalog/release_workflow.py:171-174`, trusted import `:284-287` |
| Attaching a model is additive; detaching removes it from the draft | `backend/app/services/catalog/asset_links.py:167-255`, `asset_imports.py:338-378`, `detach_asset_by_id` in `asset_imports.py` (UI: `frontend/src/components/workspace/library-component-workspace.tsx:919-923`) |
| Placement reads the released revision's files | `component_catalog_domain.py:1134-1152` (`released_only=True`) → `catalog/placement.py:122-125` → `catalog/placement_payloads.py:180-181` (`path.read_bytes()`) |
| Audit verification re-hashes every revision's asset files | `catalog/component_history.py:169-190` (`asset_content_mismatch`) |
| Scheduled daily, on by default | `backend/app/prism_worker.py:482-496`, `CATALOG_RETENTION_ENABLED` default `True` (`app/core/config.py:579`); call site `catalog_worker_tasks.py:200-201` |

Reproduction: `backend/tests/test_catalog_step_purge.py` (on this branch)
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
trigger (`catalog/postgres_integrity.py:137`). `release_status = 'released'`
stays set on superseded revisions, but archiving rewrites it, so it is not
enough on its own.

## Options

### A. Stop purging catalog STEP files

Remove the purge from artifact maintenance and delete
`purge_superseded_step_files`. This is the smallest change and needs no
schema change. But every STEP ever attached, including models that were only
on abandoned drafts, stays on disk forever. Git library sources (section 9)
make that set grow: every import that replaces a model and is then reworked
leaves a model nobody can ever place.

### B. Purge only models of abandoned, never-released drafts (recommended)

Keep a purge, but make it safe and visible:

- delete a model only when no revision that **could still be read for a
  release or placement** links it;
- record every purge;
- teach the audit check and the readers what a recorded purge means.

### C. Keep any model a project uses, via `component_usage`

`component_usage` matches by MPN and project Git revision, and records no
catalog revision (section 9, "No record of which catalog revision a project
uses"). It cannot tell which revision's model a project needs, so it adds
nothing over B. It becomes useful after section 9 item 3 (`Prism_Revision`).

**Recommendation: B.** A remains the fallback if maintainers do not want to
change the audit check (open question 1).

## Retention rule (option B)

A STEP asset's file may be removed only when **every** revision that links
it, in **any** component, meets all of these:

1. it is not that component's `current_revision_id` (the head: an
   in-progress draft, or the released revision when no draft exists);
2. it is not that component's `released_revision_id`;
3. it has no `component_release_records` row (written by both release
   paths and append-only, so it survives archiving), and its
   `release_status` is not `released` (covers legacy rows that predate
   release records);
4. it was created more than `CATALOG_DRAFT_MODEL_RETENTION_DAYS` ago (new
   setting, default 30; `0` disables the purge). This grace period gives
   authors time to undo a detach, and keeps the model of a Git import that
   is being reworked.

Rule 1 only matters for the head; no non-head revision can become the head
again (there is no revert). So once rules 1–3 hold, the bytes can only be
reached through history and the audit check.

How the rule handles each case:

| Case | Kept by |
|---|---|
| Released revision while a draft exists (the bug) | rules 2 and 3 |
| Earlier released revision, superseded by a newer release | rule 3 |
| Released revision later archived | rule 3 (release record) |
| Retired part (`is_active = 0`) | same as an active part; deactivation does not make anything purgeable |
| Draft in progress | rule 1 |
| Model shared by two components | the check is per asset across all components |
| Abandoned draft model, older than the grace period | **purged and recorded** |

Candidate query, run as a snapshot. Each candidate is checked again under a
lock before its file is moved:

```sql
SELECT asset.id, asset.sha256, asset.canonical_path
FROM assets asset
WHERE asset.asset_type = '3dmodel'
  AND (lower(asset.canonical_path) LIKE '%.step' OR lower(asset.canonical_path) LIKE '%.stp')
  AND EXISTS (SELECT 1 FROM revision_assets link WHERE link.asset_id = asset.id)
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
        OR revision.created_at >= %(cutoff)s
        OR EXISTS (SELECT 1 FROM component_release_records record
                   WHERE record.revision_id = revision.id)
      )
  )
  AND NOT EXISTS (
    SELECT 1 FROM catalog_asset_file_purges purge
    WHERE purge.asset_id = asset.id AND purge.restored_at IS NULL
  )
```

`idx_revision_assets_asset` (catalog migration 8) already serves the
asset → revision lookup.

## Changes (option B)

### 1. Purge ledger: catalog migration step 9

`catalog_schema_migrations.py` gets step `(9, "asset_file_purges", ...)`:

```sql
CREATE TABLE IF NOT EXISTS catalog_asset_file_purges (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES assets(id),
    sha256 TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    quarantine_path TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,          -- 'abandoned_draft' | 'legacy_purge'
    job_id TEXT NOT NULL DEFAULT '',
    purged_at TEXT NOT NULL,
    restored_at TEXT               -- set once when the bytes come back
);
CREATE INDEX IF NOT EXISTS idx_asset_file_purges_asset ON catalog_asset_file_purges(asset_id);
```

`catalog/postgres_integrity.py:134-142` guards it against `DELETE`. Updates
are allowed only to set `restored_at` once, by a trigger like the one on
`assets` (`:214`). The table stays an audit record: a restore is never
written by deleting a row.

### 2. A narrow retention collaborator

New `backend/app/services/catalog/model_retention.py` (persistence layer)
replaces `CatalogAssetFiles.purge_superseded_step_files`. It owns one
transaction per candidate and takes a connection factory, as
`metadata_batch_lifecycle.py` does. It is exposed as
`catalog_service.model_retention`. The facade forwarder
`component_catalog_domain.py:462-466` is removed, so the facade does not
grow (1,222-line ceiling in `scripts/catalog_architecture_baseline.json`).

For each candidate:

1. take `pg_advisory_xact_lock(hashtext('catalog-asset:' || sha256))`;
2. evaluate the rule again for that one asset id; if it no longer holds,
   skip it;
3. resolve the path: it must be inside `store_root` and have a STEP suffix,
   as today (`asset_files.py:160-166`);
4. `os.replace` the file to `store_root/purged/<run>/<sha256>/<name>`;
5. insert the ledger row (`reason='abandoned_draft'`, quarantine path,
   job id);
6. commit. If anything fails before the commit, move the file back and roll
   back.

A second pass deletes `store_root/purged/<run>/` directories older than 14
days, the same window the object store uses for quarantine
(`local_artifact_store.py:441-469`). Until then, an operator can restore
from quarantine.

`catalog_worker_tasks.py:200-212` calls
`catalog_service.model_retention.purge_abandoned_draft_models(job_id=...)`.
The result key becomes `"abandoned_draft_models_purged"`; see open
question 4 for `superseded_steps_purged`.

### 3. Close the re-link race

A model on an abandoned draft can be linked again while the purge runs:

- re-uploading the same bytes finds the same asset row by path or signature
  (`asset_registry.py:62-120`);
- a project import links an existing asset
  (`project_import_acceptance.py:239-242`).

The file is written before any lock is taken (`asset_imports.py:355-366`),
so locking the component is not enough. Every path that adds a
`revision_assets` row for a `3dmodel` therefore takes the same advisory
lock on the asset's sha256 and checks, under that lock, that the file exists
and matches. If it does not, it raises
`CatalogConflict(code="asset_bytes_missing")`, which is retryable:
re-uploading writes the file again, and `register_asset` repairs the row.
The paths are:

- `CatalogAssetLinks.link_asset_to_revision` (`asset_links.py`), used by
  attach, project import and System Builder;
- the direct insert in `library_import_writer.py:259-266`.

`clone_revision` copies links from the head, and the head is never a
candidate, so it needs no lock.

Ordering under the lock:

- **link first:** the link commits, and the purge's re-check skips the
  asset;
- **purge first:** the file is quarantined and committed, and the linker
  sees a missing file and refuses. It never links missing bytes.

### 4. Audit check treats a recorded purge as expected

`catalog/component_history.py:169-190` changes. When an asset file is
missing:

- **valid:** the asset has an open ledger row (`restored_at IS NULL`), *and*
  no revision of this component that links it is released or
  release-recorded. The asset id goes into a new response key,
  `purged_asset_ids`.
- **invalid, as today:** everything else
  (`reason='asset_content_mismatch'`).

A file that is present but has the wrong hash is always invalid. A ledger
row can never excuse a lost released model, because rule 3 never produced
one. The new key is additive: `CatalogAuditVerification` in `frontend/src/types/catalog.ts:287-293` gains it, and
the evidence panel shows "N model files purged by retention".

### 5. Readers return a typed error, not a 500

`catalog_asset_source` (`component_catalog_domain.py:259-270`) and
`get_asset_by_id` reach `placement_payloads.py:180` (`read_bytes`). For a
missing file with an open ledger row they raise a typed `NotFound`
(`code="asset_purged"`, `core/errors.py:101`); other missing files stay
errors. Placement of a released revision cannot hit this, by rule 2.

### 6. Detect and repair files the old rule already deleted

Script `scripts/check_catalog_asset_files.py` uses the new collaborator
through public service APIs. For each linked `assets` row it checks that
the file exists and its sha256 matches, and reports it by impact:

- **placement broken:** linked to a `released_revision_id`;
- **history broken:** linked to a released or release-recorded revision;
- **draft-only:** everything else.

It exits non-zero when the first group is not empty.

`--restore` (dry run by default) looks for bytes with the same sha256, in
this order:

1. the purge quarantine (`store_root/purged/`);
2. the object store: `catalog_artifacts` by sha256; `available` and
   `archived` go through `LocalArtifactStore.ensure_available`, and
   `quarantined` is read from `object_path`;
3. project-harvest staging, `store_root/imports/<session>/`;
4. `--search DIR` directories: volume backups, library checkouts, users'
   `RemoteLibrary/remote_3d/`. Placement passes model bytes through
   unchanged, so those copies hash-match.

It writes only on an exact sha256 match, to the row's `canonical_path`, and
sets `restored_at` on any open ledger row.

`--record-legacy` writes `reason='legacy_purge'` ledger rows for missing
**draft-only** files that the old rule deleted and that cannot be restored.
Their components then verify as valid again. It refuses any asset in the
other two groups.

### 7. Docs

- `docs/releases/<next tag>.md`: the data-loss window, the new setting, and
  the detection/repair command.
- `docs/UPGRADES.md`: migration step 9 and running the check after
  upgrading.
- `.env.example`: `CATALOG_DRAFT_MODEL_RETENTION_DAYS`.

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

Lost released models must be restored or reported to the owners of the
affected parts. Lost draft-only models can be recorded as `legacy_purge`.

## Tests

PostgreSQL tests through public service APIs, extending
`backend/tests/test_catalog_step_purge.py` (it fails on `dev` today, as
shown above). Each case runs the purge with a zero grace period unless it
says otherwise.

1. Released rev, then a draft that replaces the model: the released STEP is
   kept, `build_inline_bundle` succeeds, the audit chain is valid.
2. rev1 released, rev2 with a new model released: rev1's STEP is kept.
3. A released revision archived: its STEP is kept (release record).
4. Abandoned draft model (attach, detach, older than the grace period):
   - its file moves to quarantine and one ledger row is written;
   - the audit chain is valid with `purged_asset_ids == [id]`;
   - `get_asset_by_id` raises `asset_purged`.
5. Grace period: the same draft model newer than the cutoff is kept.
6. A model shared by two components, released in one and abandoned in the
   other: kept.
7. Retired part with an abandoned draft model: purged, same as an active
   part.
8. Re-link race:
   - a second connection holds the asset lock and links the asset; the
     purge skips it;
   - a purge commits first; then linking the asset raises
     `asset_bytes_missing`, and re-uploading the same bytes succeeds and
     restores the file.
9. Rollback: the ledger insert fails, and the file is moved back.
10. Audit check: a missing file **without** a ledger row is still invalid,
    and a ledger row on a released model does not make it valid.
11. Migration: step 9 applies on a database at step 8, and the integrity
    trigger refuses `DELETE` and a second `restored_at` update.
12. Detection and repair script:
    - each impact group;
    - restore from quarantine, from the object store, and from `--search`;
    - refusal on a sha256 mismatch;
    - `--record-legacy` refuses released assets;
    - a dry run writes nothing.

Gate:

- the `prism-catalog-change` quick checks, including
  `check_catalog_architecture.py`, since a module and an exposed attribute
  are new;
- `python3 scripts/check_module_size.py`;
- the full backend suite;
- the dedicated PostgreSQL integration gate with zero skips;
- the frontend type check and tests for the evidence-panel change.

## Catalog contracts touched

| Contract | Effect |
|---|---|
| Component payload, revision manifest hashes, placement manifests, inline bundles, signed URLs | unchanged; they work again for affected parts |
| Audit-chain verification | **changed**: a recorded purge of a never-released model is valid; the response gains `purged_asset_ids` (additive) |
| Asset reads | missing and recorded → typed `asset_purged` instead of a 500 |
| Linking a `3dmodel` | can refuse with `asset_bytes_missing` (new, retryable) |
| Schema | catalog migration step 9; one append-only table plus its integrity trigger |
| Settings | `CATALOG_DRAFT_MODEL_RETENTION_DAYS` (default 30, `0` disables) |
| `artifact_maintenance` job result | `superseded_steps_purged` is replaced by `abandoned_draft_models_purged` |
| Facade surface | one forwarder replaced by the `model_retention` attribute; ceiling unchanged or lower |
| DBL export, CSV, imports | untouched apart from the link-time check |

## Risks

- **Advisory-lock coverage.** A future path that inserts `revision_assets`
  without `link_asset_to_revision` would bypass the race guard. Mitigation:
  an architecture-test assertion that only `asset_links.py`,
  `revision_kernel.py` (clone) and `library_import_writer.py` write
  `revision_assets`.
- **Audit semantics change.** Reviewers must accept that "valid" now allows
  recorded, release-free gaps. The ledger cannot excuse a released model,
  and it can only be appended to.
- **Repair restores wrong bytes.** Prevented by an exact sha256 match and a
  dry-run default.
- **Ordering with other section 9 work.** The `--overwrite-assets` and
  DBL-export fixes
  ([fix-dbl-export-models-and-versioning.md](fix-dbl-export-models-and-versioning.md))
  assume old released bytes stay on disk. This fix should land first.
- **Hidden purge elsewhere.** `run_retention` and
  `collect_unreferenced_objects` act on the object store, not on
  `store_root/3dmodels`. This was checked by reading the code and should be
  confirmed in review.

## Not in this change

- `catalog_health` counts `missing_files` from the head's availability
  (`catalog/health.py:42-61`), so it would not have seen this loss.
- System Builder resolves models from `current_revision_id`
  (`catalog/models.py:132-146`), so a pinned `catalog_revision_id` renders
  the head's model.
- The evidence panel shows "First invalid event:" with an empty id when the
  reason is `asset_content_mismatch`
  (`library-component-evidence-panels.tsx:729`).
- Pinning models in projects and recording `Prism_Revision` (section 9,
  items 2 and 3).
- Purging symbols, footprints or SPICE models (they are small; not
  proposed).

## Open questions for maintainers

1. **Audit semantics.** Is it acceptable for `verify_component_audit_chain`
   to stay valid over a *recorded* purge of a model that was never
   released? If not, use option A (no purge) instead.
2. **Grace period.** Is 30 days the right default for
   `CATALOG_DRAFT_MODEL_RETENTION_DAYS`, and should it be counted from the
   revision's creation (proposed) or from the day the model was detached?
3. **Repair delivery.** Ship detection and repair as a supported script
   with release notes (proposed), or only document manual recovery? Should
   detection also run inside `artifact_maintenance` and flag
   released-revision loss in `catalog_health`?
4. `superseded_steps_purged` in the job result: rename it (proposed), or
   keep it for one release next to the new key?
5. Should this ship in `v4.0.0-alpha` as a data-loss fix, ahead of the rest
   of section 9?
