# Proposal: Git repositories as Library Manager sources

Status: draft for upstream discussion.

## Problem

Projects can be imported from a Git remote, then fetched and synced. Component
libraries cannot. Library Manager accepts components from three sources only:

- server folders listed in `CATALOG_IMPORT_ROOTS`;
- folders uploaded from the browser;
- project harvest.

To use a library kept on GitHub or GitLab, an operator has to clone it onto the
server, keep that clone updated outside Prism, and expose it as an import root.
Prism then has no record of which remote or commit a component came from, and
cannot tell when the upstream library has changed.

## Goal

Make a Git repository a first-class catalog source, matching projects where it
makes sense:

- register a remote with the same URL policy, credentials and access help that
  project import uses;
- fetch it manually and on a schedule;
- see how far the catalog's imports are behind the remote;
- import at an exact commit, and record that commit on every proposal and
  accepted revision.

The catalog's governance stays as it is. An import still produces proposals
that a person reviews and accepts, and release still needs QA approval. A
library source never writes to the catalog by itself.

## Non-goals

- Pushing catalog changes back to the library repository.
- Retiring or changing components automatically because a symbol was removed
  or edited upstream.
- Push webhooks in the first phase. Polling fetch is enough to start.

## What already exists and can be reused

| Need | Existing piece |
|---|---|
| URL validation, host allowlist, scp/ssh/https forms | `git_remote_url.parse_remote_url`, `git_remote_access.remote_url_policy` |
| Credentials (workspace SSH key, `GITHUB_TOKEN`), pinned host keys | `git_remote_access.git_env`, `git_access_service` |
| Branch listing, SSH fallback for GitHub | `git_remote_access.resolve_remote_access` |
| Access failure classification and help | `git_failures.as_access_error`, `POST /api/projects/access-help` |
| A bare clone with no working tree, periodic fetch from the worker tick | `systems/git_tracking.py` (`ensure_clone`, `enqueue_due_fetches`) |
| Reading files at a commit without a checkout | `project_revision.resolve_commit`, `project_revision.materialize` (`ls-tree` + `cat-file --batch`) |
| Immutable, content-addressed file sets | `local_artifact_store` snapshots (`catalog_import_snapshots`) |
| Discovery, proposals, findings, remediation | `kicad_library_discovery.discover_library`, `library_folder_import_service.build_folder_proposals` |
| Accepting a proposal into a new or existing component | `catalog/project_import_acceptance.py` |
| Import Center review UI | `frontend/src/components/workspace/library-import-center.tsx` and its dialogs |

The new parts are a stored "library source", capturing a snapshot from Git,
commit provenance, and comparing two commits. The design reuses the
folder-snapshot pipeline. A Git import produces a snapshot with
`source_type="git"`, and everything from discovery onward is unchanged.

## Gaps found while reviewing the current pipeline

Fix these first, because repeated automated imports would hit them.

1. **Duplicate MPNs within one folder session fail the session.** Folder
   proposals use `sha256(manufacturer, mpn)` as `dedupe_key`
   (`library_folder_import_service.py`, `build_folder_proposals`). The proposals
   table is `UNIQUE(session_id, dedupe_key)` and `stage_proposals` inserts
   without merging. Two symbols that share an MPN, which is common for variants
   in vendor libraries, abort staging. Project harvest already merges these
   (`project_component_import_service.py`); folder import should do the same,
   or turn the second one into a blocking finding.
2. **Retrying a server-folder job fails once its snapshot is `ready`.**
   `capture_server_snapshot` calls `add_snapshot_file` again, which rejects an
   immutable snapshot. `run_catalog_job_v3` treats unknown errors as retryable,
   so the retry marks the session failed. Capture should be skipped when the
   snapshot is already `ready`.
3. **A repeated `/complete` creates an extra session.** The session row is
   inserted before the job is enqueued, and only the job is deduplicated.
4. **Folder imports record no source revision.** Provenance holds only the
   snapshot id and its manifest hash. `component_usage` is not written for
   folder imports, and `components.external_source` / `external_id` are set
   only when the component is first created.
