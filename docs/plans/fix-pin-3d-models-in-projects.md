# Plan: keep placed 3D models on the revision they were placed from

Status: draft for maintainer discussion. PRs: #686 (match models by file name), #697 (embed at placement, option (a)). Over-cap models, persisted blobs, harvest of `kicad-embed://` and a KiCad round trip are still open; see section 11 of the main plan. Expands item 2 of section 9 in
[catalog-git-library-sources.md](catalog-git-library-sources.md) ("Pin 3D
models in the project"). Line numbers refer to the checkout this was written
against.

## Problem

When a part is placed through the remote symbol provider, its symbol and
footprint are copied into the `.kicad_sch` and `.kicad_pcb`, so the project
commit pins them. The 3D model is not copied. The board only holds a path
to a shared file, and that file can change under it.

1. **The model path depends only on the file name.**
   `rewrite_footprint_payload` changes every `(model "...")` to
   `<destination>/<prefix>_3d/<basename>`, using only the stored file's base
   name (`backend/app/services/catalog/placement_payloads.py:146-171`, path
   built at lines 167-168). The defaults are `${KIPRJMOD}/RemoteLibrary` and
   `remote` (`backend/app/core/config.py:337-345`). The asserted result is
   `${KIPRJMOD}/RemoteLibrary/remote_3d/R.step`
   (`backend/tests/test_catalog_placement_payloads.py:84-98`).
2. **A new revision keeps the old file name.** If a new revision brings
   different bytes under the same name, `write_canonical_file` stores them at
   `store/revisions/<sha256>/3dmodels/<lib>/<name>`. The name at the end does
   not change (`backend/app/services/catalog/asset_files.py:78-100`,
   `aux_destination` at lines 115-118). So the path in the footprint is the
   same for every revision.
3. **The model file is shipped under that same name.** The inline bundle
   names `3dmodel` entries `asset["name"]`, which is `path.name`
   (`backend/app/services/catalog/placement.py:128-133`,
   `placement_payloads.py:204`). The manifest uses the same name
   (`placement.py:86-90`).
4. **KiCad overwrites the file and never rewrites the path.** KiCad 10
   writes models to `<dest>/<prefix>_3d/<sanitized name>`, overwriting any
   existing file without checking. It stores footprint bytes exactly as
   received (KiCad `10.0`, `eeschema/panel_remote_symbol.cpp:1282,1312-1335`;
   `eeschema/remote_symbol_import_utils.cpp:75-109`). Verified by reading
   the source.
5. **`RemoteLibrary/` is not committed.** Prism's default `.gitignore` lists
   it (`backend/app/services/kicad_noise_service.py:154`,
   `tools/prism_agent/gitignore.py:50`, used through
   `backend/app/services/git_host_service.py:196-203`). The history noise
   filter also hides it (`kicad_noise_service.py:50-53`).

The result for a project that has not changed, even a released one:

- **Re-placing any revision of the part** in the same checkout overwrites
  `remote_3d/<name>`. Every board in that checkout that uses that name now
  shows the new model.
- **A fresh clone has no model.** Placing the part again fetches the current
  released revision under the old name.
- **Prism's own server-side output is affected too.** The server checkout
  never has these files, so these runs happen without the model:
  - System STEP export (`kicad-cli pcb export step --subst-models`,
    `backend/app/services/systems/service_step_export.py:74`);
  - Release Studio renders;
  - project harvest (`project_component_asset_extractor.py:182-200`).
- **Release Studio records the reference as advisory "missing" only**
  (`backend/app/release_studio/closure.py:1027-1048`). A release is
  therefore not pinned by its commit.
- **DBL export has dangling paths.** It writes the same rewritten
  footprints but never exports models
  (`backend/app/services/catalog/dbl_export.py:140-153`).

## Facts about KiCad that decide the options

These were verified by reading the KiCad `10.0` source unless marked
otherwise.

- **Remote placement needs KiCad 10.** The remote provider first shipped in
  KiCad 10.0.0 and is absent from `9.0`. Every client that places through
  Prism therefore already supports embedded files, which KiCad 9 introduced
  (format `20240706`, Murmur3 checksums from `20240819`). Prism's server
  image runs KiCad 10.0.4 (`backend/Dockerfile:13`).
- **A `.kicad_mod` can carry its own `embedded_files` block.** `FOOTPRINT`
  is an `EMBEDDED_FILES` (`pcbnew/footprint.h:238`), and the parser reads
  the block inside a footprint
  (`pcb_io_kicad_sexpr_parser.cpp:5736-5751`).
- **How the board stores footprint files:**
  - When saving a board, each footprint keeps only name, type and checksum.
    The data moves to one block for the whole board, keyed by name, so each
    model is stored once (`pcb_io_kicad_sexpr.cpp:859-882`).
  - On load, `BOARD::FixupEmbeddedData` copies the data back into each
    footprint (`board.cpp:1263-1280`).
  - `fixtures/release-studio/usb-pd/USB-PD-Trigger-Board.kicad_pcb` in this
    repository shows this layout.
- **Names must be unique per content.** Embedded files are identified by
  name only. If two footprints carry different bytes under the same name,
  one silently wins on save, and every footprint gets the winner's data on
  reload (`embedded_files.cpp:147-162`). Related issues: KiCad #20495, and
  #25220, which is still open.
- **How `kicad-embed://` is resolved.** KiCad looks in the footprint's
  embedded files first, then the board's
  (`common/filename_resolver.cpp:272-298`). The file is extracted to a user
  cache.
- **The encoding:**
  - zstd level 15, one shot, content size recorded in the frame;
  - plain base64 in 76-column lines between `|` markers;
  - checksum is MurmurHash3_x64_128 with seed `0xABBA2345`, computed over
    data zero-padded to a multiple of 4 bytes, written as 32 uppercase hex
    digits (`embedded_files.cpp:43-50,210-302`, `mmh3_hash.h:86-92`).
  - A Python version reproduced both fixture blobs and checksums byte for
    byte (`zstd -15 --no-check`, zstd 1.5.7).
  - There is a 1 GB limit after decompression.
  - **Unverified:** whether zstd output stays identical across library
    versions. zstd does not promise it.
- **The server has to write the block itself.** No `kicad-cli` command
  embeds a file (`fp upgrade` only takes `--force`). The IPC API can do it
  but needs a running GUI.
- **KiCad ignores the inline-bundle `checksum`.** Only the manifest path
  checks `sha256` and `size_bytes`
  (`remote_symbol_download_manager.cpp:122-172`).
- **Fixed KiCad bugs that matter here:**
  - #23257: Update Footprints from Library dropped footprint-local embedded
    file metadata. Fixed for 10.0.
  - #24345: memory blow-up when cloning footprints with embedded files
    during Update PCB. Fixed in 10.0.5.

## Options

| | (a) Embed in the footprint | (b) Unique name + commit `remote_3d/` | (c) Unique name, Prism serves by digest |
|---|---|---|---|
| Pinned by the project commit alone | yes | yes, if the user commits the file | no |
| Fresh clone opens correctly without Prism | yes | yes | no: KiCad has no hook to fetch on open, so an agent or manual step is needed |
| Safe across re-placement in one checkout | yes (unique embedded name) | yes | yes |
| Works with any destination or prefix the user sets in KiCad | yes (`kicad-embed://` is path-free) | no: a hard-coded path breaks if the user changes KiCad's settings (true today too) | no, same |
| Server-side STEP, renders, harvest | work with no change to the checkout | work only if committed | still missing |
| Fixes DBL dangling paths | yes | no | no |
| Repository cost | model is zstd-compressed inside `.kicad_pcb`, stored once per board | raw STEP file in Git; old ones pile up unless pruned | none |
| Effect on existing repositories | none | `.gitignore` must change in every existing repo, plus the history noise rule | none |
| Diff noise | one base64 block per new model in the board file | one binary file per model | none |
| New code | encoder (zstd + Murmur3), block insertion | path rename, `.gitignore` and noise rules | digest endpoint and a client-side fetcher |

The "unique" in (b) and (c) cannot be a subdirectory, because KiCad
reduces the file name to `[A-Za-z0-9_.-]`. It would have to be a flat name
such as `<stem>-<sha12>.step`.

## Recommendation

**(a): embed each model in the footprint at placement.** As a safety net,
models too large to embed fall back to a content-unique external name.

- Only (a) makes the project commit fully self-contained: KiCad, Prism's
  server-side tools, Release Studio and DBL all see the same bytes.
- It needs no change to existing repositories or `.gitignore`.
- It is immune to KiCad's configurable destination and prefix.
- Every client able to place through Prism is KiCad 10, so there is no
  version gap.
- It is what KiCad itself does when a user ticks "embed" on a model.
- (b) puts the pinning in the user's hands (they must commit, and they keep
  an ever-growing `remote_3d/`). (c) does not meet the goal.

