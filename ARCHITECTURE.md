# Architecture

## System overview

Pinboard is a local coordination system for one repository-owned work ledger. The installed `pinboard` command discovers the project, opens its work root, reads and validates current state, computes legal actions, and applies accepted changes atomically. Pinboard is an installed application and plugin, not a supported Python library: its production interface is the `pinboard` CLI, with `python -m pinboard` as an alias to that same interface; internal modules and package exports are not public extension APIs.

Codex is the primary, stress-tested outer integration. Claude Code can load the same repository root experimentally through `.claude-plugin/plugin.json` and `--plugin-dir`; both integrations call the same launcher, Python entry point, skills, schemas, and SQLite authority. Runtime-specific identity, worktree, subagent, permission, waiting, and optional-messaging operations stay in the agent-facing runtime adapter rather than entering the Python dependency graph or creating another lifecycle.

SQLite is the authority for the repository work ledger. Attempt continuation and review jobs are read-only projections from current authoritative state and verified accepted artifacts; they add no lifecycle state or stored workflow owner.

Each invocation resolves two Git-backed roots with different owners. The source checkout root is the current or explicitly selected checkout's exact top level, including a linked worktree, and owns project-file and authority reads for that attempt. The shared repository root is derived from the Git common directory and owns repository-shared Pinboard state and local Git exclude configuration. In a primary checkout the two roots are equal; in a linked worktree they are intentionally different.

Agent guidance has a separate dependency boundary from the Python runtime graph. Repository guidance routes a task to the installed Pinboard skill before read-only work becomes repository writes and while accepted repository work awaits human disposition; the Pinboard delivery and intake skills remain specialized callers. The main skill owns the shared threshold for user-relevant process detail, human task-start routing, explicit review-actor language, current-responsibility routing, preparation start, project-specific Git baseline inference and checkout confirmation, bounded checkout-risk assessment, reconciliation of late accepted direction with the current brief and candidate, and human-approved repository wrap-up. Specialized skills retain their distinctive evidence and blocking outcomes without creating separate narration policy, persistent checkout ownership, or wrap-up state.

The bundled repository-care skills are independent guidance owners. Repository Readiness owns assessment of whether an unfamiliar repository can be changed reliably, including its authority map, coverage modes, developer-navigation method, and storytelling lens. Slop Cleanup owns recursive removal of approved unsupported residue and its deletion fixed point. Maintaining Agent Guidance owns placement and reconciliation of durable AI-facing guidance. They may refer to one another or use Pinboard for coordination, but no sibling or ledger is required for their baseline workflow.

One work root has three durable roles:

```text
.codex/pinboard/
  state.sqlite3   # authoritative lifecycle, dependency, authority, and history state
  artifacts/      # immutable long-form bytes referenced by SQLite
  views/          # repairable Markdown projections generated from SQLite
```

The architecture map is semantic rather than an exhaustive file tree. It names the owners and dependency boundaries a contributor needs to preserve; leaf modules that do not change those relationships need not be listed individually.

## Dependency direction

Pure decisions remain at the center:

```text
interfaces ────────> application ────────> domain
     │                    ▲                  ▲
     └────────────> adapters ───────────────┘
```

`domain` depends only on the Python standard library and `msgspec` for exact canonical records. `application` depends on domain values and storage-independent capability protocols. `adapters` implement those capabilities with SQLite and the filesystem. `interfaces` decode external values, compose concrete adapters with application use cases, and present results. Production dependency tests keep domain independent of outer layers, application independent of adapters and interfaces, and adapters independent of interfaces. They also keep interface composition acyclic and constrain the CLI entry point to routing and final error presentation.

The method used to preserve these boundaries, expose effects, and stop decomposition at a useful fixed point is recorded in [the design principles](DESIGN_PRINCIPLES.md).

## Runtime ownership

### Launcher

The plugin `scripts/pinboard` launcher prefers the package already installed in the plugin cache's ready environment. When executed from a source checkout without that installed environment, it falls back to the repository's locked uv execution path. This keeps ordinary installed use independent of ambient uv cache selection while preserving one source-development route on macOS and Linux.