5. **`sym-lib-table` / `fp-lib-table` are captured but never read.** Library
   nicknames come only from `.kicad_sym` file names and `.pretty` directory
   names. That is fine for KiCad's own library layout, but repositories whose
   tables rename libraries or use `${VAR}` paths get the wrong nicknames.

## Design

### 1. Data model

A new table `catalog.library_sources`, added as a catalog migration step:

| Column | Notes |
|---|---|
| `id` | `lsrc_<12 hex>`, the same `_new_id` style as `repo_` and `prj_` |
| `name` | display name |
| `url`, `dedup_key` | as parsed by `parse_remote_url`; `dedup_key` UNIQUE together with `subpath` |
| `ref` | branch to track; NULL means the remote's default branch |
| `subpath` | optional directory inside the repository, for monorepos |
| `mirror_path` | relative to the library-repos root |
| `last_fetched_at`, `fetched_head` | set by fetch jobs |
| `last_imported_commit` | the commit of the most recent session that reached `staged` |
| `auto_stage` | `off` (default) or `on`; see section 5 |
| `created_by`, `created_at`, `updated_at` | |

Snapshots: an artifacts migration Step 2 adds nullable `source_commit`,
`source_ref` and `library_source_id` columns to `catalog_import_snapshots`.
`source_type` gains the value `git`. Browser and server snapshots leave the new
columns NULL.

Sessions: `scope` gains `git`. `source_revision` (already present) holds the
commit, and `selection_json` holds `{snapshot_id, library_source_id,
base_commit}`.

Proposal provenance, folder form extended:

```json
{"source": "git_library", "librarySourceId": "lsrc_…", "remote": "github.com/org/lib",
 "commit": "<40 hex>", "symbolPath": "symbols/Foo.kicad_sym",
 "snapshotId": "…", "manifestSha256": "…"}
```

`remote` is the `dedup_key`, so credentials can never end up in it. On accept,
the audit event already stores provenance, so every accepted revision is
traceable to a remote and commit without further schema changes. Open question
Q3 covers whether this should also go into a column that can be queried.

### 2. Storage and fetching

- Mirrors live at `PROJECTS_ROOT/.kicad-prism/library-repos/<id>.git`, with an
  optional `PRISM_LIBRARY_REPOS_ROOT` override, following
  `systems/git_tracking.repos_root`.
- Clones are bare, with `--filter=blob:none`. Only trees are fetched up front,
  and a blob is downloaded the first time `cat-file` reads it. This matters for
  3D model repositories: `kicad-packages3D` is several GB and most imports need
  a small part of it. Lazy blob fetches happen inside our git calls and must
  run with `git_env()` so they get the same credentials.
- A new job kind `library_source_fetch` runs `fetch --prune` and records
  `fetched_head`. It takes the `prism_worker` pool, the existing `import`
  resource and a write lock on `library-source:<id>`, the same shape as
  `project_sync`.
- Periodic fetch runs from the worker tick, using `PRISM_AUTO_SYNC_INTERVAL_SECONDS`
  and the per-scan cap that `project_auto_sync_service.enqueue_due_fetches`
  already applies. A fetch never imports anything (except with `auto_stage`,
  section 5).
- Submodules and Git LFS are not followed in phase 1 (see Q5).

### 3. Capturing a snapshot from Git

`capture_git_snapshot(snapshot_id, source, commit)`, in a new
`backend/app/services/catalog/git_library_source.py`:

1. If the snapshot is already `ready`, return. This keeps retries safe; gap 2
   gets the same guard for server folders.
2. `ls-tree -r -l <commit> -- <subpath>`, filtered with the folder pipeline's
   `SUPPORTED_SUFFIXES` and lib-table names. Apply `CATALOG_IMPORT_MAX_FILES`
   and the size limits **before** reading any blob, using the sizes `ls-tree -l`
   reports.
3. Stream each blob through `cat-file --batch` into `artifact_store.put_stream`,
   then call `add_snapshot_file` with the path relative to `subpath`.
4. `complete_snapshot`, then store `source_commit`, `source_ref` and
   `library_source_id`.