## Detailed changes

### 1. New pure module `backend/app/services/catalog/kicad_embedded_files.py` (foundation layer)

- `embedded_model_name(asset) -> str` returns
  `<sanitized stem>-<sha256[:12]><ext>`, using `assets.sha256`. The result
  is restricted to `[A-Za-z0-9_.-]`. The digest makes the name unique per
  content, which avoids KiCad's name collisions.
- `kicad_mmh3_128(data) -> str` is KiCad's padded MurmurHash3_x64_128,
  pure Python.
- `encode_embedded_model(name, data) -> str` returns one
  `(file (name ...) (type model) (data |...|) (checksum "..."))` form,
  formatted exactly as KiCad writes it.
- It has no I/O and imports nothing from the facade, as the catalog
  `AGENTS.md` layering requires.

### 2. Byte-stable compressed blobs (contract)

- Placement output must be byte-stable for identical inputs. zstd output is
  not guaranteed to stay stable across library upgrades.
- So the compressed blob is computed once per model `sha256` and persisted
  immutably: `store_root/embedded/<sha256>.zst`, written next to the asset
  by a new `CatalogAssetFiles` helper, plus its Murmur3 checksum.
- Later placements reuse it. The blob is a derived cache, never part of
  revision manifests.
- If it is missing, it is regenerated. This happens on first use, or can be
  backfilled by the artifact-maintenance job.
