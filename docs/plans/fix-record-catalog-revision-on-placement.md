# Fix: record which catalog revision a KiCad project placed

Status: draft for maintainer discussion. PR: #695 covers section 1 only (fields on the placed symbol). Sections 2-8 (reader, migration step 10, scan job, backfill, API/UI, Release Studio) are still open. Part of phase 0b in
[catalog-git-library-sources.md](catalog-git-library-sources.md), section 9,
item 3 ("Record the revision"). The STEP purge fix (item 1), pinning 3D models
(item 2) and Release Studio pinning (item 4) are separate changes. This plan
only produces the data Release Studio could use.

## Problem

Prism cannot say which catalog revision a KiCad project uses. So it cannot
warn that a project is behind the library, and it cannot list the exact
catalog revisions in a release dossier. System Builder can already do this for
catalog instances.

### Evidence (current code)

**Placement stamps metadata but no identity.**

- `backend/app/services/catalog/placement_payloads.py:49-57`
  `symbol_metadata_fields` emits the built-in symbol labels
  (`metadata_descriptors.py:255-264`, `SYMBOL_METADATA_LABEL_TO_KEY` /
  `SYMBOL_METADATA_FIELD_ORDER`) plus every `extra_fields` key except
  `Reference`/`Footprint`. No component id, revision id or digest is written.
- `placement_payloads.py:99-143` `rewrite_symbol_payload` rebuilds the
  top-level property blocks: existing order, then the built-in order, then
  extras sorted, then `Footprint`. Hidden fields use
  `symbol_property_block(..., hidden=True)` (`:39-46`).
- `placement_payloads.py:174-205` `materialize_asset` is the only caller. It
  serves the manifest (`placement.py:74-113`), the inline bundle
  (`placement.py:115-149`) and the signed asset download
  (`placement.py:151-188`, from `remote_provider.py:389-416`, which always
  passes `rev`). All three receive a `component` payload that already carries
  `id`, `revision_id`, `revision` (version), `manifest_hash` and
  `released_revision_id` (`component_read_models.py`, around line 771).
  `build_manifest`/`build_inline_bundle` load it with `released_only=True`
  (`component_catalog_domain.py:1134-1152`), so the revision placed is always
  the released one.
- Byte stability is "same inputs give same bytes", not a frozen fixture:
  `backend/tests/test_catalog_placement_payloads.py:48-72` checks property
  order and that a second rewrite is identical, and
  `test_component_catalog_postgres_integration.py:1072-1090` checks that two
  inline bundles are identical and the checksums match. No test stores golden
  hashes. The revision manifest hash (`revision_kernel.py:126-175`) hashes
  the *stored* asset `sha256`, not the placement payload, so it is not
  affected.

**Usage is matched by MPN, per project commit, and only during harvest.**

- `catalog/postgres_schema.py:126-138` `component_usage`:
  `UNIQUE(component_id, project_id, source_revision)`. `source_revision` is the
  project's Git commit. There is no catalog revision column, so one row cannot
  say "R1 is at v2 and R7 is at v3".
- `catalog/project_import_matching.py:84-112` `index_project_component_usage`
  resolves each proposal by `ComponentIdentity.for_part(manufacturer, mpn)`.
  Parts without an MPN identity, or whose MPN was edited, are never matched.
- `project_component_import_service.py:326-379` is the only path that calls
  it (`:378`), inside `run_project_import_session`, meaning a Library Manager
  harvest session (`catalog_admin.py:306-331`, `:437`, `:469`). A project sync
  (`project_import_service.py:1218-1250`) does not refresh usage. It queues
  System Builder detection, thumbnails and metadata only.
- `project_import_acceptance.py:257-263` records usage on accept as well, with
  `source="project_import"`.
- Readers: `catalog/component_history.py:206-230` `list_component_usage`, behind
  `GET /api/catalog/components/{id}/usage` (`catalog_admin.py:850-858`,
  filtered to projects the caller can see), and the Library Manager where-used
  list (`frontend/src/components/workspace/catalog-client.ts:108`).

**The project scan already sees every symbol property.**