Gitlinks (submodules) are skipped, the same way `project_revision.materialize`
skips them. Symlinks are not followed. Paths go through the existing
`LocalArtifactStore.normalize_relative_path` checks.

### 4. Importing, review, and comparing commits

Import is an explicit action: "Import `<branch>` at `<short sha>`".

1. The API resolves the ref to a commit against the mirror, at request time,
   in the same way project harvest resolves its revisions.
2. It creates the snapshot and session and enqueues
   `folder_library_import` with `git_source={library_source_id, commit}`. The
   idempotency key is `git-import:<source>:<commit>`; together with the fix
   for gap 3, importing the same commit twice returns the existing session.
3. The worker captures the snapshot and runs the existing discovery and
   proposal build, which add the Git provenance described above.

**Approval step.** Server-folder import currently stages every component with
no discovery preview, unlike browser import. For large libraries, Git sources
need a preview. Allow `POST /import-snapshots/folders/{id}/discover` on `ready`
snapshots whose source is `git` or `server`, so Import Center can show the
existing discovery dialog (Existing / Retired / New, footprint resolution)
before proposals are staged.

**Comparing commits.** If `last_imported_commit` is set, the session also
records `base_commit`. Discovery runs `git diff --name-status base..commit --
<subpath>` and labels each candidate:

- **unchanged:** none of the symbol's, footprint's or resolved models' files
  changed. Hidden by default, so a re-import after a small upstream change
  surfaces only the parts that changed.
- **changed:** an existing component matched by MPN goes through the current
  accept path. `revision_matches_import` either does nothing or creates a new
  revision. Nothing new is needed there; the diff only makes it visible.
- **new:** as today.
- **removed upstream:** a component whose provenance points at this source and
  whose `symbolPath` disappeared, or whose symbol is gone from that file. It is
  reported as information only. Prism never retires it; a person decides.

The browser discovery dialog currently blocks "Existing" parts from being
approved at all ("It will not be processed again"). For Git sources, a changed
existing part must be selectable, otherwise upstream fixes can never come in.
That is a deliberate change to the UI contract and should be agreed upstream.

### 5. Optional automatic staging

With `auto_stage=on`, a fetch that moves `fetched_head` past
`last_imported_commit` enqueues an import of the new head. The import stops at
`staged`. Accepting, QA and release stay manual. Default `off`.

### 6. API

All routes go under the catalog admin router. Reading needs
`require_catalog_reader`; changes need `require_catalog_writer`; delete needs
admin.

| Method and path | Purpose |
|---|---|
| `POST /library-sources/analyze` | `{url, ref?}`. Runs `ls-remote` for branches and the default branch, then a partial clone with only tree data to count `.kicad_sym`, `.pretty` and 3D files under each candidate `subpath`. Returns `already_registered`. Runs as a job, as project analyze does. |
| `POST /library-sources/access-help` | Reuses the project access-help code for the URL. |
| `POST /library-sources` | `{url, name, ref?, subpath?, auto_stage?}`. Creates the row and enqueues the first fetch. |
| `GET /library-sources` | List with status: `fetched_head`, `last_imported_commit`, commits behind, files changed since the last import. |
| `GET /library-sources/{id}` | Details plus recent sessions. |
| `PATCH /library-sources/{id}` | Change `name`, `ref`, `subpath`, `auto_stage`. |
| `POST /library-sources/{id}/fetch` | Enqueues `library_source_fetch`. |
| `GET /library-sources/{id}/branches` | Branch list from the mirror. |
| `POST /library-sources/{id}/imports` | `{commit? or ref?}`. Creates snapshot and session and enqueues the import. Returns `{session_id, job_id}`. |
| `DELETE /library-sources/{id}` | Removes the row and the mirror (only inside the library-repos root). Snapshots, proposals and accepted components are kept; provenance still names the remote and commit. Refuse while a session is `queued`, `scanning` or has proposals in `accepting`. |

Job errors reuse the `git_access` error code, so the frontend can show the
same access help that project import shows.

### 7. Frontend

Inside Import Center, a "Git sources" section:

- **Add source wizard**, modelled on `import-dialog.tsx`: URL, then analysis
  (branches, library counts per directory, choose a `subpath`), then create.
  Access failures show the existing access-help panel.
