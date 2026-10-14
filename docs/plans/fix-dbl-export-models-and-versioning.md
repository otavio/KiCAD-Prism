# Fix: DBL export 3D models, footprint names, and immutable bulk-import assets

Status: draft for maintainer discussion. PRs: #689 (footprint name alignment), #694 (export models, option C), #690 (immutable bulk-import assets). The atomic rebuild is still open. Implements item 5 of section 9 of
[catalog-git-library-sources.md](catalog-git-library-sources.md) ("Keeping
existing projects on the library version they used"), plus the
`--overwrite-assets` problem listed in the same section.

## Problem

### 1. DBL footprints point at 3D models that the bundle does not contain

`CatalogDblExport.collect_dbl_assets` exports only symbols and footprints
(`backend/app/services/catalog/dbl_export.py:142-144`) and gets their bytes from
`materialize_asset` (`dbl_export.py:145`), which is the remote-provider
placement rewrite. For footprints that calls `rewrite_footprint_payload`
(`backend/app/services/catalog/placement_payloads.py:192-197`), which replaces
every `(model "...")` with
`${KIPRJMOD}/RemoteLibrary/remote_3d/<basename>`
(`placement_payloads.py:155-169`, defaults from
`REMOTE_PROVIDER_DESTINATION_DIR` / `REMOTE_PROVIDER_LIBRARY_PREFIX`,
`backend/app/core/config.py:337-345`).

That path exists only if the same part was also placed in that project through
the remote provider. A board that uses the DBL renders without models, or with
whatever model a remote-provider placement of *any* revision left under the
same base name. The export itself never writes a model file.

### 2. Re-export silently replaces every file with the newest released revision

`export_bundle` deletes and rebuilds the whole root
(`dbl_export.py:164-166`). File names carry no revision:
`SchLib/Prism_<PN>_<lib>_<name>.kicad_sym` (`dbl_export.py:60-64,147-148`) and
`PcbLib/<target_library>.pretty/<target_name>.kicad_mod`
(`dbl_export.py:150-152`). The components come from
`list_components(released_only=True)`
(`backend/app/services/component_catalog_domain.py:1210-1222`), i.e. only
`released_revision_id`. Previously released revisions disappear from the
bundle.

For symbols and footprints this is normal library semantics: KiCad keeps a copy
in the `.kicad_sch` `lib_symbols` cache and in the `.kicad_pcb`, so an unchanged
project only changes on an explicit "Update ... from library". For 3D models it
is not: the board stores only the path (problem 1), so a model path that is
reused across revisions changes what an unchanged, even released, board shows.

The rebuild is also not atomic: the root is removed and rewritten in place by a
synchronous request (`backend/app/api/catalog_admin.py:1409-1413`). A KiCad
session reading `PRISM_LIB_DIR` during an export, or two concurrent exports,
see a partial bundle.

### 3. Symbol `Footprint` property disagrees with `LibFootprint`

The DBL row sets `LibFootprint = <target_library>:<target_name>`
(`dbl_export.py:82-83`) and the `fp-lib-table` registers `<target_library>`
(`dbl_export.py:247-269`). The embedded symbol is rewritten by
`materialize_asset` with `remote_library_nickname(...)`, i.e.
`remote_<lib>:<name>` (`placement_payloads.py:183-191, 29-32`). The integration
contract pins this today: `LibFootprint` is `Prism_Test:R_Test`
(`backend/tests/test_component_catalog_postgres_integration.py:1235`) while the
symbol file hash includes the `remote_prism_test:R_Test` property.

When a part is placed through the DBL, KiCad takes the footprint from the
`footprints` column, so the visible effect is limited (to be confirmed on the
minimum supported KiCad). The symbols in `SchLib/` are, however, registered in
`sym-lib-table` (hidden, `dbl_export.py:259-262`) and can be placed directly or
used by "Update symbols from library"; those get a footprint library nickname
that the bundle's `fp-lib-table` does not define.

### 4. `--overwrite-assets` rewrites canonical files that older revisions use

Both bulk import scripts write canonical files with `_write_or_copy`
(`scripts/import_database_library.py:258-273`,
`scripts/import_footprint_library.py:386-397`). With `--overwrite-assets`
(`import_database_library.py:1139`, `import_footprint_library.py:591`) a file
whose bytes differ is overwritten in place
(`import_database_library.py:727, 752`; `import_footprint_library.py:461, 483`).

`CatalogAssetRegistry.register_asset` then notices the path is already
registered with a different hash and copies the new bytes to
`revisions/<sha256>/<relative>` for the *new* asset
(`backend/app/services/catalog/asset_registry.py:65-82`). The *old* asset row
keeps `canonical_path` = the overwritten file. From then on every revision
linked to the old row (released ones included) serves the new bytes:
`materialize_asset` reads the path and hashes what it reads
(`placement_payloads.py:180-181, 203`), so placement manifests, inline bundles
and the DBL report the new hash while the revision's recorded manifest hash
still describes the old one. Nothing detects the divergence.

`import_database_library.py` makes this easy to hit: it registers assets for
every planned row before the identity check, so re-running it against a
populated catalog with `--overwrite-assets` rewrites files of existing,
released components (`import_database_library.py:1245-1257`, then
`_import_groups`).

The in-app import paths already do the right thing:
`CatalogAssetFiles.write_canonical_file` never overwrites; differing bytes go to
`revisions/<sha256>/<relative>` (`backend/app/services/catalog/asset_files.py:78-100`).

## Constraints

- **DBL contract** (`backend/app/services/catalog/AGENTS.md`, "Contracts that
  must not move"; `.agents/skills/prism-catalog-change/SKILL.md`): bundle
  layout, column order and file names are fixed, and output is byte-stable for
  an unchanged catalog. The integration test pins per-file SHA-256s and checks
  two consecutive exports are identical
  (`test_component_catalog_postgres_integration.py:1152-1253`). This change
  necessarily alters the layout (new `3DModels/` tree) and some file bytes; the
  plan keeps byte-stability and changes the pinned hashes deliberately, in the
  same PR, with the reason in the commit message.
- **Placement contract**: remote-provider manifests and inline bundles must not
  change (`test_catalog_placement_payloads.py:84`, integration manifest and
  inline hashes). All DBL-specific rewriting must be opt-in, with today's
  defaults unchanged.
- `PRISM_LIB_DIR` is a global KiCad path variable, not per project. Any design
  that "pins" by pointing `PRISM_LIB_DIR` at a versioned directory pins every
  project on that workstation at once.
- Module-size and architecture ratchets (`scripts/check_module_size.py`,
  `scripts/check_catalog_architecture.py`): no new private callers, nothing in
  `catalog/` imports the legacy facades.

## Options

### A. Document the DBL as a "latest released" view only

Docs-only. Leaves problems 1 and 3, and boards keep silently picking up other
models. Not sufficient on its own.

### B. Versioned export directories

Write each export to `exports/kicad-dbl/<catalog-digest>/` and keep earlier
ones. Because `PRISM_LIB_DIR` is global, a workstation can follow only one
version, so this does not keep *one* project on an old version while another
moves on. It also multiplies storage by the number of exports. Useful only as
the atomic-build mechanism (see change 4), not as the pinning mechanism.

### C. Content-addressed 3D models, cumulative over released revisions (recommended)

- Export every 3D model under `3DModels/<sha256[:16]>/<sanitized basename>`
  and rewrite footprint model paths to
  `${PRISM_LIB_DIR}/3DModels/<sha256[:16]>/<basename>`.
- Export the models of **every revision that was ever released**
  (`component_release_records`, `backend/app/services/catalog/postgres_schema.py:154-166`),
  not only the current released one. A model path in a board then always
  resolves to the bytes it was placed with, as long as the workstation has a
  bundle at least as new as the board.
- Keep symbols and footprints as a "latest released" view, and document that
  "Update symbols/footprints from library" moves a design to the newest
  released revision, as with any KiCad library. Their copies inside the
  project already keep unchanged projects stable.

This fixes the only silent drift (models) with no change to KiCad-visible
library or footprint names, stays deterministic from database state (so the
byte-stability contract still holds), and lines up with option (b) of section 9
item 2 for the remote provider.

### D. Revision-addressed symbols and footprints too

As C, plus per-revision footprint library names (e.g.
`<target_library>__r<version>`) and symbol libraries, so "Update from library"
on an old board keeps resolving to the old revision. Costs: `fp-lib-table`
grows with every release, footprint and library names change for every
existing DBL user, and the DBL row still references only the latest revision,
so it buys little over C. Not recommended now; listed as an open question.

## Changes (option C)

### 1. Make model destinations a parameter of the footprint rewrite

`placement_payloads.py`:

- `rewrite_footprint_payload(payload, asset, model_assets, *, model_path=None)`:
  `model_path` is a callable `(model_asset) -> str`. When `None`, behaviour and
  bytes are exactly today's (`${KIPRJMOD}/RemoteLibrary/remote_3d/<basename>`).
- `materialize_asset(asset, assets_for_revision, component=None, *,
  footprint_ref=None, model_path=None)`: `footprint_ref` overrides the symbol's
  `Footprint` value (default: today's `remote_<lib>:<name>`); `model_path` is
  passed to the footprint rewrite.

The remote provider (`placement.py`) keeps calling with no keywords.

### 2. DBL asset collection

`dbl_export.py`:

- New pure helper `dbl_model_relpath(model_asset) -> str` returning
  `3DModels/<sha256[:16]>/<sanitize_name(basename)>`, using the asset row's
  stored `sha256` (the content identity, not the path).
- `collect_dbl_assets` passes
  `footprint_ref=f"{target_library}:{target_name}"` (the same string as
  `LibFootprint`, built by one shared helper so they cannot diverge again) and
  `model_path=lambda m: "${PRISM_LIB_DIR}/" + dbl_model_relpath(m)`.
- It also copies each `3dmodel` asset of the revision to
  `export_root/dbl_model_relpath(m)`. Before writing, it verifies that the file's
  SHA-256 equals the row's `sha256`; on mismatch or a missing file it skips the
  file and records it (see change 5) rather than exporting wrong bytes.
- A new step exports the models of all *other* ever-released revisions of the
  exported components (same helper, same verification). Their symbols and
  footprints are not exported.
- Two different files that share a 16-hex prefix fail the export with a clear
  error (never silently overwrite).

### 3. Footprint name alignment

Covered by `footprint_ref` above. The symbol's `Footprint` property in
`SchLib/*.kicad_sym` becomes `<target_library>:<target_name>`, matching
`LibFootprint` and `fp-lib-table`.

### 4. Atomic rebuild

Build into a sibling temporary directory (`<export_root>.tmp-<uuid>`), then
swap: rename the current root aside, rename the new one into place, and remove
the old one. Readers see either the old or the new bundle, never a partial one.
Concurrent exports are last-writer-wins, which is safe because both are
deterministic from the same database state. (Taking a catalog advisory lock is
an option; see open questions.)

### 5. Response and documentation

- `export_bundle` returns, in addition to today's keys, `model_count` and
  `missing_models` (list of `{component_id, revision_id, asset_id, reason}`).
  The response is additive; the endpoint stays synchronous.
- `docs/LIBRARY_MANAGER.md` "KiCad DBL export": the bundle now includes 3D
  models; it is a latest-released view for symbols/footprints; models of earlier
  releases are kept so existing boards keep their models; distribute the whole
  directory and replace it as a unit.
- `docs/UPGRADES.md`: boards that used DBL footprints before this change
  reference `${KIPRJMOD}/RemoteLibrary/remote_3d/...`; re-running "Update
  footprints from library" once picks up the new paths. No automatic migration.

### 6. Bulk-import scripts write immutable files

In both scripts, replace `_write_or_copy`'s overwrite branch with
`service.asset_files.write_canonical_file(service.runtime, destination, payload)`,
which stores differing bytes at `revisions/<sha256>/<relative>` and never
modifies an existing file. Behaviour:

| Flag | Same bytes | Different bytes |
|---|---|---|
| none (default) | reuse | refuse with "Canonical asset conflict" (unchanged) |
| `--overwrite-assets` | reuse | write a new immutable file; existing file untouched |

The flag keeps its name for compatibility; its help text becomes "Store
changed asset content as a new immutable file instead of refusing". (Renaming
it, e.g. `--accept-changed-assets` with the old name as a deprecated alias, is
an open question.) `write_canonical_file` reads the whole payload into memory;
the footprint script currently uses `shutil.copy2` for STEP files, but
`register_asset` already reads every file fully (`asset_registry.py:60`), so
peak memory does not change.

### 7. Detect damage already done (read-only)

Add `scripts/check_catalog_asset_integrity.py` (or a subcommand of an existing
maintenance script): for every `assets` row under the store root, compare the
file's SHA-256 with the row's `sha256` and list mismatches with the revisions
and release records that link them. Read-only; repair (restoring bytes from a
backup or the source library, or recording a new revision) is a manual,
reviewed step. Whether this belongs in the catalog health report instead is an
open question.

## Tests

- `backend/tests/test_catalog_placement_payloads.py`
  - existing `test_model_paths_point_at_the_remote_destination` unchanged
    (proves the default path is untouched);
  - `model_path` callable is applied in order and models beyond the linked
    assets keep their original path;
  - `footprint_ref` override sets the symbol's `Footprint` property.
- New `backend/tests/test_catalog_dbl_export.py` (unit, temp dirs, fake
  placement collaborator through the public constructor):
  - a footprint with a `(model ...)` exports the STEP to
    `3DModels/<sha16>/<name>` and the footprint references
    `${PRISM_LIB_DIR}/3DModels/<sha16>/<name>`;
  - symbol `Footprint` equals the row's `LibFootprint`;
  - component with released rev1 and rev2 having different STEP bytes under the
    same name: both model files are present, the footprint references rev2's;
  - a model file whose bytes differ from the row `sha256` is not exported and is
    listed in `missing_models`;
  - two exports of the same input are byte-identical; a failed export leaves
    the previous bundle intact (atomic swap).
- `backend/tests/test_component_catalog_postgres_integration.py`: update the
  pinned DBL hashes (symbol file changes because of `Footprint`; new
  `3DModels/<sha16>/R_Test.step` entry) and keep the second-export equality
  check. The fixture footprint has no `(model ...)`, so its hash stays the same;
  the unit test above covers the rewrite.
- `backend/tests/test_catalog_import_scripts.py`: re-importing a footprint with
  changed bytes under `--overwrite-assets` leaves the original canonical file and
  the existing asset row byte-identical, and the new asset row points at
  `revisions/<sha>/...`; without the flag it still refuses.
- Integrity check script: one mismatched and one clean asset.
- Run the `prism-catalog-change` checks and the PostgreSQL integration suite
  with zero skips (`.agents/skills/prism-quality-gate/SKILL.md`).

## Contracts touched

| Contract | Change |
|---|---|
| DBL layout and file names | Adds `3DModels/<sha16>/<file>`. Existing names and column order unchanged. |
| DBL file bytes | Symbol files (Footprint property) and footprints that contain models. Byte-stable for an unchanged catalog still holds. Pinned hashes updated in the same PR. |
| `.kicad_dbl`, `sym-lib-table`, `fp-lib-table`, SQLite | Unchanged. |
| Placement manifests, inline bundles, signed URLs | Unchanged (defaults preserved). |
| Revision manifest hashes, audit chain | Unchanged. |
| Bulk-import CLI | `--overwrite-assets` no longer modifies existing files. |
| Export API response | Additive keys only. |

## Risks

- **Bundle size grows** with every release that changes a model. STEP files can
  be large. Mitigation: shared content across components is stored once; size is
  reported. A pruning policy would reintroduce the problem this fixes, so none is
  proposed.
- **Depends on the STEP purge fix** (section 9 item 1):
  `purge_superseded_step_files` (`asset_files.py:121-171`) can delete models of
  revisions that are still released, or were released before. Until that is
  fixed, those models land in `missing_models` instead of the bundle. This is
  visible, not silent, but the two fixes should ship together or purge first.
- **Windows path length**: `${PRISM_LIB_DIR}/3DModels/<16 hex>/<name>` adds 26
  characters; acceptable, but noted.
- **Existing DBL users** see changed footprint/symbol bytes on the next export
  and model paths only after "Update footprints from library".
- **Existing damage from `--overwrite-assets`** is only reported, not repaired.

## Related findings, not in this change

Reported here per the one-purpose-per-change rule; each would be its own PR.

- **DBL keys are not stable.** `Part Number Nocolon` is
  `part_number_nocolon(mpn or value or id)` with `_2`, `_3`... suffixes assigned
  in sort order (`dbl_export.py:181-188`). Parts without an MPN fall back to
  `value` (e.g. `10k`), and a newly released component can shift the suffixes,
  so an existing schematic's key can resolve to a *different component* after
  re-export. A key derived from the component's immutable identity (slug or id)
  would fix it, but changes every existing key.
- **`fp-lib-table` lists libraries that are not exported.** It is built from all
  footprint assets of the revision (`dbl_export.py:247-254`), while only the
  default representation's footprint is written (`dbl_export.py:141-154`).
  A component with a non-default footprint in another library adds a table
  entry pointing at a missing `.pretty` directory.
- **No integrity check at serve time.** `materialize_asset` never compares the
  file with the asset row's `sha256`. The remote provider could refuse to serve
  mismatched bytes; that is a placement-contract decision.

## Open questions for maintainers

1. **C or D?** Is "latest released view for symbols/footprints, pinned models"
   acceptable, or do you want revision-addressed footprint libraries (D) so
   "Update from library" can stay on an old revision?
2. **Which revisions keep their models in the bundle:** every revision with a
   release record (proposed), or only the currently released one plus revisions
   recorded in `component_usage`?
3. **Hash prefix length** for `3DModels/<prefix>/`: 16 hex (proposed), 12 as in
   section 9, or the full digest?
4. **Shared layout with the remote provider.** Should the remote provider adopt
   the same `<root>/3d/<sha16>/<name>` layout (section 9 item 2 option b) in a
   follow-up, so DBL and remote-placed boards behave the same?
5. **`--overwrite-assets`:** keep the name with new semantics (proposed), or
   rename with a deprecated alias?
6. **Integrity check:** standalone script (proposed) or part of the catalog
   health report? Should the remote provider refuse to serve mismatched bytes?
7. **Export concurrency:** is atomic swap with last-writer-wins enough, or should
   the export take a catalog advisory lock / become a catalog job given the
   larger output?