### Package root

Only distribution version lookup and command composition live at the package root.

| Location | Ownership |
| --- | --- |
| `pinboard/__init__.py` | Installed distribution version lookup |
| `pinboard/__main__.py` | `python -m pinboard` route to the CLI |

### Domain

`domain` owns immutable identifiers, ledger values, canonical history records, and pure decisions. It does not read files, issue SQL, parse command-line or JSON input, render views, or coordinate transactions.

| Owner group | Responsibility |
| --- | --- |
| `work_models.py`, `ledger.py`, `identifiers.py`, `errors.py` | Work-ledger values and canonical artifact kinds, read-only snapshot behavior, opaque identifiers, expected decision failures, exact mismatch facts, effect disposition, retry classification, and recovery-action receipts |
| `decision_models.py`, `decisions.py` | Closed item and attempt commands, explicit lifecycle-change variants, and lifecycle, dependency, requirement, and review legality |
| `authority_models.py`, `authority_decisions.py` | Closed ready-item preparation and attempt authority operations, lifecycle, and fencing |
| `proposal_models.py`, `proposal_decisions.py` | Closed proposal intake values, intake queue placement, and relation-derived dependency decisions |
| `history.py`, `definition_decisions.py` | Canonical work-item definition records and digests, immutable revision legality, dependency replacement and cycle decisions, and receipt relationships |

Expected rejections return typed failure values. Domain and stale-persistence paths use `DecisionFailure`; application-owned queries and dispatch selection use their own closed failures where the installed interface must preserve a more specific public error. The interface returns closed command, proposal, and dispatch failures through one CLI presenter. Low-level decoders may raise while values are still external representations, but an installed use case converts its exact advertised invalid-input outcomes into its typed result. Infrastructure failures, malformed persisted relationships, and programming-contract failures remain typed exceptions.

### Application

`application` owns use-case sequencing, persistence contracts, read models, and cross-capability workflows. It converts complete stored state into the narrower domain snapshots used by decisions and commits only closed accepted mutations.

| Owner group | Responsibility |
| --- | --- |
| `stored_state.py` | Complete typed read aggregate plus storage-specific vocabulary |
| `mutation_models.py`, `mutations.py`, `ports.py` | Closed exact mutation records, exhaustive decision-to-relational conversion, and storage-independent transactional capabilities |
| `decision_projection.py`, `service.py` | Shared-index projection of complete stored collections into domain decision facts and locked mutation orchestration |
| `actions.py`, `query_models.py`, `queries.py`, `handover.py` | Legal-action discovery plus current overview, exact item-status, current-definition, bounded definition-history, parallel-preview and attempt-continuation records, and the strict versioned portable handover projection from one complete stored snapshot |
| `artifacts.py`, `artifact_publication.py`, `dispatch_models.py`, `dispatch.py` | Immutable artifact references and typed brief identity, artifact-acceptance capabilities, activation, resume, and rebind brief guards, and result-shaped dispatch selection, review publication, and final authority confirmation |

SQLite rows are not active domain objects. `StoredWorkState` is the exact typed read aggregate without SQL handles or filesystem paths, while live mutations carry only the accepted decision, receipt, and affected auxiliary values. `LedgerSnapshot` remains the storage-independent decision input.

### Adapters

Adapters own concrete persistence and filesystem mechanics without deciding product legality or presenting commands.