- `semantic_index_service.py:390-427` `_schematic_instance_fields` reads all
  properties of every placed symbol through
  `kicad_library_reader.symbol_properties`, keyed by symbol UUID. Line 877
  merges them into `parameters`. `_canonical_fields` (`:503`) keeps unknown
  keys as extras, and `:885` publishes them as `component["fields"]`. A new
  `Prism_*` property therefore reaches `fields` with **no parser change**.
- The same `fields` are passed on unfiltered in three places:
  - harvest proposals: `project_component_import_service.py:110-114` filters
    only instance-context names (`:32-54`) and `_`-prefixed names;
  - acceptance: `project_import_acceptance.py:82-94` copies `fields` into
    `extra_fields`;
  - the non-MPN dedupe key (`project_component_import_service.py:64-79`).
  A stamped project that is harvested would put `Prism_Revision` into the
  catalog's `extra_fields`. Placement would then emit that value as metadata
  (`placement_payloads.py:53-56`), and every placement would split the
  non-MPN dedupe key.
- Design comparison BOM rows make every field a column
  (`design_compare_occurrences.py:197-224`), so a revision bump would show as
  a field change.

**System Builder pattern to reuse.** Each instance stores
`catalog_component_id` + `catalog_revision_id`.
`systems/service_reading.py:85-101` reads the revision through the catalog
(`service_base.py:201-206` → `system_items_facade.system_revision`) and sets
`updateAvailable = latestReleasedRevisionId != catalog_revision_id`.

**Constraint found during review: the facade cannot grow.**
`component_catalog_domain.py` is 1,222 lines, exactly at its ceiling in
`scripts/catalog_architecture_baseline.json:9`. `catalog/AGENTS.md` says it
"may not grow". New reads have to go somewhere other than new facade methods
(see Design §4).

## Design

### 1. Fields on the placed symbol

Two hidden properties, which `rewrite_symbol_payload` appends after the
sorted metadata extras, in this fixed order:

| Field | Value | Why |
|---|---|---|
| `Prism_Component` | component UUID (`components.id`) | Stays the same across revisions. Matches parts without an MPN, and parts whose MPN was later corrected. |
| `Prism_Revision` | revision UUID (`component_revisions.id`) | Exact, indexed, never reused. Only released revisions are served, so it is always a released one. |

Format rules:

- Lowercase canonical UUID text, nothing else. Readers accept exactly that
  form and record anything else as `unparsed` instead of guessing.
- Written with the existing `symbol_property_block(..., hidden=True)`, the
  same mechanism and S-expression form the 16 metadata fields already use and
  that KiCad 8–10 load from `RemoteLibrary/`.
- Deterministic for one (revision, representation), so the byte-stability
  contract still holds. Every symbol payload changes **once**, when this
  ships.
- Reserved names (case-insensitive `prism_*`):
  `symbol_metadata_fields` drops `extra_fields` keys with that prefix, so a
  catalog field can never override the stamp.

Why not a digest in the field: `manifest_hash` is content-only (it excludes
ids and version, `revision_kernel.py:130-143`). That would make it portable
between Prism instances, but a field reader would then need a prefix lookup,
and a 12–16 character prefix is not exact. The usage row records
`manifest_hash` at scan time instead (§3), so the dossier still gets a
digest. This is open question Q1.

**What happens in KiCad.**
- KiCad copies library properties onto the instance when it places a symbol,
  and keeps a copy in the schematic's `lib_symbols` cache.
- "Update symbols from library" after `RemoteLibrary/` was refreshed brings
  the new revision's fields. That is correct: the instance now really does
  use the newer library symbol.
- If the designer leaves the field unchecked in the dialog's field list, the
  instance keeps the old stamp while the cache entry has the new one. Phase 2
  reads the `lib_symbols` copy and records a mismatch (`cache_mismatch`).
- Manual edits or deletion of the field are possible. The field is
  provenance, not a security control.
- Exact dialog behaviour must be checked on KiCad 8, 9 and 10.0.4 (the
  Release Studio pin) before merging (test matrix below).

