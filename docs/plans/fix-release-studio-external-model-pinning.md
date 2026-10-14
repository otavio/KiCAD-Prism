# Fix: pin the 3D models a Release Studio build reads

Status: draft for upstream discussion. Item 4 of phase 0b in
`catalog-git-library-sources.md` (section 9). Can merge on its own; it does
not depend on Git library sources.

Line numbers refer to `dev` at `89a8a111`.

## Problem

Release Studio's `build_key` claims that two builds with the same key used the
same inputs. For 3D models that claim does not hold. Two kinds of documents
read models. The model bytes are not in `input_closure_digest`, and the tools
look up models with whatever environment the worker process has.

### What reads a model

| Reader | Where | Environment it runs with |
|---|---|---|
| Cover render, `kicad-cli pcb render` | `documents/artwork.py:376-417`, queued by `documents/acquisition.py:196-200` | Inherited `os.environ` (`services/kicad_tools.py:246-253`). TMP and XDG_RUNTIME are redirected by `isolated_env` (`:166-172`); `HOME` and KiCad's config directory are not. No `cwd`. |
| Assembly views, `kicad-cruncher pcb-svg` | `artwork.py:565-607` → `_run_pcb_svg` `:652-684`, called from `pipeline.py:384-392` | Inherited `os.environ` plus `KIPRJMOD` only (`artwork.py:682`) |
| Testpoint views, same tool | `artwork.py:423-491`, staged board, `project_dir` = the closure board's directory | Same as assembly |

No catalogue step reads a model. `steps.py:129-219` has DRC, ERC, stats,
Gerbers, drill, positions, BOM and schematic PDF, and no STEP, GLB or VRML
export. The model readers are therefore the documents step and the Cruncher
wave.

### What the closure records about models

- The production caller passes no `env` and no `toolchain_resources`
  (`release_studio_build_service.py:191-196`). Every `${VAR}` other than
  `KIPRJMOD` is unbound, and `_PathResolver._value` raises `ExternalPathError`
  (`closure.py:1138-1139`).
- The failure is caught at `closure.py:1026-1047`. When the reference ends in a
  model suffix (`_ADVISORY_SUFFIXES` `:66-68`, `_is_advisory_reference`
  `:957-966`), it is recorded with `advisory=True`, `resolved_path=""`, and no
  digest.
- `InputClosure.external_references` (`:254-262`) leaves advisory entries out,
  so `non_hermetic_reasons()` never names a model. The build service logs and
  carries them only as `closure:` build warnings
  (`release_studio_build_service.py:205-212`, `:463-472`).
- The `ResolvedLibraryPath` docstring (`closure.py:188-193`) says that "nothing
  in the Stage 1 catalogue opens" a model. That was true of the catalogue. It
  is no longer true of the build, since the documents step and the Cruncher
  wave both open models.
- The reference string goes into `library_references` and so into the digest
  (`closure.py:233-243`), but the bytes do not. If a host file changes under
  `${KICAD9_3DMODEL_DIR}`, the build key stays the same.
- `_closure_rows` (`release_studio_build_service.py:265-304`) does not persist
  `library_references` at all. The `ws_release_closure_inputs.kind` CHECK
  allows only `repository|submodule|lfs|toolchain|env`
  (`workspace_migrations/m008_release_studio.py:85-88`).

### Model paths that are never scanned

`_structured_path_references` (`closure.py:1226-1277`) keeps only the scalars
that pass `_is_path_reference` (`:1181-1207`), which requires a `${VAR}`.
These forms of `(model "...")` are therefore not recorded at all:

| Form | Example | What the tools do | What the closure records |
|---|---|---|---|
| Absolute path | `/home/alice/kicad/3d/foo.step` | Read the host file if it exists | nothing |
| Relative path | `3d/foo.step`, `../lib/foo.wrl` | KiCad resolves it against the project directory, then its search paths | nothing. When the file is in the repository its bytes are already hashed as a repository input, but nothing says the board uses it. |
| Embedded | `kicad-embed://foo.step` | Read from the board's embedded files | nothing. The bytes are in the board, so they are already pinned. Today `_PathResolver.resolve` would call this URI "network or unsupported" (`:1071-1074`). |
| Legacy alias | `:ALIAS:foo.wrl` | Resolved through KiCad's 3D search-path aliases | nothing |

A `${KIPRJMOD}/...` model that resolves into the closure is correct today
(`location="closure"`, and its bytes are hashed as a repository input). A
gitignored `${KIPRJMOD}/RemoteLibrary/remote_3d/...` model, the way Prism's
own placement writes it, is `missing`. The tools also find it missing,
because they run against the materialized closure. That is reproducible, but
the drawing is incomplete, and only a warning says so.