| Owner group | Responsibility |
| --- | --- |
| `files/root.py`, `files/file_io.py`, `files/models.py`, `files/errors.py` | Distinct Git-backed source-checkout and shared-repository discovery, repository-local exclusion of the default durable root, durable-root resolution, file-operation records, exact failure families, directory creation, and atomic file publication that preserves an existing regular file when its bytes already match and requires directory-only synchronization support |
| `files/artifacts.py` | Immutable artifact naming, publication, digest verification, and reference resolution |
| `files/views.py` | Semantic-content-derived queue, item, attempt, and history projections; interface composition supplies complete live-v2 brief projections |
| `sqlite/schema.sql`, `sqlite/database.py`, `sqlite/models.py`, `sqlite/errors.py` | Exact current schema, connection configuration, typed row conversion, compare-and-set result helpers, schema verification, initialization, diagnostic and runtime read transaction scopes, synchronization, and exact storage failures |
| `sqlite/store.py` | Complete runtime connection and write-transaction lifetime, public store capabilities, exhaustive accepted-mutation routing, project revision advancement, expected-result rollback selection, and post-write readback |
| `sqlite/state.py` | Complete `StoredWorkState` assembly, cross-record validation, project metadata, and transition history |
| `sqlite/lifecycle.py`, `sqlite/proposals.py`, `sqlite/artifacts.py`, `sqlite/authority.py` | Thematic row conversion, reads, and targeted effects over an explicitly supplied connection |

### Interfaces

Interfaces own user-facing boundaries. They may depend on application use cases, concrete adapters for composition, and domain identifiers needed to construct typed input. They do not own lifecycle legality or persistence policy. The entry point owns one exhaustive route and process exit policy; thematic interface modules own concrete cross-layer composition for one command family.

| Owner group | Responsibility |
| --- | --- |
| `cli_commands.py`, `cli_parser.py` | Exact leaf command records, complete command grammar, field-local constraints on exact leaves and coupled option records, and the few coupled-option decoders needed to construct the closed command union |
| `cli.py`, `cli_output.py` | Sole exhaustive command-family route, final typed-result-to-exit policy, canonical success output, and versioned structured rejection or committed-effect presentation |
| `tool_contract.py` | Versioned static projection of installed parser leaves, the exact command union, action semantics, strict input and artifact schemas, effect classes, and retry rules; inventory validation rejects missing, duplicate, or unknown classifications |
| `work_inspection_models.py`, `work_inspection.py` | Read-only status, overview, item, action, input-contract, parallel-preview, attempt-continuation, and candidate-bound review-job composition and presentation |
| `action_selection.py`, `transition_models.py`, `transition_input.py`, `transitions.py` | Current-action selection from an opaque CLI capability receipt, strict payload decoding directly into the exact command owned by that action variant, complete definition-replacement conversion, checkpoint identity checks, invoking task identity, and transition presentation |
| `project_handover.py` | Read-only composition of one SQLite snapshot with verified immutable artifact bytes and explicit supported media types before canonical JSON presentation |
| `preparation_authority.py`, `attempt_authority.py` | Thematic ready-item preparation- and attempt-authority command composition, including ordinary atomic preparation start and exact lower-level recovery operations |
| `brief_source_models.py`, `brief_sources.py`, `brief_source_commands.py` | Strict source manifests and selector grammar, trusted selector conversion, deterministic source planning and selection, and installed plan or batch presentation |
| `work_brief_models.py`, `work_briefs.py`, `work_brief_contract.py` | Strict v2 brief and review records with same-record validation, exact canonical codecs, cross-artifact review validation, digest computation, reviewed-authority checks, complete Markdown rendering, and generated unresolved local or cross-boundary construction contracts |
| `work_brief_publication.py`, `dispatch_brief.py` | Canonical brief publication, typed accepted-brief identity checks, cross-boundary review validation, dispatch orchestration, and canonical launch prompt rendering |
| `work_views.py` | Shared live-attempt brief projection plus post-commit generated-view refresh and rebuild composition |
| `work_state.py`, `work_state_models.py`, `work_state_commands.py` | Fresh initialization, optional post-success repository-care advice, Codex-only user-config advice selected at the outer runtime boundary, whole-work-root validation, root resolution, validation presentation, and repair commands |
| `proposal_models.py`, `proposals.py`, `proposal_commands.py` | Strict proposal-file records, decoding, explicit boundary-to-domain conversion, SQLite intake composition, and result presentation |
| `errors.py` | Closed command, proposal, and dispatch result families plus exact boundary and infrastructure exception families |