- **Source list:** name, remote, branch, "N commits behind · M library files
  changed", last fetch time, plus Fetch / Import latest / Import commit…
  actions.
- Session rail entries labelled `<source name> @ <short sha>`.
- The discovery dialog gets the unchanged / changed / new / removed labels and
  can select changed existing parts.
- `catalog-client.ts` and `types/catalog.ts` get the matching functions and
  types.

### 8. Security

- Never accept or store URLs with credentials; `parse_remote_url` already
  rejects them. Log and show only the `dedup_key`.
- All git calls use `git_env()`: `StrictHostKeyChecking=yes`, `BatchMode=yes`,
  `GIT_TERMINAL_PROMPT=0`. Host allowlist from `IMPORT_ALLOWED_HOSTS_STR`.
- Disable unexpected transports for mirror operations: `protocol.file.allow=never`
  and `protocol.ext.allow=never`. Submodules are never fetched in phase 1.
- Enforce size and count limits from tree metadata before downloading blobs.
- Validate `subpath` the same way snapshot paths are validated.
- Mirror deletion is limited to paths strictly inside the library-repos root,
  as project deletion is limited to `PROJECTS_ROOT`.

### 9. Keeping existing projects on the library version they used

A Git source makes catalog revisions change more often, so the following has to
hold first: an unchanged project, and above all a released one, keeps the
library version it was designed with. Reviewing the current code gives this
picture.

A commit in the library repository changes nothing by itself. A project is
affected only after an import is accepted and the new revision is **released**,
because placement serves only `components.released_revision_id`
(`placement.py`, `remote_provider.py`). From then on:

| Asset | Unchanged project | When it silently picks up the new version |
|---|---|---|
| Symbol | Kept. KiCad copies it into the `.kicad_sch` `lib_symbols` cache, which the commit pins. | Re-placing the part, or "Update symbols from library" after `RemoteLibrary/` or a DBL export was refreshed. The same `lib:name` now resolves to the new revision. |
| Footprint | Kept. Embedded in the `.kicad_pcb`. | Re-placing, or "Update footprints from library", as for symbols. |
| 3D model | **Not kept.** The board holds only `${KIPRJMOD}/RemoteLibrary/remote_3d/<basename>`. That file is gitignored, and its name is the same for every revision (`rewrite_footprint_payload` keeps only the base name). | Placing any revision of the part in that checkout overwrites it. A fresh clone has no file, and fetching again returns the new revision under the same name. DBL export never exports models. |

Additional problems found in the same review:

- **The released STEP can be deleted.** `purge_superseded_step_files`
  (`asset_files.py`) keeps only the models of `current_revision_id`. Starting
  a draft moves `current_revision_id` to the draft at once
  (`revision_kernel.py`), while `released_revision_id` moves only on release.
  So when a draft replaces the model (attaches a new STEP and detaches the
  old one; attaching alone is additive and safe), the next artifact-maintenance
  run deletes the STEP file of the revision that is still released. Placement
  of that revision then fails with `FileNotFoundError`. Every purge, including
  the intended ones, also makes `verify_component_audit_chain` report
  `asset_content_mismatch`. A Git import that changes a model triggers exactly
  this. Reproduced on PostgreSQL 16; see `fix-step-purge-released-models.md`.
- **No record of which catalog revision a project uses.** `component_usage`
  stores the project's Git revision and matches by MPN. Placed symbols carry
  no catalog revision id. System Builder pins `catalog_revision_id`; KiCad
  projects do not.
- **Release Studio does not pin models outside the repository.** The closure
  includes committed files, submodules and LFS. Models reached through
  `${KICAD*_3DMODEL_DIR}`, absolute paths or other unbound variables only
  produce an advisory warning, and their bytes are not hashed. Stored dossiers
  are immutable, but a rebuild can render the cover image and assembly
  drawings with different models while `build_key` stays the same.
- **Viewers.** The WebGPU bundle and System STEP caches are keyed by board
  hash or commit plus KiCad version. Cached results stay as they are, but a
  rebuild uses whatever models the server has at that moment.