**Footprint.** In v1 the footprint gets no stamp of its own. KiCad copies
symbol fields to the PCB footprint on "Update PCB from schematic", so the
board carries the same values as a side effect. Stamping footprint payloads
directly is open question Q5.

### 2. Reading the fields in the project scan

A new pure helper, `backend/app/services/catalog/placement_stamp.py`
(foundation layer, no DB):

- `STAMP_FIELDS = ("Prism_Component", "Prism_Revision")`,
  `is_stamp_field(name)` and `stamp_symbol_fields(component)` for the writer;
- `read_stamp(fields: Mapping[str, str]) -> Stamp | None` for readers. It
  matches names case-insensitively and validates the UUIDs.

Callers:

- `placement_payloads.symbol_metadata_fields` / `rewrite_symbol_payload`
  write the fields.
- `project_component_import_service.py` excludes stamp fields in `_proposal`,
  `_dedupe_key` and `_merge_metadata`, through `is_stamp_field`, kept apart
  from the instance-context predicate. It reads the stamp into the proposal's
  `provenance` entries (`catalogComponentId`, `catalogRevisionId`). Harvest
  then stops putting stamps into `extra_fields` and still records the
  placement.
- `semantic_index_service.py` needs no change. Stamps arrive in `fields`.
  `GENERATOR_BUILD` changes only if a module in `GENERATOR_MODULE_PATHS`
  changes, and none does.

### 3. Data model (catalog migration step 10)

Keep `component_usage` as the where-used summary. It stays one row per
(component, project, commit). Add two tables in step
`(10, "project_catalog_placements", _project_catalog_placements)` in
`catalog_schema_migrations.py`. The step is additive and uses
`IF NOT EXISTS`, like steps 7 and 8.

**Ordering dependency.** Step 9 is claimed by the STEP purge fix
([fix-step-purge-released-models.md](fix-step-purge-released-models.md),
`(9, "asset_file_purges", ...)`), which is expected to land first. This
plan takes step 10 and must be rebased onto it. If the purge fix slips, the
numbers must not be swapped after either step has shipped. A branch
database that recorded this step under 9 goes into `RENUMBERED`, as
`agent_tokens_registry` did.

```sql
CREATE TABLE IF NOT EXISTS project_catalog_scans (
    project_id      TEXT NOT NULL,
    source_revision TEXT NOT NULL,        -- project Git commit
    extractor       TEXT NOT NULL,        -- semantic_index_service.generator_cache_tag()
    status          TEXT NOT NULL,        -- 'ready' | 'failed'
    stamped_count   INTEGER NOT NULL DEFAULT 0,
    inferred_count  INTEGER NOT NULL DEFAULT 0,
    untracked_count INTEGER NOT NULL DEFAULT 0,
    scanned_at      TEXT NOT NULL,
    PRIMARY KEY (project_id, source_revision)
);

CREATE TABLE IF NOT EXISTS project_catalog_placements (
    project_id          TEXT NOT NULL,
    source_revision     TEXT NOT NULL,
    component_id        TEXT NOT NULL,      -- from the stamp or the match; no FK (see below)
    catalog_revision_id TEXT NOT NULL DEFAULT '',   -- '' = component known, revision not
    basis               TEXT NOT NULL,      -- 'stamp' | 'inferred_metadata' | 'identity'
    resolution          TEXT NOT NULL,      -- 'resolved' | 'unknown_component' | 'unknown_revision' | 'unparsed' | 'cache_mismatch'
    manifest_hash       TEXT NOT NULL DEFAULT '',   -- copied from the revision at scan time
    references_json     TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (project_id, source_revision, component_id, catalog_revision_id, basis)
);
CREATE INDEX IF NOT EXISTS idx_project_catalog_placements_revision
    ON project_catalog_placements (component_id, catalog_revision_id);
```

- **No foreign keys.** A stamp may name a component from another Prism
  instance, or one that was removed by a reset script. The row must still
  record what the file says. Resolution is computed when the row is written.
  The report recomputes it when read.
- **Per commit, replace-on-write.** One transaction deletes and reinserts
  rows for `(project_id, source_revision)` and upserts the scan row. Running
  it again with the same input is a no-op, so the job can safely resume from
  a checkpoint.