## Storage boundaries

### Authoritative SQLite state

`.codex/pinboard/state.sqlite3` owns project revision and host epoch; immutable work-item definition revisions; item, attempt, dependency, requirement, and proposal state; preparation and attempt authority; accepted artifact references; and transition history. Every retained attempt or preparation lease must have its exact current generation anchor before application code can consume it. Relational dependency rows are the query-efficient projection of the current definition and must match it exactly. A mutation opens a write transaction, rereads current state and authority, updates only the relations named by one accepted closed mutation, and advances the revision with its history receipt. Revision and affected-row guards return typed stale-action or fencing rejection values. The transaction owner rolls back those expected failures; SQLite, persisted-invariant, and programming failures roll back and remain exceptional. Initialization creates the exact empty current schema directly and owns every row required by that valid empty state; live state enters only through exact mutations.

### Immutable artifacts

Accepted requirements, briefs, results, reviews, and other evidence are immutable files below `.codex/pinboard/artifacts/`. Canonical v2 work briefs and independent brief reviews are strict JSON; SQLite stores each accepted artifact's kind, selector, revision, digest, and size. Attempts and history own the relationships they actually consume. The installed brief-publication path validates and canonicalizes a candidate, publishes immutable bytes, and accepts their reference without changing scheduling. Activation, resume, and rebind validate their selected v2 brief identity against the locked ledger snapshot. Readers resolve artifacts through accepted references and verify their bytes. The files do not independently own lifecycle state.

### Generated views

`.codex/pinboard/views/` is human-readable output derived from SQLite and its accepted artifact references. Generated bytes include semantic revisions where the individual view presents them, but omit the unrelated global database revision. A live v2 attempt view contains a complete Markdown rendering of the canonical JSON brief; Pinboard is its only writer, and no runtime path reads it for brief semantics. A successful SQLite commit is authoritative before refresh begins. If view refresh fails, the command reports repair guidance without rolling back the accepted transition. `pinboard views rebuild` recreates the full projection without replacing files whose bytes already match, and validation distinguishes an authoritative defect from stale or missing generated output.

### Project evidence boundary

Pinboard does not require or produce a companion notes directory. Project documentation and other human-owned notes remain outside the installed runtime contract. Durable execution semantics enter Pinboard only through accepted immutable artifact references.

## Representative flows

### Initialization and reopen

`pinboard init` resolves the default `.codex/pinboard` root below the shared repository root and idempotently adds only `/.codex/pinboard/` to that repository's local Git exclude file. Invoking initialization from a linked worktree therefore reuses the repository ledger and does not create a worktree-local authority. It never edits a committed `.gitignore`, so unrelated `.codex` content remains visible. An explicit `--work-root` selects that exact path instead. Interface-owned work-state composition creates the SQLite schema when `state.sqlite3` is absent. When the database exists, it verifies that exact schema before reconciling the fixed publication staging path: a same-file staging alias left after publication is removed, while a different-file conflict is rejected without replacement. It then ensures the artifact directories exist and rebuilds views. Only after a successful fresh initialization and its receipt, the outer work-state interface prints one optional repository-care pointer. The default Codex runtime may also read the user Codex config to recommend `body_after_prefix` when that user default is absent; the explicit Claude runtime suppresses only this Codex-specific line. The pointer starts no skill or work, and neither effect writes configuration. A trusted project's `.codex/config.toml` may override the Codex user default. Resume and failure paths print neither effect; unreadable or malformed user config suppresses only the setting recommendation.

### Reads and validation