### Hermetic is recorded and then ignored

- `hermetic` and `non_hermetic_reasons` are stored on the candidate
  (`release_studio_service.py:288-336`).
- `build_approval_state` (`release_studio_service.py:648-701`) blocks on
  electrical errors, vendor readiness and the two approvals. It never reads
  `candidate["hermetic"]`.
- `ReleasePublisher.publish` (`release_studio_publish_service.py:379-395`)
  publishes when `can_publish` is set. It does not check hermeticity either.
- The frontend declares the fields (`frontend/src/components/release-studio/types.ts:42-43`)
  but does not render them. Only the `closure:` warning strings reach the user.

### Consequences

1. The same commit, configuration and toolchain on two workers, or on one
   worker before and after the stock library or an operator-mounted library
   changes, gives the same `build_key` and a different cover PNG and assembly
   SVGs. `dossier_digest` changes. Nothing explains why.
2. `create_candidate` reuses the candidate on the same `build_key`
   (`release_studio_service.py:309-318`). The `hermetic` flag and reasons from
   the first preparation are kept even when the model situation has changed
   since then.
3. kicad-cli and Cruncher may resolve the same reference differently. kicad-cli
   applies KiCad's built-in defaults and `kicad_common.json`; Cruncher sees only
   the process environment plus `KIPRJMOD`. The cover can then show a model
   while the assembly sheet draws a box for that part. Prism records neither
   resolution.
4. Section 9 of the library-sources plan makes catalog revisions change more
   often, so models that silently change between builds become more likely.

## Goal

For every 3D model the board references, the closure records what a build
step reads, or would read: where it resolved and the sha256 of the bytes, or
that it is absent. That record is part of `input_closure_digest`, so it moves
`build_key`. The tools read exactly the bytes that were hashed. Designer and
QA can see when a build depends on bytes that the commit and the pinned image
do not provide. Whether that blocks a release is for maintainers to decide
(see the options below).

Non-goals: changing how Prism places models in projects (that is phase 0b
item 2), shipping model bytes in the dossier, and isolating KiCad's whole
user configuration for every step (see open question 6).

## Design

### 1. Model references are found structurally, not by suffix

Add `_board_model_references(destination, board_path) -> list[str]` to
`closure.py`. It reads `(model <path> ...)` forms from each `.kicad_pcb` in
`_design_inputs` using the kicad-monkey s-expression parser that
`_structured_path_references` already uses. For every footprint it collects
the first argument of each `model` child, hidden models included. Hidden
models are recorded as well. That errs on the safe side and costs only a hash
(open question 4).

All other path references keep going through the existing generic scan. Model
strings are removed from that scan, so they cannot be recorded twice. This
removes the suffix heuristic (`_ADVISORY_SUFFIXES`, `_is_advisory_reference`)
for boards. The heuristic stays only for non-board files, if any such file
turns out to carry model paths (open question 7).

### 2. Each reference is classified and hashed

New frozen record, in `closure.py`:

```python
@dataclass(frozen=True, slots=True)
class ModelInput:
    source_path: str      # repository path of the board
    reference: str        # the raw (model "...") string
    resolution: str       # see table
    location: str         # stable, host-independent label
    digest: str           # sha256 of the bytes, "" when absent
    size: int             # -1 when absent
```

| `resolution` | When | `location` | Bytes hashed |
|---|---|---|---|
| `closure` | Resolves inside the materialized tree, via `${KIPRJMOD}`, a relative path, or a variable bound into the closure | `closure:<repo path>` | yes (also present as a repository input) |
| `embedded` | `kicad-embed://name` | `embedded:<name>` | Already pinned through the board's own bytes. Optionally, the embedded payload digest (open question 5). |
| `toolchain` | `${VAR}` bound to a pinned toolchain model root (section 4) | `toolchain:<resource>/<rel>` | yes |
| `host` | `${VAR}` bound by deployment configuration to a host directory that is not pinned | `host:<VAR>/<rel>` | yes, then staged (section 3) |
| `absolute` | An absolute path without a variable, and the file exists | `absolute:<the reference string>` | yes, but the tool reads the live host file (section 3) |
| `missing` | Resolved to a place where no file exists, or `${VAR}` is unbound | `missing:<VAR or path>` | no; recorded as absent |
| `unsupported` | `:alias:` forms, other URI schemes, `~` | `unsupported` | no |

Locations never contain a host root. Two workers with different mount points
and the same bytes produce the same record. This is the rule that
`PinnedToolchainResource` already follows (`closure.py:149-159`).