- `component_usage` keeps being written. The component is resolved from the
  stamp first, falling back to the MPN identity (`project_import_matching.py`
  change, behind the existing facade signature). `details_json` gains the
  revision ids, so the where-used list can show them without a join.
- **Interaction with the STEP purge.** Purge rule B of the purge fix
  deletes the STEP files of never-released draft revisions after 30 days.
  Placement rows only ever reference released revisions:
  - a stamp is written only by placement, which serves only
    `released_revision_id`;
  - inference (§6) considers only revisions that were ever released.

  So no row this plan writes can point at a revision whose model rule B may
  purge. A stamp naming an unreleased revision can only come from a manual
  edit or another Prism installation. It is recorded as found, and the
  report shows it as `unknown_revision` or as not released, never as
  placeable. Rule B therefore needs no exclusion for
  `project_catalog_placements`. If a later change lets placement serve
  drafts, it has to revisit both plans.
- Retention: rows are small (one per distinct part per scanned commit). v1
  keeps them all. Pruning is open question Q6.

### 4. Where the code lives (facade ceiling)

- Persistence: `catalog/project_placements.py` (`CatalogProjectPlacements`)
  with `record_scan(conn, ...)`, `placements(conn, project_id, commit)` and
  `report(conn, project_id, commit)`. It is a collaborator: it takes `conn`
  and never commits.
- Wiring without growing the facade: a small service module outside the
  package, `backend/app/services/project_catalog_usage_service.py`. It owns the
  transactions over `catalog_service.connection()` and is the only thing the
  job and API call. This matches how `metadata_batch_lifecycle`/
  `system_items` own their transactions, minus the facade property that would
  cost lines. If maintainers prefer a `catalog_service.project_placements`
  property, the baseline has to move up by about 4 lines (open question Q7).
- `index_project_component_usage` keeps its facade signature. Its
  collaborator also calls `CatalogProjectPlacements.record_scan` when the
  proposals carry stamps, so a harvest records placements too.

### 5. Scan trigger (job)

- New catalog `JobKind` `project_catalog_usage` in `catalog_worker_tasks.py`,
  added to `HANDLERS`/`CATALOG_JOB_KINDS`. It has a lock per
  `project:commit`, attempts 3, and low priority.
- Payload: `{project_id, commit}`. The handler:
  - calls `semantic_index_service.get_or_build(project, commit)`, the same
    reader harvest uses, so it gets correct hierarchical references and DNP;
  - runs `read_stamp` per component;
  - runs inference (§6) for components without a stamp;
  - writes through `project_catalog_usage_service`.
- Queued:
  - best-effort after a successful non-fetch-only sync in
    `run_project_sync_job_v3` (next to `project_synced`, same try/except
    shape; a queue failure never fails the sync);
  - by `POST .../catalog-usage/scan`;
  - by Release Studio for the build commit (§8).
- Cost: one semantic index build per synced commit. It is cached by
  `sourceRevisionKey`, so viewers that open the project later reuse it.
  Should it be opt-in per installation? That is open question Q3.

### 6. Backfill for parts placed before this change