- **DBL export** overwrites files of the same name with the newest released
  revision.
- **`--overwrite-assets`** in the bulk import scripts rewrites canonical files
  in place, so older revisions then point at new bytes.

Changes needed before Git sources are enabled (phase 0b). Section 11 lists
the PRs for each item and what each still lacks:

1. **Purge:** see `fix-step-purge-released-models.md`. Chosen rule (option
   B): purge a STEP only when every revision linking it was never released,
   is not the head, and is older than a grace period; record each purge in an
   append-only ledger and quarantine the file. Stopping purging altogether
   (option A) remains the fallback.
2. **Pin 3D models in the project.** Options for upstream to choose from:
   - (a) Embed the model in the footprint at placement time, using KiCad's
     embedded files (`kicad-embed://`). The `.kicad_pcb` then carries the
     model and the commit pins it. Needs checking against the minimum KiCad
     version Prism supports.
   - (b) Make the model path unique per content, e.g.
     `remote_3d/<sha256[:12]>/<name>.step`, and stop ignoring
     `RemoteLibrary/remote_3d/` in the default `.gitignore`, so models are
     committed with the project.
   - (c) As (b) but leave the files out of Git and let Prism fetch them by
     digest. This still needs Prism available for a clone to open correctly.

   (a) or (b) is recommended, since then a release is pinned by its commit
   alone.
3. **Record the revision.** Add a hidden `Prism_Revision` (revision id or
   short digest) field to placed symbols, and record it in `component_usage`.
   That allows a "library updates available" report for each project, the way
   System Builder's `updateAvailable` works, and it also gives the release
   dossier a list of exact catalog revisions.
4. **Release Studio:** hash every 3D model the board references that is
   outside the closure and that the build actually read. Treat a missing or
   unhashed model as non-hermetic, which the reviewers can see; whether it
   should block release is for upstream to decide.
5. **DBL export:** export models too, and either version the output directory
   or document that a DBL is a "latest released" view that must not be used to
   update released projects.

Imports from any source, Git included, must drop the `Prism_Component` /
`Prism_Revision` stamp fields (item 3) from proposal metadata and from the
dedupe key. Otherwise a library that was itself built from placed parts feeds
stale stamps back into the catalog.

Tests: a component with released rev1 and draft rev2 that has a new STEP —
maintenance keeps rev1's STEP. Placement payloads for rev1 and rev2 produce
different model paths or embedded content. A project placed at rev1 still
renders rev1's model after rev2 is released.

### 10. Fix plans and related bugs

Each phase 0b item has its own plan:

| Item | Plan |
|---|---|
| 1. STEP purge | `fix-step-purge-released-models.md` |
| 2. Pin 3D models | `fix-pin-3d-models-in-projects.md` |
| 3. Record the revision | `fix-record-catalog-revision-on-placement.md` |
| 4. Release Studio | `fix-release-studio-external-model-pinning.md` |
| 5. DBL export | `fix-dbl-export-models-and-versioning.md` |

Bugs found while writing them, for separate PRs:

- DBL export keys are not stable (`_2` suffixes). Still open.
- The DBL `fp-lib-table` lists libraries that were not exported. Still open.
- Footprints with more than one model are matched to model assets by
  position, so they can get the wrong models. Fixed by #686.

### 11. Pull requests and remaining gaps

As of 2026-10-14, the open PRs against `dev` (base `89a8a111`) are listed
below. Each one is up to date with `dev` and mergeable. Only GitGuardian has
run. The `Quality gate` workflow is waiting for a maintainer to approve
running it on fork PRs (`action_required`), so all test results so far come
from local runs.