- `zstandard==0.25.0` is already in `requirements/runtime.lock`, but only
  as a transitive dependency of `kicad-monkey`. It must become a direct
  entry in `requirements/runtime.in`, following `docs/DEPENDENCIES.md`.
  The backend runs Python 3.12, so the standard-library `compression.zstd`
  is not available.

### 3. `rewrite_footprint_payload` (`placement_payloads.py:146-171`)

- **Embedded case:** each matched `(model "...")` becomes
  `(model "kicad-embed://<embedded name>"`. One `embedded_files` block is
  then inserted before the footprint's closing parenthesis. If the
  footprint already has a block, the new entries are added to it, deduped
  by name, in node order.
- **Fallback case:** when the compressed model is larger than
  `REMOTE_PROVIDER_EMBED_MAX_BYTES` (new setting), the path becomes
  `<destination>/<prefix>_3d/<embedded name>`. This is still content-unique,
  so a re-placement in the same checkout can no longer overwrite it, but it
  is not pinned. Placement and Release Studio report it as such.
- **Setting `REMOTE_PROVIDER_EMBED_3D_MODELS` (default on).** When off,
  output is byte-identical to today, so operators can roll back.
- `materialize_asset` passes model bytes and blobs through an injected
  reader, so the rewrite stays pure.

### 4. Placement bundles (`placement.py`)

- When a model is embedded, KiCad still writes the shipped `3dmodel` file
  to `remote_3d/`, but nothing references it.
- Proposal: skip embedded models in the manifest and inline entries. Keep
  them, under the unique name, only for the fallback case. This also cuts
  the transfer size roughly in half for parts with models.
- The `3dmodel` name in both payloads becomes the embedded name.
- Bump `provider_version` (`backend/app/api/remote_provider.py:117`) to
  `0.4.0` and document the change in `docs/REMOTE_SYMBOL_PROVIDER.md`.

### 5. Size limits

- **Nesting overhead.** The model is base64 inside the footprint, which is
  base64 inside the bundle JSON, which is base64 again. That is about 1.8×
  the compressed size.