`InputClosure` gains `model_inputs: tuple[ModelInput, ...]`. `to_dict` emits
it, and adds `"closure_schema": 2` so that the digest change is explicit and
versioned, not incidental. New properties: `unpinned_model_inputs` (resolution
`host`, `absolute` or `unsupported`) and `missing_model_inputs`.

Resolution order mirrors KiCad's `FILENAME_RESOLVER` for 10.0.4: expand
variables; for a relative result, use the project directory. The pinned source
must be checked before this is written down: whether KiCad also tries the
working directory or search-path aliases, and whether it aliases legacy
`KICAD{6..9}_3DMODEL_DIR` to `KICAD10_3DMODEL_DIR` (open question 2). Wherever
KiCad would try more than one candidate, record each candidate that exists, so
that "could read" is covered.

### 3. Tools read only what was hashed

Hashing at closure time and letting the tools read the host minutes later
leaves a gap between check and use, and a gap between the two resolvers. So
the build controls the environment the model readers see:

- **Staging.** For every `${VAR}` that some model reference uses, the closure
  step creates `workspace/models/<VAR>/` and copies each resolved model file
  to its relative path there. Only the files the board references are copied,
  not the library. The copy is hashed, not the source: copy first, then hash
  the copy, so the hashed bytes are exactly the bytes the tools will open. An
  unbound or missing reference stays absent from the staged root, so it is
  absent for every tool, the same way on every worker.
- **Binding.** The closure returns `model_env: dict[str, str]` (VAR → staged
  root). It is not part of the digest; the digest holds the hashes. The build
  passes it to the tools through a new `KicadTools.with_env(env)`, next to
  `with_cancel` (`kicad_tools.py:367-372`), so every kicad-cli and Cruncher
  call gets it. That is one seam, and no artwork function changes signature.
  `KIPRJMOD` keeps its current handling.
- **Toolchain roots** (section 4) are staged in the same way. The image is
  read-only in normal operation, but staging keeps a single rule. Large boards
  could instead link from a per-worker cache keyed by digest (open
  question 8).
- **Absolute paths cannot be redirected** without rewriting the board. The
  build must not rewrite its inputs, because DRC and the documents must see
  the committed board. An absolute model is therefore hashed and recorded as
  `absolute`, and it is re-hashed after the documents step. When the hash has
  changed, the build fails with a typed error ("model changed during build").
  It is always unpinned (section 5).

### 4. Pinned toolchain model set

`toolchain_resources` exists (`closure.py:149-171`, `:749-844`), but the
production caller does not pass it. It cannot be used as it is for KiCad's
stock 3D library: `_coerce_toolchain_resources` hashes the whole tree on every
build (`_digest_path`, `:793-820`), which means gigabytes of STEP files.

Proposal: an **image-pinned model root**. The executor image already enters
`toolchain_digest` through `/etc/prism/kicad-base-image`
(`release_studio_build_service.py:63-97`). If the pinned `kicad/kicad:10.0.4`
image ships the stock models (verify that `/usr/share/kicad/3dmodels` exists;
open question 1), register it as a toolchain resource named
`kicad-stock-3dmodels`. Its identity is the image digest that
`toolchain_digest` already covers, so no tree walk is needed. Per-file hashes
come from section 2, so correctness does not depend on the image claim.
`KICAD10_3DMODEL_DIR` binds to it by default.

Implementation: a `PinnedToolchainResource` variant, or a flag
(`identity="executor_image"`), that `_coerce_toolchain_resources` accepts
without calling `resource_root_digest`. The record is
`ToolchainResource(name, digest=<executor image digest>)`.

Operator-mounted libraries, for example a company 3D library volume, come
from a new setting `RELEASE_STUDIO_MODEL_ROOTS`: a `{VAR: path}` mapping that
the build passes as `env`. Their models resolve as `host`: hashed and staged,
but not reproducible from the commit and image alone.

Without this section the fix still works: every stock model would resolve as
`host` or `missing`. With it, a board that uses only stock models and project
models is fully pinned by commit and image.

### 5. Hermeticity, warnings, and what the reviewer sees

Two separate properties, both computed in `prepare_candidate`:

- **hermetic.** Every byte a step reads is identified in the build key. False
  when there are `absolute` or `unsupported` model inputs, or when existing
  non-model reasons apply. `host` models are hashed and staged, so they do not
  break hermeticity.
- **self-contained.** Every model comes from the commit, an embedded file, or
  the pinned image (`closure`, `embedded`, `toolchain`). False when there are
  `host` inputs. A third party cannot rebuild the release without those
  bytes.