Status, overview, exact item status, current definition, bounded newest-first definition history, action discovery, parallel preview, and attempt inspection open one `StoredWorkState` snapshot through `SQLiteWorkStore`, then build application-owned read models. Attempt inspection resolves the exact outcome owner from the verified accepted brief and derives one tagged next operation from the attempt, recorded conditions, and currently advertised legal actions. It never reconstructs ownership from task ancestry and never persists a second continuation state. Expected absence or an unavailable selection returns a typed result. Nullable source, notes, and queue position remain null through application JSON projections; human-facing text and generated Markdown render explicit absence markers. Each selected SQLite row is converted directly into its declared stored-state record; explicit storage checks are reserved for row cardinality, canonical history JSON, and relationships spanning records. These reads never parse generated Markdown. Interface-owned work-state composition verifies the database and every accepted artifact reference, validates live v2 brief identity and structure through the typed boundary, keeps historical terminal brief bytes opaque, then reports generated-view drift separately.

`pinboard handover --json` opens one validated `StoredWorkState` snapshot, projects every admitted and pending project fact into the strict application-owned `pinboard-project-handover/v2` model, and lets the interface owner verify and read each referenced artifact through the filesystem adapter. Artifact suffixes outside the installed media vocabulary are rejected before any JSON is written. Only after the full package is materialized does the CLI write canonical JSON. The path does not read generated views or mutate SQLite, artifacts, lifecycle, or authority.

### Brief source planning

The installed `pinboard brief-sources` command reads a strict source manifest without opening work state. It resolves every source-checkout-relative whole-file or Markdown-heading selector before emitting content, rejects overlapping line spans, reports normalized selected-source digests, and assigns every selected UTF-8 byte to one consecutive segment and batch. Its heading selection is also used by dispatch against the same selected source checkout when validating reviewed-authority digests. Planning is read-only and does not acquire authority or write project state.

`pinboard tool-contract` is another state-independent read. It derives one complete index from the installed parser leaves, recursively expanded exact command union, closed action family, and closed brief-boundary family. A selected operation projects exact installed CLI usage including optional global-root placement alongside its purpose, mutation class, authority, subject, lifecycle precondition, generated strict schema, artifact contract, success postcondition, and retry semantics. Selected actions add their exact execution-route family, preventing read-only runtime continuation or blocker evidence from being presented as payload-bearing lifecycle transitions. Brief publication includes typed unresolved local and cross-boundary starters, canonical byte rules, relational rules, and its fact-validation boundary generated beside the canonical brief records. A boundary-specific starter projection omits the much larger validation schema while retaining one complete unresolved shape and every relational and fact rule needed to fill it. The starters preserve only structural tags; semantic and identity values remain unresolved, so this discovery path cannot invent scope, authority, or verification. Publication validates the candidate record and its accepted Pinboard references, but it does not resolve branch or base-revision declarations against Git or prove source-derived semantic claims.

### Mutations and proposal intake

Each argparse leaf carries its selected parser and either an exact command model or one of the few named coupled-option decoders. The interface converts the raw namespace once, reports structural or coupled-option failures through that leaf parser, and dispatches a closed command union; handlers never receive a general argument namespace or reparse command and operation strings. Command, proposal, and dispatch handlers propagate their closed expected failures to one exhaustive CLI presenter, which owns the stable text and exit status for each family. With explicit JSON output, malformed arguments and typed execution failures instead produce `pinboard-rejected-operation/v1`: stable code, observed facts, mismatches, changed surfaces, retry disposition, and bounded recovery operations. A transition handler parses the opaque CLI capability receipt, selects the matching current advertised action, decodes strict JSON against that concrete variant, and constructs the exact command directly from the selected action and its decoded payload. Project transitions carry task and host attribution supplied by the invoking trusted local task; those strings are audit facts, not authenticated credentials. Preparation and attempt transitions carry their exact lease identity and fencing generation as retained operation authority. No general action discriminator or broad transition-input union enters domain decision code. `application.service` validates that selected action's exact authority and legality again inside the store transaction, then commits one closed exact mutation. When that locked observation rejects, the interface reloads legal same-subject actions and returns their fresh opaque receipts without treating preflight as a reservation. The SQLite adapter exhaustively matches the accepted mutation, explicitly propagates expected stale results from thematic effect functions, advances revision and history, and returns the exact committed mutation receipt rather than making the interface attribute a later snapshot's revision to the earlier change. Proposal intake follows the same SQLite transaction boundary: the proposal file is decoded at the interface, invalid input is returned as a proposal failure, duplicate identities and invalid positions are rejected, and one mutation stores both the immutable proposal facts and a same-identity `intake` work item. Queue positions are one-based and contiguous across live items. Intake appends by default or minimally shifts positions for an explicit insertion. Follow-up candidates depend on their related item; a prerequisite candidate becomes a dependency of its live related item. Intake never creates an attempt or activates work.