| Item | PR | What it covers | Depends on |
|---|---|---|---|
| 1. Purge | #684 | Purge only when no linking revision is the head, released, `release_status = 'released'`, or has a release record. | — |
| 1. Purge | #691 | Append-only ledger `catalog_asset_file_purges` (catalog migration step 9). The audit check accepts a recorded purge. Reading a purged file gives 404 `asset_purged`. | #684 |
| 1. Purge | #696 | Advisory lock per sha256 shared by the purge and by 3D-model linking. Linking a missing file gives 409 `asset_bytes_missing`. | #691 |
| 1. Purge | #688 | `scripts/check_catalog_asset_files.py`: reports missing or changed files and restores them from the object store or `--search`. | — |
| 1. Purge | #687 | `released_missing_files` in `GET /catalog/health`. | — |
| 1. Purge | #685 | The audit panel names the revision or asset when verification fails outside the event chain. | — |
| 2. Pin 3D models | #686 | Match footprint models to assets by file name. | — |
| 2. Pin 3D models | #697 | Embed models at placement (`kicad-embed://<stem>-<sha12><ext>`). Embedded models are dropped from the manifest. The shipment never grows, and each footprint is capped at 4 MiB. | #686 |
| 3. Record the revision | #695 | Hidden `Prism_Component` / `Prism_Revision` fields on remote placement. The names are reserved, and harvest ignores them. Covers only section 1 of the fix plan. | — |
| 4. Release Studio | #693 | Resolve external models the way KiCad does and hash them into the closure digest. Fails closed as non-hermetic. Does not gate approval. | — |
| 5. DBL export | #689 | The symbol's `Footprint` property equals `LibFootprint`. | — |
| 5. DBL export | #694 | Models under `${PRISM_LIB_DIR}/3DModels/<sha16>/`, models of every released revision kept, `model_count` / `missing_models`. | #689 |
| 5. DBL export | #690 | The bulk import scripts write through `write_canonical_file` and never rewrite a canonical file. | — |
| Related | #692 | A System Builder instance pinned to a revision draws that revision's model, and the scene queues its conversion. | — |

Merge order and conflicts, checked by trial-merging every branch onto `dev`:

- #684 → #691 → #696, #686 → #697 and #689 → #694 are stacks; each later PR
  already contains the earlier one's commits.