- **Unverified limit.** Discovery advertises
  `max_download_bytes = 16 MiB` (`remote_provider.py:134`). Whether KiCad
  applies it per asset or per bundle has not been checked.
- Proposed default for `REMOTE_PROVIDER_EMBED_MAX_BYTES`: 6 MiB compressed.

### 6. DBL export (`dbl_export.py:140-153`)

No code change. It calls `materialize_asset`, so exported footprints become
self-contained. Document this in the DBL section of `LIBRARY_MANAGER.md`.

### 7. Consumers that read models inside Prism

- **Project harvest** (`project_component_asset_extractor.py`) must resolve
  `kicad-embed://` by decoding the board-level block. Otherwise, harvesting
  a project placed this way loses its models.
- **Release Studio closure** needs no change, since only `${VAR}` references
  are scanned. A test should confirm that `kicad-embed://` produces no
  advisory entry.
- **The Rust native reader** already blanks `embedded_files`
  (`board_body.rs:485`, `materialize.rs:2072`).

### 8. Purge fix (section 9 item 1)

This is a separate change, but it must land first or with this one.
Embedding reads the model's bytes at placement, so a released revision whose
STEP was purged fails to place.

## Existing projects

Boards placed before this change keep
`${KIPRJMOD}/RemoteLibrary/<prefix>_3d/<name>` paths, and their bytes are
not recoverable from Git.

- **Report only (proposed for this change).**
  - Detect footprints whose model path starts with the configured
    destination and prefix.
  - Show them as "unpinned remote 3D model" in the project's library usage
    view, and as a Release Studio advisory with its own reason, separate
    from generic "missing".
  - Identify the component from the footprint library ID
    (`remote_<lib>:<name>`) and the symbol's MPN.
  - Name the revision that was likely in use: the one released at the
    project commit's date, taken from append-only release records. Mark it
    as inferred.
- **Remediation is left to the designer, in KiCad:**
  - re-place the part, which embeds the current released revision (it may
    be newer); or
  - embed the local `remote_3d` file they still have, using the footprint's
    3D model embed option.
  - **Unverified:** the exact KiCad 10 UI path.
- **No automatic rewriting of boards.** Prism does not write design files.
  A server-side "pin to revision X" that edits `.kicad_pcb` would be a new
  capability with its own review and authorization. This is listed as an
  open question.
- Recording the revision on placement (section 9 item 3, `Prism_Revision`)
  turns the inferred revision into an exact one for future placements. It
  is a separate change but is best shipped together.

## Repository size

- **Size today.** The model sits in the `.kicad_pcb` as zstd-compressed
  base64. STEP files typically shrink 4–8×, and base64 adds 33%. A board
  with 40 distinct models of about 1 MB each grows by roughly 10 MB.
  Unchanged models do not create new deltas, but zstd output does not
  delta-compress well when a model changes.
- **Git LFS cannot help.** The data is inside a text design file, so LFS
  cannot hold it (it does for (b)). Release Studio already understands LFS
  inputs (`closure.py:578-591`), so (b) remains a possible future option
  for teams that prefer it.
- **Board load time.** Large boards load slower, since KiCad decodes every
  model on load. Embedding the models of KiCad's own library is common
  practice and behaves acceptably.
- The size cap above limits the worst case.

## Tests

Unit tests, in `backend/tests/test_catalog_placement_payloads.py` and a new
`test_catalog_kicad_embedded_files.py`:

- the Murmur3 and encoder output match the two blocks in the USB-PD fixture
  board byte for byte;
- the embedded name is content-unique: same name with different bytes gives
  different names, and the same bytes always give the same name;
- the rewrite replaces paths with `kicad-embed://`, inserts or merges one
  block, keeps non-matched models, and is byte-stable for identical inputs;
- the fallback above the size cap produces the unique external name;
- with the setting off, output is byte-identical to today (the existing
  assertion at `:97` keeps passing).

PostgreSQL integration, extending
`test_assets_release_evidence_and_diff_scope_round_trip` (`:845`) and
`test_non_default_representation_drives_manifest_and_inline_pair`
(`:1255`); zero skips required:

- rev1 and rev2 with a same-name, different STEP produce different
  embedded names and data;