After a successful transition, generated views refresh outside the authoritative commit. JSON presentation then reloads current state and includes the affected attempt's derived continuation when available. A view warning or continuation-projection warning cannot undo the committed transition, and callers must not replay that mutation merely because the projection needs a fresh inspection.

Every work item has immutable `work_item_definition_revisions`; the highest contiguous revision is its current accepted definition and the sole owner of title, objective, hypothesis, evidence selectors, scope, non-scope, acceptance criteria, ordered dependencies, effect, and unlock. Proposal intake creates revision 1 from immutable proposal facts. Explicit proposal-acceptance dependencies, prerequisite intake, and `revise-item:<item>` append another complete revision and replace relational dependency rows in the same transaction. Revision decisions compare both expected revision and digest, validate the complete canonical definition, reject missing or cyclic dependencies, and leave lifecycle state untouched. Block operations only confirm dependencies already present in the current definition. Attempts retain the exact definition revision and digest from their accepted brief. Current-scope actions stay unavailable to stale attempts; an active or paused stale attempt can rebind to a matching current-definition brief and corrected Git lineage while preserving its lifecycle state and evidence and fencing worker authority.

A dependency-satisfied ready item may retain one renewable preparation claim pinned to its exact current definition. The item remains `ready` while a preparer compiles and reviews the canonical brief. Ordinary `preparation start` samples its operation time at the boundary, then opens one write transaction and selects the current definition together with either initial acquisition when no claim exists or transfer when the retained claim is inactive. A live conflicting claim or an item that is no longer ready is rejected without change. Exact low-level acquisition and transfer remain supported for recovery and contract diagnosis. Renew and release require the exact preparation token, while revocation records the invoking project task and host. A live claim suppresses conflicting ready-item lifecycle and definition mutations. Activation is advertised only to its exact preparer, validates the accepted brief and definition pin inside the write transaction, consumes the claim, creates the attempt, and moves the item to `active` atomically. Expiry, release, or revocation makes the ready-item operations available again; a later ordinary start repins the current definition.

### Worker dispatch and review publication

`application.dispatch` returns expected selection, review-publication, and final-authority outcomes as typed results. `interfaces.dispatch_brief` composes those operations with artifact verification and prompt rendering, converting its advertised environment, identity, source, review, and prompt rejections into one closed dispatch result. It requires the dispatch environment checkout to match the selected source checkout before decoding the strict record and checking attempt, item, branch, stable checkpoint ID, accepted scope, architecture impact, contracts, authorization bases, verification, reviewed-authority digests, coverage, lifecycle disposition, and independent ready review. Reviewed-authority digests are recomputed from that source checkout rather than the shared repository root. Accepted-scope authorization must match the reselected attempt; source-derived authorization must name one exact reviewed authority family. The independent reviewer verifies that each selected source truthfully serves its claimed product-authority, repository-policy, or existing-consumer role and that each mandatory check's tool, threshold, platform, compatibility obligation, or hardening target is supported by accepted scope or selected source bytes. The stable action JSON field named `effect` classifies lifecycle transition input, not every outer command effect: dispatch may publish or reuse immutable review evidence while leaving item and attempt lifecycle unchanged. Domain code names that narrower fact `LifecycleEffect`; the inspection boundary preserves the existing field and values.

The agent-facing runtime adapter maps the accepted checkout, permission declarations, native subagent launch, waiting, and optional messaging without changing this flow. Pinboard validates the dispatch record but grants no runtime capability and creates no worker.