Prism never writes into a checkout (`backend/app/services/AGENTS.md`, "A
project at a revision"), so backfill is **inference recorded in Prism**. It
is never written back into project files.

For a component without a stamp:

1. **Is it Prism's?** Its `lib_id` nickname matches
   `remote_library_nickname(...)` for some asset's `target_library`, and its
   name matches `target_name`. Otherwise it counts only if its MPN identity
   matches (`find_identity_holder`), as today.
2. **Candidate revisions.** Every revision of that component that was ever
   released (`component_release_records`).
3. **Metadata match.** Compare the instance's values for
   `SYMBOL_METADATA_FIELD_ORDER` and the revision's `extra_fields`, using the
   same normalization as `_normalized`, with each candidate's
   `symbol_metadata_fields`.
   - Exactly one best full match gives `basis='inferred_metadata'`,
     `resolution='resolved'`.
   - A tie (metadata unchanged between revisions) picks the **oldest** such
     revision. The report says "v2 or later, same metadata".
   - No match gives `basis='identity'`, `catalog_revision_id=''`.
4. **Symbol/footprint geometry hash (phase 2, optional).** Compare a
   normalized fingerprint of the `lib_symbols` cache entry (pins: number, name,
   electrical type, position; body primitive count) and of the board footprint
   (pads: number, shape, size, position) with fingerprints computed once per
   stored asset. Byte hashes cannot be used, because KiCad re-serializes
   embedded symbols and footprints. This needs the kicad-monkey adapter and is
   not required for v1.

The report and dossier always show `basis`. An inferred revision is never
shown as if it were stamped.

### 7. API and UI for "library updates available"

**API**
- New router `backend/app/api/project_catalog_usage.py`. Every route resolves
  the project with `authorized_project(user, project_id, "read")`, never a
  role-blind lookup.
- `GET /api/projects/{project_id}/catalog-usage?commit=<sha>`: `commit`
  defaults to the checkout's HEAD through `project_revision.resolve_commit`.
  Response:

  ```json
  {
    "commit": "…", "scan": {"status": "ready", "scannedAt": "…", "extractor": "…"} | null,
    "summary": {"current": 41, "updateAvailable": 3, "retired": 0, "unknown": 1, "inferred": 12, "untracked": 5},
    "items": [{
      "componentId": "…", "revisionId": "…", "basis": "stamp",
      "status": "update_available",
      "recorded": {"version": 2, "manifestHash": "…"},
      "latestReleased": {"revisionId": "…", "version": 3},
      "name": "…", "manufacturer": "…", "mpn": "…",
      "references": ["R1", "R7"]
    }]
  }
  ```

  - `status` is one of `current`, `update_available`, `retired`,
    `unknown_component`, `unknown_revision`, `mixed_revisions` (same
    component at two revisions in one commit), `untracked`.
  - It is computed at read time from `components.released_revision_id` and
    `is_active`, the same rule as System Builder's `updateAvailable`.
  - Catalog names are shown only to callers who can see the catalog
    (`require_catalog_browser` rule in `core/policy.py`). Other callers get
    ids and status only. This needs a policy entry and a regenerated role
    matrix (`scripts/export_role_matrix.py`).
- `POST /api/projects/{project_id}/catalog-usage/scan` (`{commit?}`): queues
  the job and returns `{job_id}`. The right is the same as project sync. Every
  refusal is a typed error from `core/errors.py`.
- `GET /api/catalog/components/{id}/usage` gains `revisions:
  [{revisionId, basis, references}]` per item. The change is additive. It
  lets a release reviewer see who is on which version.

**UI**
- A "Library" panel in the project page, in a new feature directory
  `frontend/src/features/project-library-usage/`, lazy-loaded from
  `ProjectDetailPage.tsx`.
- Reads go through `frontend/src/lib/server-state.ts`. The scan job is
  watched through `frontend/src/lib/jobs.ts`.
- Rows are grouped by status, with links to the component and to its revision
  comparison in Library Manager.
- A header badge says "N library updates". It is hidden when there is no scan.
- The panel explains how to update in KiCad (refresh the part from the panel,
  then "Update symbols from library"). Prism offers no write action.
- The Library Manager where-used list shows the version per project.

### 8. What Release Studio could consume (not pinning)

- **Facts from the commit.** The list of `(Prism_Component, Prism_Revision,
  references)` comes only from the committed schematic, so it is
  deterministic. It can join the build's projections and their
  `projection_digests` (`release_studio/dossier.py:328-370`) without breaking
  the determinism contract.
- **Facts from the catalog.** Catalog-side data (names, versions, "update
  available", `manifest_hash`) changes over time. It belongs in evidence or
  the human-readable document only. It must stay out of every hashed
  manifest, as `assert_no_governance_leak` requires.
- **Gaps.** Inferred and untracked parts become an advisory finding, like
  unhashed 3D models.

How and whether a release *pins* catalog revisions is the separate Release
Studio thread.

## Changes (by file)

| File | Change |
|---|---|
| `backend/app/services/catalog/placement_stamp.py` (new) | Field names, reserved-prefix predicate, writer and reader helpers. |
| `backend/app/services/catalog/placement_payloads.py` | Append the stamp in `rewrite_symbol_payload`. Drop `prism_*` keys from extras in `symbol_metadata_fields`. |
| `backend/app/services/catalog/project_placements.py` (new) | Collaborator: record a scan, read placements, build the report. |
| `backend/app/services/catalog/project_import_matching.py` | Resolve the component from the stamp before the MPN. Pass revision ids into `details_json`. Call `record_scan`. |
| `backend/app/services/catalog/collaborators.py` | Wire `CatalogProjectPlacements`. |
| `backend/app/services/catalog_schema_migrations.py` | Step 10 `project_catalog_placements` (after the purge fix's step 9). |
| `backend/app/services/project_component_import_service.py` | Exclude stamp fields from proposals, dedupe and merge. Carry the stamp in provenance. |
| `backend/app/services/project_catalog_usage_service.py` (new) | Transactions, inference (§6), scan entry point. |
| `backend/app/services/catalog_worker_tasks.py` | `project_catalog_usage` kind and handler. |
| `backend/app/services/project_import_service.py` | Queue the scan after sync (best effort). |
| `backend/app/api/project_catalog_usage.py` (new), `backend/app/main.py` | Routes. |
| `backend/app/core/policy.py`, generated role matrix | Read and scan rights. |
| `backend/app/api/catalog_admin.py`, `catalog/component_history.py` | Additive `revisions` on usage items. |
| `frontend/src/features/project-library-usage/*` (new), `frontend/src/pages/ProjectDetailPage.tsx`, `frontend/src/components/workspace/catalog-client.ts` | Panel, badge, where-used column. |
| Docs | `docs/REMOTE_SYMBOL_PROVIDER.md` (the stamp and what KiCad does with it), `backend/app/services/catalog/AGENTS.md` and `.agents/skills/prism-catalog-change/SKILL.md` (contract), `backend/app/services/AGENTS.md` (job list). |

## Tests

**Unit, no database**
- `test_catalog_placement_payloads.py`:
  - the stamp is present, hidden and in a fixed position;
  - a second rewrite gives identical bytes;
  - an `extra_fields` key `Prism_Revision`/`prism_component` is ignored;
  - the existing order assertions (`Reference, Value, Footprint`, SAP Code
    before extras) still pass;
  - a symbol without properties is unchanged.
- New `test_catalog_placement_stamp.py`: the reader accepts canonical UUIDs
  only, matches names case-insensitively, and records `unparsed` for
  anything else.
- `test_project_component_import_service.py`:
  - stamps never reach proposal `fields` or `extra_fields`;
  - a non-MPN dedupe key is the same with and without a stamp;
  - provenance carries the stamp.
- `test_semantic_index_service.py`: a stamped fixture schematic gives
  `fields["Prism_Revision"]`. This guards the "no parser change" assumption.
- Inference: given fixture revisions v1, v2 (metadata changed) and v3
  (metadata unchanged from v2), an instance matching v2 resolves to v2 with
  the "or later" tie, and one matching nothing resolves to `identity`.

**PostgreSQL (`test_component_catalog_postgres_integration.py`, zero
skips)**
- Place v1 (a manifest payload contains v1's ids), release v2, scan a stamped
  project: `update_available` with recorded v1 and latest v2.
- Two stamps of one component at different revisions in one commit give
  `mixed_revisions`.
- A rescan of the same commit is idempotent: same rows, and the scan row is
  updated.
- A stamp with an unknown component id is kept as `unknown_component`.
- The where-used list shows `revisions`.

**Migrations.** `test_catalog_schema_migrations.py`: step 10 applies to a
fresh database and to a step-9 database (with the purge fix's
`asset_file_purges` already applied). A second apply is a no-op.

**API**
- A user without read on the project gets the 404/403 that `authorized_project`
  defines.
- A non-catalog reader gets ids only.
- Scan queues one job, and a duplicate request reuses the lock.

**Frontend.** Client functions, panel states (no scan, scanning, ready, failed)
and the badge count.

**KiCad matrix (manual, recorded in the PR)**
- On KiCad 8, 9 and 10.0.4: place from the panel, and the stamp is on the
  instance and in `lib_symbols`.
- Refresh `RemoteLibrary/` to a newer revision and "Update symbols from
  library" with the default options: record whether the stamp updates.
- Do the same with the field unchecked.
- "Update PCB from schematic" copies the stamp to the footprint.

Gates: `prism-quality-gate` and `prism-catalog-change` checks,
`scripts/check_catalog_architecture.py` (facade ceiling unchanged),
`scripts/check_agent_docs.py`.

## Contracts touched

- **Placement payload bytes** (`catalog/AGENTS.md`, "Contracts that must not
  move"): still deterministic for identical inputs, but the input now includes
  the revision id, and every existing component's symbol payload changes once.
  This is called out in the release note. KiCad clients that cache by
  `sha256` re-download once. Manifest and inline bundle *shape* are
  unchanged.
- **Revision manifest hashes**: unchanged. They hash stored assets.
- **Signed URLs**: unchanged.
- **DBL export**: unchanged (it does not use `rewrite_symbol_payload`). DBL
  placements carry no stamp (Q4).
- **Import/proposal behaviour**: stamp fields are now dropped from proposals.
  This is a behaviour change that the authoritative import suite covers.
- **Where-used**: matches more parts (stamp before MPN). The payload change is
  additive.
- **New reserved field prefix** `Prism_` in symbol metadata: documented in
  `catalog/AGENTS.md` and the catalog skill.
- **Facade ceiling**: untouched by design (§4).

## Risks

- **Stale or edited stamps.** The designer can edit or delete the field, or
  leave it unchanged during an update. The report shows the claim and its
  `basis`. Phase 2 `cache_mismatch` catches the common case.
- **Design comparison noise.** A library update shows as a `Prism_Revision`
  field change with UUID values in BOM diffs
  (`design_compare_occurrences.py:197-224`). That is useful information but
  ugly. A follow-up can label it ("library revision v2 → v3"), which is a
  comparison change of its own.
- **BOM exports.** If a project exports "all fields", two UUID columns appear
  in BOM exports. Release Studio's BOM uses configured columns. Document it.
- **Cost.** One semantic-index build per sync and commit. Mitigations: cache
  reuse, low priority, opt-out (Q3).
- **Cross-instance projects.** A project placed from Prism A and scanned in
  Prism B gets `unknown_component` everywhere. That is correct, but it could
  confuse users. Q1 and Q2 address it.
- **Backfill false confidence.** When metadata is unchanged across revisions,
  inference cannot tell them apart. The tie rule and the "inferred" label are
  deliberate.

## Open questions for maintainers

1. **Field value:** revision UUID (recommended: exact and indexed), or a
   short `manifest_hash` digest (portable between instances, but needs a
   prefix lookup), or both?
2. **Instance identity:** add a third field (e.g. `Prism_Catalog` = a stable
   installation id) so a scan can tell "another Prism's part" from "deleted
   part"?
3. **Scan on every sync, or on demand only** (report button and Release
   Studio)? Is a per-installation setting acceptable?
4. **DBL placements:** add hidden `Prism_Component`/`Prism_Revision` columns
   to the DBL export? That changes the DBL column contract, and a DBL is a
   "latest released" view anyway.
5. **Footprint stamp:** also add the properties to the footprint payload, so
   the PCB records the footprint revision independently of the symbol?
6. **Retention** of `project_catalog_placements`: keep all scanned commits,
   or only the tracked HEAD plus commits referenced by Release Studio
   candidates?
7. **Exposure:** a separate service module (recommended, keeps the facade at
   its ceiling), or a `catalog_service.project_placements` property that
   raises the 1,222-line baseline slightly?
8. **Re-stamping old designs:** should the provider offer "place this exact
   released revision" (not only the latest), so a designer can stamp an
   existing part without upgrading it? Today it serves only
   `released_revision_id`.