Missing models are neither. They are reproducible absences, and they get a
warning that names the parts. `assembly_projection_warnings`
(`artwork.py:321-347`) already counts boxed parts; this adds the list of
missing references.

Store `self_contained` and the model summary on the candidate (counts per
resolution plus the list of unpinned and missing references). Persist the
`model_inputs` rows in `ws_release_closure_inputs`, which needs a migration to
add `'model'` to the kind CHECK (next free number, after `m055`).

How Designer and QA see it. Three options; maintainers decide:

| Option | Behaviour | Cost |
|---|---|---|
| **A. Visible warning** | The Outputs and Approval panels show a "Model inputs" card: pinned / host / missing / unpinned counts, and the list. Approval and publish are unchanged. | Smallest. Matches how missing models are handled today. Relies on reviewers reading it. |
| **B. Acknowledge to approve** | Same card. While `hermetic` is false (and optionally while `self_contained` is false), Designer and QA approvals must carry an explicit acknowledgment, which is stored on the review record. Publish needs both. This copies the existing electrical-error rule (`release_studio_service.py:672-673`: errors require both approvals). | Moderate: one field on review decisions, plus API and UI. |
| **C. Block publish** | `can_publish` is false while `hermetic` is false. `self_contained` could become a policy flag (`policy` is already a governance key, `config/digests.py:34`, so the flag would not move `build_key`). | Strictest. Policy snapshots are not wired yet (`policy_snapshot_captured=False` at `release_studio_build_service.py:228`), so the policy part needs that first. Some existing projects would stop publishing until they pin their models. |

Recommendation for discussion: **B for `hermetic=false`, A for
`self_contained=false` and missing models**, with C available later through
policy once policy snapshots exist.

## Changes

| File | Change |
|---|---|
| `backend/app/release_studio/closure.py` | `ModelInput`; `_board_model_references`; model resolution and staging; `model_inputs`, `model_env`, `closure_schema` on `InputClosure`; properties for unpinned and missing models; the image-pinned toolchain resource variant; leave model strings out of the generic scan; update the `ResolvedLibraryPath` docstring. |
| `backend/app/services/kicad_tools.py` | `KicadTools.with_env` and `LocalTool` support for a bound env, applied under any per-call `env`. |
| `backend/app/services/kicad_tool_replay.py` | Pass the bound env through, and record the variable names (not the values) so replays stay host-independent. |
| `backend/app/services/release_studio_build_service.py` | Pass `toolchain_resources` and `env` from settings; compute `self_contained`; `_closure_rows` emits `model` rows; `execute_build` applies `tools.with_env(closure.model_env)` and the absolute-model re-check; bump `GENERATOR_BUILD` to `r26`. |
| `backend/app/core/config.py` | `RELEASE_STUDIO_MODEL_ROOTS`, and the stock-model resource toggle. |
| `backend/app/services/workspace_migrations/m056_release_closure_model_inputs.py` | Add `'model'` to the kind CHECK; add `self_contained` and `model_summary` to `ws_release_candidates`. Check `m015`'s immutability trigger against the new columns. |
| `backend/app/services/release_studio_service.py` | `create_candidate` stores the new fields; `build_approval_state` applies the chosen option. |
| `backend/app/services/release_studio_publish_service.py` | Only if option B or C: the acknowledgment or block reaches `publish`. |
| `backend/app/api/release_studio.py` + `frontend/src/components/release-studio/` | Expose and render the "Model inputs" card (and, for option B, the acknowledgment). |
| `backend/Dockerfile` | Only if needed: assert that the stock-model directory exists in the image, so a base-image change that drops it fails at build time. |
| `docs/release-studio/ARCHITECTURE.md`, `USER_GUIDE.md`, `OPERATIONS.md` | Model pinning, `RELEASE_STUDIO_MODEL_ROOTS`, what the card means. |

## Tests

In `backend/tests/test_release_studio_closure.py`, the load-bearing suite:

- Each row of the resolution table, including `kicad-embed://`, a relative
  path in the repository, a relative path missing from it, an absolute path
  that exists, an unbound variable, `:alias:`, and a hidden model.
- Changing one byte of a `host` model changes `input_closure_digest`.
  Changing the host *root path* with the same bytes does not.
- Staging: the staged root holds exactly the referenced files; an unreferenced
  file in the same host directory is absent; a missing file stays missing.
- The image-pinned toolchain resource is not walked: assert this with a root
  whose tree walk would raise.