Ready review evidence is strict JSON bound to the stable checkpoint ID, canonical checkpoint-record digest, and canonical ordered authority-set digest. The interface canonicalizes it, the application publishes and accepts it through the result-shaped artifact capability, and the interface verifies the accepted bytes before rendering the launch prompt. The launch prompt only points the worker to the canonical JSON brief and names the execution environment; it does not duplicate or reinterpret the task contract.

After a candidate is submitted, the read-only `review-job` command requires the exact protected candidate and current review continuation. It verifies and resolves the accepted brief, reads canonical `result.md` bytes from the attempt directory, rejects missing or empty evidence, and emits a bounded fresh-context prompt with the brief and result paths and render-time SHA-256 digests. The reviewer must verify those bytes and the candidate identity immediately before use. Review-job rendering does not publish evidence, mutate lifecycle, create a task, or transfer acceptance authority; the accepted brief's `owner_task_id` remains the exact outcome owner. The Codex runtime creates the subordinate reviewer. If that capability is unavailable, the candidate remains in review as a valid resumable boundary rather than being routed to another user-owned task.

### Checkpoint and terminal acceptance

An accepted nonterminal checkpoint uses the brief's stable checkpoint ID, preserves exact result and review evidence, pauses the same attempt, and fences its worker authority. Checkpoint evidence is published before the locked acceptance transaction. If locked rejection or infrastructure failure follows a newly published immutable revision, the CLI reports a committed-effect outcome naming that surface while the prior ledger relationships remain intact; replay is not presented as an unchanged retry. When review accepts the protected candidate but the current attempt should continue, review acceptance returns the item and attempt to active, clears the protected candidate, preserves the accepted candidate and evidence in the transition receipt, and fences the prior worker authority. Late accepted direction uses these existing operations rather than another lifecycle state: definition revision makes an active attempt stale; review return clears a superseded protected candidate without calling its implementation defective; matching brief publication and rebind accept the revised target without changing active or paused state before another exact candidate and review. Terminal completion records accepted evidence and removes the item from live work only after the human has chosen repository disposition and confirmed completion. Direction after that terminal boundary becomes new work. Review return keeps the same attempt and evidence while fencing the rejected worker lease.

## Stored formats

The default `.codex/pinboard` path is current; explicit work roots remain supported at their selected paths. Proposal JSON uses `pinboard-proposal/v1`; complete definition replacement uses `pinboard-item-revision/v1` containing `pinboard-work-item-definition/v1`; accepted work briefs use `pinboard-work-brief/v2`; independent review evidence uses `pinboard-work-brief-review/v2`; dispatch environments use `pinboard-dispatch/v1`; brief-source manifests and plans use `pinboard-brief-sources/v1` and `pinboard-brief-source-plan/v1`; static command discovery uses `pinboard-agent-tool-contract/v1` and its selected operation, action, and presentation records; work-brief construction uses `pinboard-work-brief-contract/v1`; unsuccessful JSON output uses `pinboard-rejected-operation/v1`; and read projections include `pinboard-item-definition/v1`, `pinboard-item-definition-history/v1`, `pinboard-overview/v2`, and `pinboard-parallel-preview/v1`. The overview orders every live item by its authoritative queue position and carries eligibility, dependency reasons, and review flags without a separate hidden proposal collection. Historical terminal v1 brief artifacts remain opaque immutable evidence rather than a supported input format. Atomic file publication uses private `.pinboard-stage-*` names.

## Keeping this map current

This document describes implemented ownership and dependency direction. Every implementation checkpoint declares its architecture impact before dispatch. A checkpoint that changes an owner or dependency direction names this file as `update-required` and includes the coherent documentation change in the same candidate. A `read-only` checkpoint names the authority it must conform to; `none` records why no architecture change occurs. Typed brief validation enforces the declaration shape, while brief and implementation review verify that the declaration is true for the sources and final diff.

Future behavior belongs here only when its implementation is present. Delivery history, speculative modules, and deferred redesigns remain in private planning evidence until they change the current architecture.