- #695, #697 and #694 conflict with each other in `placement_payloads.py`
  (and #695 with #697 in `placement.py`). #694 documents its conflict with
  #686, but none of them mentions the other two. Whichever lands second has to
  combine `stamp=True` with `embed_models=True` in `_placement_payloads` and
  the signed-download path, and add the `model_path` callable from #694.
  Merged together, the stamp and embed changes alter the placed payload bytes
  only once.
- All other pairs merge cleanly.

Gaps that no PR covers yet. These are the candidates for the upstream issue:

1. **Purge, not yet as decided (option B).** There is no grace period and no
   quarantine; files are still unlinked at once. The run commits once, so a
   database error after some unlinks loses their ledger rows (#696
   follow-up). Files purged before the ledger existed still fail the audit,
   and there is no `--record-legacy`. There is no release note or
   `docs/UPGRADES.md` entry naming the data-loss window.
2. **Models that are not embedded are still unpinned.** Models over the cap
   and incompressible models keep `RemoteLibrary/remote_3d/<basename>`. That
   name is the same for every revision, and nothing reports it. Compressed
   blobs are not persisted, so upgrading `zstandard` changes placement bytes
   once. Nobody has checked yet that KiCad 9/10 loads the result
   (`kicad-cli` round trip and manual placement). Harvest does not read
   `kicad-embed://` models (`project_component_asset_extractor.py`), so
   importing a project placed after #697 loses its models. Boards placed
   before #697 have no report and no backfill.
3. **Revision recording is only written, never read.** Still missing:
   - the reader;
   - the `project_catalog_scans` / `project_catalog_placements` tables
     (catalog migration step 10, after #691's step 9);
   - the usage scan job;
   - backfill by inference;
   - the "library updates available" API and UI;
   - use in Release Studio.

   Placement still serves only `released_revision_id`, so re-placing at the
   pinned revision (fix plan Q8) is not possible. DBL stamping (Q4) and
   footprint stamping (Q5) are open. Nobody has checked yet that the stamp
   survives "Update symbols from library" and "Update PCB from schematic" in
   KiCad 8/9/10.
4. **Release Studio hashes models but does not stage them.** The tools can
   still read bytes other than the hashed ones. Still missing:
   - the stock 3D model set pinned in the image;
   - persisting model inputs (`ws_release_closure_inputs`, needs a
     migration);
   - `self_contained` on the candidate;
   - relative, `:alias:`, `$VAR` and `$(VAR)` model forms.

   Whether non-hermetic models gate approval or publish is undecided.
5. **DBL export.** The rebuild is not atomic. Keys are unstable (`_2`), and
   `fp-lib-table` lists libraries that were not exported. Symbols and
   footprints are still a "latest released" view. Boards placed from an older
   DBL keep `RemoteLibrary` paths until "Update footprints from library".
6. **System Builder.** Harness end housings and module connector parts still
   use the current revision, and the catalog UI's Convert button converts
   only the head.
7. **Smaller follow-ups.** No admin UI shows `released_missing_files`.
   `--dry-run` in `import_kicad_library_assets.py` still writes directories
   and reports. The overwrite flags might be renamed.
8. **Git library sources themselves.** There are no PRs yet for phase 0
   (gaps 1–5 above) or phases 1–4.

## Phases

| Phase | Scope | Rough size |
|---|---|---|
| 0 | Fix gaps 1–3, each with a test. Can merge on its own. | small |
| 0b | Keep projects on the version they used (section 9): STEP purge fix, pinned 3D models, revision recorded on placement. Needed before any automated source. | medium |
| 1 | `library_sources` table and mirror, analyze/create/fetch/list/delete, `capture_git_snapshot`, import at a commit with provenance, minimal UI (add, list, import latest). | medium |
| 2 | Periodic fetch, commits behind / files changed status, comparing against `base_commit`, discovery preview for Git snapshots, selecting changed existing parts. | medium |
| 3 | Read `sym-lib-table` / `fp-lib-table` for nicknames and `${VAR}` model paths (gap 5); optional `auto_stage`. | small–medium |
| 4 | Several repositories in one source group (official KiCad layout), submodule and LFS support, push webhooks. Subject to Q4–Q6. | open |

## Tests

- Backend: URL and `subpath` validation; capture from a local test repository
  at a fixed commit (gitlinks skipped, limits enforced before blob reads, retry
  after `ready` does nothing); provenance on proposals and in audit details;
  comparison labels across two commits including a renamed symbol file; delete
  refused while a session is active; access-failure mapping to `git_access`.
- Gaps 1–3: regression tests in `test_library_folder_import_service.py`.
- Concurrency: fetch and import on the same source serialize via the
  `library-source:<id>` lock. Add to `test_component_catalog_postgres_integration.py`,
  which must run with zero skips.
- Migrations: `test_catalog_schema_migrations.py` for the new steps.
- Frontend: client functions, wizard error path, status display.
- Run the checks from the `prism-quality-gate` and `prism-catalog-change`
  skills, including the catalog contract suites.

## Open questions for upstream

1. **Where should the source entity live?** This draft puts it in the `catalog`
   schema, because it belongs to Library Manager. Alternatively it could
   extend `workspace.ws_repositories` with a `kind`, which would reuse project
   sync code but mix catalog sources into the project list and its folder
   visibility rules.
2. **Selecting existing parts in discovery.** Is it acceptable that a Git
   import can propose new revisions of existing (even released) components?
   Accepting already creates a new revision rather than changing a released
   one, so governance holds, but the browser dialog's current rule blocks it.
3. **Querying provenance.** Is the audit event enough, or should revisions get
   `source_remote` / `source_commit` columns so the UI can filter "everything
   from library X at commit Y"?
4. **KiCad's own libraries** are three repositories (symbols, footprints,
   3D models). Should a source be able to group several repositories, or
   should a source be able to resolve 3D models from another source?
5. **Git LFS.** Many in-house libraries keep STEP files in LFS. Support it
   (requires `git-lfs` in the image and LFS credentials), or document it as
   unsupported at first?
6. **Project submodules.** Projects often carry their library as a submodule
   (`kicad-libraries`). Prism currently skips gitlinks everywhere. Offer
   "register this submodule as a library source at its pinned commit" from a
   project?
7. **Credentials.** Clone access today is one workspace SSH key plus
   `GITHUB_TOKEN`. Is that enough for library sources, or are per-source
   deploy keys or GitLab tokens wanted?