- Replace the existing tests that call these references advisory:
  `test_a_stock_3d_model_reference_is_advisory_not_a_blocker` and
  `test_a_model_missing_from_the_closure_is_advisory_not_a_blocker`.
  `test_a_comma_in_a_quoted_model_path_is_not_truncated` moves to the new
  scanner.

Elsewhere:

- `test_release_studio_replay_build.py`: a replayed build receives the
  `model_env` names; the cover and Cruncher calls see the staged roots.
- `test_release_studio_approvals.py`: the chosen option's rules for
  `hermetic` and `self_contained`.
- `test_release_studio_schema_migration.py`: the migration and the CHECK.
- `test_release_studio_canonicalization.py` and `test_release_studio_dossier.py`
  must pass unchanged. The dossier byte format does not change; only
  `input_closure_digest` values in fixtures move.
- An absolute model rewritten during the documents step fails the build with
  the typed error.
- Run the `prism-quality-gate` checks for Release Studio.

## Contracts touched

- **`input_closure_digest` and `build_key` move for every existing project**,
  because of `closure_schema` and `model_inputs`. Stored dossiers and
  published releases are immutable and stay valid. Rebuilding an old commit
  creates a new candidate instead of reusing the old one. Nothing compares
  build keys across Prism versions, but release notes must say this.
- **Manifest.** Same keys; `input_closure_digest` and `build_key` carry new
  values. `assert_no_governance_leak` is unaffected: no new key is a
  governance key.
- **Build evidence.** Optionally a `closure.json` with `model_inputs` in
  `build-evidence.tar.gz`, outside the dossier digest (`dossier.py:433-460`
  shows the pattern), so a recipient can see which models were used. It must
  not contain host paths or staged roots.
- **`toolchain_digest`.** Unchanged unless maintainers want the stock-model
  identity to be separate from the image (open question 1).
- **`GENERATOR_BUILD`** bumps, because the build now runs with a different
  tool environment.
- **Database.** One new migration. No existing migration is edited.

## Risks

- **Cost.** Copying the models a large board references (hundreds of STEP
  files, possibly hundreds of MB) into each build workspace. Mitigations: hash
  while copying; a per-worker content-addressed cache with hard links
  (immutable once written). Measure on the JTYU-OBC fixture first.
- **The resolver can drift from KiCad's.** If KiCad 10.0.4 tries a path that
  Prism does not model, the tool may read something that was not hashed.
  Staging limits the damage, because only staged roots are bound, but a
  host-default variable that KiCad sets itself, such as `KICAD10_3DMODEL_DIR`
  pointing at `/usr/share/kicad/3dmodels`, is still visible to kicad-cli. Bind
  every `KICAD*_3DMODEL_DIR` explicitly: to the staged root when referenced,
  otherwise to an empty directory. The legacy names need care because of open
  question 2.
- **`kicad_common.json` in the container's `HOME`** can define path variables.
  An environment variable wins over it in KiCad, so explicit binding covers
  the model variables. Isolating the whole config directory would be safer,
  but it affects every step and belongs in a separate change.
- **Cruncher may substitute file extensions**, opening a `.step` next to a
  referenced `.wrl`. If it does, the "could read" set has to include those
  siblings (open question 3).
- **Existing projects get a new warning, or a gate**, on their next build.
  Option B or C changes the release workflow, so it needs an announcement.

## Open questions for maintainers

1. Does the pinned `kicad/kicad:10.0.4@sha256:ee71…` image ship
   `/usr/share/kicad/3dmodels`? If it does, should KiCad's stock models count
   as pinned by the image digest, or should they get their own identity in
   `toolchain_digest`?
2. Does KiCad 10.0.4 alias `KICAD9_3DMODEL_DIR` (and older names) to the
   current library at run time, or leave them unbound? Most real boards use
   the older names. Should Prism bind them to the stock root, which may render
   a different model than the designer saw, or leave them missing?
3. Does Cruncher (Geometer) resolve models exactly as KiCad does, including
   extension substitution and fallbacks?
4. Should hidden `(model ... (hide yes))` entries count as inputs? This draft
   counts them, to be safe.
5. Should `embedded` entries carry the digest of the embedded payload, or is
   pinning the board bytes enough?
6. Should all kicad-cli runs in a build get an isolated `HOME` and KiCad
   config directory? That would be a separate change.
7. Can any non-board design input carry model paths that a step reads? For
   example, a `.kicad_pro` 3D search path.
8. Is a per-worker content-addressed model cache acceptable, or must each
   build workspace hold full copies?
9. Gate: option A, B or C (or the recommended mix) for `hermetic=false` and
   `self_contained=false`?
10. Should `model_inputs` go into build evidence (`closure.json`), the
    candidate API, or both?
