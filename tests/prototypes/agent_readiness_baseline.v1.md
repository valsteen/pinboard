# Agent readiness and change-locality baseline

This retrospective prototype compares three revision-pinned maintenance cases using five exact result selectors. It preserves reported observations and does not rerun the work or infer unavailable costs.

Evidence plan: `cda9df9323997b2bfcfb6821b5a8030b2bf3e1f3a22b39ce72a13780880d4eb2`; 63,335 exact selected bytes.

Cost policy: Elapsed and token totals remain null unless the exact result preserves an end-to-end value; individual test durations are not substituted for task cost.

Interpretation: Use the separate observations to choose and later compare gradual agent-legibility improvements. This corpus is not an aggregate readiness score, ranking, threshold, or causal performance claim.

## Evidence sources

| Authority | Exact selector | Selected bytes | SHA-256 |
| --- | --- | ---: | --- |
| `large-refactor-result` | `.codex/pinboard/attempts/make-large-refactor-delivery-incremental-and-evidence-reusable-1/result.md` | 30,738 | `f5fd823163f1d45826c0a4546c7737e784f04d9cebdce1eb7bc8cb1463c99343` |
| `storytelling-fixed-point` | `.codex/pinboard/attempts/proofread-entire-repository-storytelling-1/result.md#Final integrated result: whole-repository storytelling pass` | 4,114 | `f0a23284e57a1e161563fd68961b3264fdf9a00c0a75f5d58a67b10e43cbd184` |
| `persistence-fixed-point` | `.codex/pinboard/attempts/consolidate-post-cutover-sqlite-persistence-1/result.md#Review candidate: collapse-post-persistence-residue-to-fixed-point` | 10,678 | `95edb10f36906fc5aaa5e3ad9c3b1d0a7f7d61d2029a314c8ff6613a35d9604f` |
| `dto-revalidation-result` | `.codex/pinboard/attempts/remove-internal-dto-revalidation-1/result.md` | 12,334 | `bc9967767d015b52b0585675a4f3681c51bc932cb50844c4f21510b01403848d` |
| `prefix-guidance-result` | `.codex/pinboard/attempts/recommend-body-after-prefix-on-first-run-1/result.md` | 5,471 | `7599cfe0bbdaa3efac9d689fa7544621efd90dd46e8c0cd1a8baaf8876b16a5b` |

## Representative cases

### Large cross-boundary delivery

- Case: `large-cross-boundary-delivery`
- Candidate: `4ad54ddc4d97bcf01ea10e229893de7cabb1e80a` (commit) over `dc5bc7a815459ba7a38f408ceec4cb8933a44395` on `codex/make-large-refactor-incremental`
- Primary evidence: `large-refactor-result`
- Context evidence: `storytelling-fixed-point`
- Selected source bytes: 34,852
- Meaningful edit sites: 44
- Correction rounds: 1
- Elapsed cost: not preserved
- Token cost: not preserved

Correct-owner localization:

- completion input and decision family
- focused SQLite reads and atomic mutation
- immutable artifact and history projection
- semantic validation and handover consumers
- installed completion lifecycle evidence
- architecture, guidance, and generated visitor-guide projections

The pinned range starts at Candidate D and follows the complete final-gate flow through the retained-envelope correction. Its owners span the complete decision, persistence, artifact, validation, installed-evidence, and narrative path.

Meaningful edit-site groups:

- completion input and decision family
- focused SQLite reads and atomic mutation
- immutable artifact and history projection
- validation and handover consumers
- installed contract and lifecycle tests
- architecture, guidance, and generated visitor-guide projections

Wrong paths explored:

- The first installed failure test sent the string None as a subject revision and stopped at authority validation; the helper was corrected to omit the absent value so the intended persistence paths ran.

Verification breadth:

- 366-test full suite with configured branch coverage
- Ruff formatting and lint
- Pyrefly strict diagnostics and type coverage
- generated guide and repository metadata validation
- platform skill validation
- duplication profiles
- source distribution, wheel build, and installed launcher smoke
- git diff check and live Pinboard validation and handover

### Persistence fixed-point cleanup

- Case: `persistence-fixed-point-cleanup`
- Candidate: `working-tree-sha256:d7105e63b870990f7994a716b1c9428ad16fe572557013a526ebb389f6c5d669` (working-tree-sha256) over `c92ee09348448beec3407078b81813fe28315988` on `codex/release-candidate`
- Primary evidence: `persistence-fixed-point`
- Context evidence: `none`
- Selected source bytes: 10,678
- Meaningful edit sites: 40
- Correction rounds: 2
- Elapsed cost: not preserved
- Token cost: not preserved