- placing rev1 again after rev2 is released still yields rev1's bytes;
- inline and manifest stay self-consistent (the `sha256` and size checks
  at `:1086-1093`);
- the persisted blob is reused across calls.

KiCad round trip, wherever existing KiCad-backed tests run (the image has
`kicad-cli` 10.0.4):

- load a footprint with an old `(version ...)` header plus the inserted
  block;
- place it onto a board and save;
- check the board-level block and `kicad-embed://` resolution, for example
  by `pcb export step` producing the model.

This test also answers the open question about older footprint version
headers.

Also:

- a harvest test: `kicad-embed://` models are extracted;
- a closure test: no advisory entry for embedded models; existing remote
  paths get the new "unpinned" reason;
- a manual check on desktop KiCad 10.0.5 or later: place, then Update PCB,
  save, reopen, fresh clone, re-place a newer revision. The old board must
  keep its model.
- Checks from the `prism-catalog-change` and `prism-quality-gate` skills.

## Contracts touched

| Contract | Change |
|---|---|
| Placement footprint payload bytes, inline bundle bytes, manifest footprint `sha256` and `size_bytes` | Intentional, one-time change when embedding is on. After it, still byte-stable for identical inputs, through the persisted blob. Recorded through the `provider_version` bump and in release notes. |
| Manifest and inline `3dmodel` entries | Name becomes `<stem>-<sha12><ext>`. Embedded models are omitted (if that proposal is accepted). |
| Signed-URL message and expiry | Unchanged (`signed_urls.py:42`). |
| Revision manifest hashes, audit chain, previews | Unchanged. `revision_manifest_hash` covers stored asset rows (`revision_kernel.py:126`), not placement output. |
| DBL file names and layout | Unchanged. Footprint contents change, so DBL digests change once. |
| Canonical asset paths | Unchanged. A new derived cache at `store_root/embedded/` is not a canonical asset path. |

## Risks

- **zstd behaviour.** An encoding mismatch with KiCad would only be cosmetic:
  KiCad accepts any valid frame whose checksum matches. Cross-version byte
  drift is contained by the persisted blob.
- **Older footprint version headers** (pre-`20240706`) carrying an
  `embedded_files` block: it is unverified whether the parser gates that
  token by version. The kicad-cli round trip settles it. If it is gated,
  bump the header version during the rewrite.
- **Older KiCad 10.0.x bugs.** On versions before 10.0.5, Update PCB is
  memory-heavy (#24345). Document 10.0.5 as the recommended minimum.
- **Larger board files and slower loads**, bounded by the size cap.
- **Same-name collisions** with models the user embedded themselves are
  avoided by the digest suffix. KiCad #25220 does not apply, since a new
  revision always gets a new name.

## Open questions for maintainers

1. **Embed by default?** Should embedding be on by default in
   `v4.0.0-alpha`, behind `REMOTE_PROVIDER_EMBED_3D_MODELS`?
2. **Size cap and fallback.** What should the cap be? Should the fallback
   (unique external name, not pinned) be allowed at all, or should oversized
   models block placement and be reported as an authoring finding?
3. **Shipping embedded models twice.** Drop embedded models from the
   manifest and inline bundle (smaller transfers, a `provider_version`
   bump), or keep shipping them for compatibility?
4. **Persisted blobs.** Is persisting compressed blobs under
   `store_root/embedded/` acceptable, or should the blob and checksum be
   stored on the asset row?
5. **Existing projects.** Is reporting enough, or do you want a "pin to
   revision" action that writes to the project? That needs a design-file
   write path Prism does not have today.
6. **Ship with `Prism_Revision`?** Should this ship together with
   `Prism_Revision` recording (section 9 item 3), so the report is exact
   rather than inferred?
7. **Option (b) as an alternative.** Should (b) be kept as an opt-in for
   teams that want models as files in Git or LFS?

## Out of scope, found during review

`rewrite_footprint_payload` matches `(model ...)` nodes to model assets by
position. Assets are ordered by `target_library, target_name, sha256`
(`revision_kernel.py:344-350`), not in the order the nodes appear in the
footprint. So a footprint with several models can be given the wrong
models. Report and fix this separately.
