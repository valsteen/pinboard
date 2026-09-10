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
- Candidate: `4ad54ddc4d97bcf01ea10e229893de7cabb1e80a` (commit) over `8d56c961b994eb6e5e3a23d2980a068b3524e808` on `codex/make-large-refactor-incremental`
- Selected source bytes: 34,852
- Meaningful edit sites: 44
- Correction rounds: 4
- Elapsed cost: not preserved
- Token cost: not preserved

Correct-owner localization:

- covered-completion input decoding and canonicalization
- shared semantic validation used by validate and handover
- installed persisted completion journey

The final correction changed two existing owners and their installed lifecycle evidence; the cumulative accepted final-gate candidate still spans 44 tracked files across the complete completion flow.

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
- Selected source bytes: 10,678
- Meaningful edit sites: 4
- Correction rounds: 0
- Elapsed cost: not preserved
- Token cost: not preserved

Correct-owner localization:

- application errors
- persistence tests
- shared test support
- ARCHITECTURE.md

A recursive producer-and-consumer pass reduced the cleanup itself to four files while preserving the focused mutation union, SQLite writer, and stored-state read owner.

Meaningful edit-site groups:

- application mutation error vocabulary
- persistence behavior tests
- shared test-support wording
- architecture ownership map

Wrong paths explored:

- None directly recorded in the selected result evidence.

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

The large delivery reports a final source closure of 79 authorities, 70 batches, and 1,388,069 selected implementation-source bytes. The DTO simplification reports 84 startup authorities and 982,214 startup selected bytes. The persistence selector preserves a broad recursive inventory but no comparable byte total, so none is estimated.

### Locality

The selected large case spans 44 tracked files, while the persistence fixed-point increment names four changed files and the DTO simplification names eight. The smaller increments localized their decisions without implying that file count alone establishes correctness.

### Verification

All three cases required more than a focused unit check: each retained formatting, lint, type, and diff evidence, while the broader cases also exercised coverage, metadata, packaging or installed entry points, persistence reloads, and independent review. Verification breadth is therefore a separate maintenance cost rather than a proxy for navigation or locality.

### Correction

Only the large cross-boundary case records correction rounds: three Candidate A-to-D corrections plus the final covered-completion correction. The persistence and DTO results reached the selected candidate without a recorded implementation correction round.

## Gradual improvements

- Add compact, generated owner maps only where repeated navigation evidence identifies a stable entry point.
- Preserve exact source-selection receipts across correction rounds so unchanged evidence can be reused and changed relationships can be reopened deliberately.
- Prefer one bounded legibility improvement at a time, then compare the same corpus dimensions before proposing another cross-boundary refactor.
- Keep quality evidence separate from reading and correction cost; do not collapse these observations into a readiness score or threshold.