Correct-owner localization:

- domain lifecycle and work models
- application stored-state read and focused mutation projection
- SQLite decision reads and guarded writes
- interface command, input, and presentation shapes
- service, CLI, persistence, and concurrency evidence
- architecture ownership map

The complete pinned range spans 40 files across the stored-state read, focused mutation, SQLite, interface, test, and architecture owners. Its final four-file cleanup is retained only as within-range fixed-point evidence.

Meaningful edit-site groups:

- domain lifecycle and work models
- application stored-state read, mutation records, conversion, and ports
- SQLite schema, decision reads, guarded relational writes, and transaction handling
- interface commands, strict inputs, actions, views, and errors
- service, CLI, persistence, concurrency, transfer, artifact, and validation tests
- architecture ownership map

Wrong paths explored:

- The first persistence candidate lacked direct cross-family stale and late-rollback evidence.
- The first correction still lacked standalone-artifact zero-history proof until the second review correction.

Verification breadth:

- 96-test focused persistence and transition matrix
- complete 162-test suite and configured coverage
- Ruff formatting, lint, and unused-suppression checks
- Pyrefly strict diagnostics
- repository metadata and platform skill validation
- installed command, package import, Python target, and lock checks
- schema byte identity and schema tests
- git diff check and authoritative Pinboard validation

### Local DTO simplification

- Case: `local-dto-simplification`
- Candidate: `9313f2bf20872b337a8a420c1fd23ed447debe99` (commit) over `747856dcc135b5e46cbc7236bbe03b8ed6696bdb` on `codex/remove-internal-dto-revalidation`
- Primary evidence: `dto-revalidation-result`
- Context evidence: `prefix-guidance-result`
- Selected source bytes: 17,805
- Meaningful edit sites: 8
- Correction rounds: 0
- Elapsed cost: not preserved
- Token cost: not preserved

Correct-owner localization:

- application query models
- domain decisions
- interface presentation and failure mapping
- focused query and decision tests

The final simplification removed redundant predicates and presentation mirrors at their existing owners without changing the architecture dependency direction.

Meaningful edit-site groups:

- src/pinboard/application/queries.py
- src/pinboard/application/query_models.py
- src/pinboard/domain/decisions.py
- src/pinboard/interfaces/errors.py
- src/pinboard/interfaces/work_inspection.py
- src/pinboard/interfaces/work_inspection_models.py
- tests/test_decisions.py
- tests/test_queries.py

Wrong paths explored:

- A naive-datetime query test constructed a value the installed caller never supplies.
- An empty-evidence domain test constructed a DTO that strict boundary decoding already rejects.

Verification breadth:

- 101-test focused behavior suite
- 230-test full suite with configured coverage
- Ruff formatting and lint
- Pyrefly strict diagnostics and source type coverage
- architecture dependency tests
- repository metadata validation
- git diff check
- fresh structural inventories and independent review

## Observed bottlenecks

### Navigation

The retrospective evidence selection reads 34,852 bytes for the large final-gate range, 10,678 for the persistence range, and 17,805 for the DTO range. These exact source-plan bytes measure baseline evidence loading; historical task source-read totals are not compared because the selected results do not preserve one consistent task window.

### Locality

The exact large final-gate range spans 44 tracked files, the complete persistence range spans 40, and the DTO simplification range spans eight. The persistence result separately identifies its final four-file cleanup, but that narrower increment is not substituted for the pinned range count.

### Verification

All three cases required more than a focused unit check: each retained formatting, lint, type, and diff evidence, while the broader cases also exercised coverage, metadata, packaging or installed entry points, persistence reloads, and independent review. Verification breadth is therefore a separate maintenance cost rather than a proxy for navigation or locality.

### Correction

The exact large final-gate range contains one correction after candidate 8d56c, the complete persistence range contains two documented review-correction rounds before the fixed-point candidate, and the DTO range contains none.

## Gradual improvements

- Add compact, generated owner maps only where repeated navigation evidence identifies a stable entry point.
- Preserve exact source-selection receipts across correction rounds so unchanged evidence can be reused and changed relationships can be reopened deliberately.
- Prefer one bounded legibility improvement at a time, then compare the same corpus dimensions before proposing another cross-boundary refactor.
- Keep quality evidence separate from reading and correction cost; do not collapse these observations into a readiness score or threshold.
