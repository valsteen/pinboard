---
name: slop-cleanup
description: Remove code or features cleanly by finding and collapsing every orphaned production path, test-only API, stale variant, archaeological name, document, dependency, and tool. Use for any code cleanup that deletes behavior or structure, scaling from a local residue check to a repository-wide fixed-point pass. Do not use for additive work, formatting-only changes, routine prose corrections, or a general repository-readiness assessment.
---

# Clean repository slop recursively

Turn accumulated implementation residue into deliberate cleanup whose deletions, retained exceptions, and stopping condition remain reviewable. Treat this as product-scope recovery as well as dead-code removal: a well-tested subsystem can still be premature, abandoned, or unreachable from the supported product.

## Scale every code removal

For every code cleanup that removes production behavior or structure, use this skill or explicitly perform its residue pass before completion. Trace the removed concept through producers, consumers, stored state, commands, variants, tests, documentation, generated artifacts, dependencies, and tooling, then recurse through anything newly orphaned.

Scale the pass to the deletion. For one known symbol or a localized refactor, inspect adjacent references and affected surfaces, run targeted checks, and stop when another local pass finds no new residue. For a feature, subsystem, abandoned implementation, or repeatedly revised design, use the production-root inventory and repository-wide fixed-point workflow below.

## Start from current product authority

1. Inspect current requirements, product documentation, supported entry points, and live work before creating cleanup work. Do not infer current scope from old plans, branches, transcript memory, or code archaeology alone.
2. Reuse an existing exact cleanup objective when it already owns the cleanup candidate. Otherwise create one cleanup objective and preserve the accepted decisions in the project’s chosen planning system.
3. Do not absorb cleanup into active work unless its accepted scope already covers that cleanup. Preserve discovered prerequisites or adjacent concerns through the project’s normal planning workflow.
4. Treat planning authority separately from deletion authority. Inspection and a cleanup plan do not authorize removing ambiguous features, migrating live data, or burning a compatibility bridge.

## Establish the production truth

Define the supported production roots for this repository before classifying anything. Include installed executables, documented public APIs and package exports, runtime registrations, plugin or framework entry points, configuration-driven routes, build/package contents, external protocols, and readers required by supported persisted data. Account explicitly for reflection, generated code, dependency injection, or other dynamic reachability. Treat an explicit product-authority statement that an interface is unsupported as decisive; do not keep hypothetical undisclosed consumers alive after the project owner has ruled them out. Ask only when current authority is silent or contradictory.

Trace both directions:

- From each production root, trace inward to the code and state it can exercise.
- From each stored state, command, variant, branch, or feature, identify a real production producer and a real production consumer.

Tests, fixtures, examples, type references, deserializers, and documentation are not production producers. A current persistence reader can still be required when supported data exists even if no current command creates new values. Conversely, tests that seed data plus production code that only reads or rejects it can form a self-supporting dead island; references alone do not prove a feature is reachable.

Treat comments written by humans or agents as claims to verify, including confident explanations for retaining code. Trace the claimed purpose to current behavior, an accepted requirement, a real consumer, or a required interface, then use the cheapest observation that could disprove it. Historical intent and a plausible explanation do not establish a current need; correct or remove the explanation when the evidence contradicts it.

For every candidate, record:

- the supported entry path, producer, and consumer, or their absence;
- current persisted-data or external-protocol responsibility;
- the first known introduction and intended product effect;
- any accepted or deferred requirement or work item that still needs it;
- documentation, third-party dependencies, build configuration, and CI support likely to become orphaned if it is removed;
- the proposed disposition and evidence that could falsify it.

Use runtime observations and real stores when they cheaply distinguish a required compatibility reader from empty scaffolding. Zero rows strengthen a case but do not by themselves prove that a product capability is unwanted.

## Audit semantic maintenance cost

When auditing semantic ownership, copied settings or evidence, or unexplained change amplification, read [references/semantic-audit.md](references/semantic-audit.md). It owns candidate categories, dispositions, canonical inputs versus independent expectations, the representative evidence trace, sibling-change simulation and semantic stopping conditions. Use it in the production-root inventory for a broad cleanup; a known local deletion needs only its relevant residue checks.

Apply the target project's current design authority when one exists. The reference remains self-contained when it does not; Slop Cleanup requires neither a project design guide nor Pinboard.

Mechanical matches are leads. Settle each candidate through its real producer, consumer, retained responsibility and accepted authority. Keep consequential unresolved decisions visible rather than treating them as retained exceptions or a fixed point.

## Keep the inventory bounded and resumable

For a repository-wide pass, read [references/reachability-inventory.md](references/reachability-inventory.md). Run its bundled inventory with the repository's existing analyzers. On a Python repository, run the same inputs once in generic mode and once with Python AST enrichment; compare their coverage and candidates so language-specific precision does not conceal what the portable pass can and cannot establish. For every category without analyzer coverage, use the language-portable fallback with a concrete extractor that understands the repository's actual syntax, and produce the same receipt rather than weakening the stopping condition. The inventory generates candidates and explicit coverage receipts, not semantic reachability by itself.

Keep one compact private ledger outside tracked product files unless the user requests a durable audit artifact. Record production roots, dynamic mechanisms, completed inventory passes, per-category coverage receipts, atom-level producer/consumer evidence, candidate selectors, dispositions, retained exceptions, and the next unresolved batch. Store selectors and short evidence summaries instead of full source listings or raw analyzer output. Inspect candidate bodies in coherent batches and update the ledger after each batch.

If context is compacted or work resumes later, re-ground from current product authority, production roots, and this ledger. Do not replay completed scans unless the code or relevant authority changed. After a coherent removal, rerun the affected inventories; reserve one complete inventory for the final fixed-point pass.

## Answer provenance challenges honestly

When the user asks “When did I ask for this?” or challenges a feature’s origin, trace the claim to primary evidence. Search exact user-authored messages or transcripts, accepted requirements and decision records, the project’s work tracker, then version-control introduction history and blame. Give the date, durable selector or task link, and a short exact excerpt when available.

Classify the result as one of:

- explicitly requested by the user;
- present in an accepted requirement but not found in a user-authored request;
- introduced as implementation inference, design proof, migration support, or temporary scaffolding;
- inherited from an older product state;
- provenance not found.

Never say the user asked for something based only on code, tests, a commit message, or agent-authored planning text. Absence of provenance is not proof that code is dead, and historical provenance is not a reason to retain code that no longer serves the current product.

## Let the user choose ambiguous product intent

Translate each ambiguous subsystem into its current user scenario. Explain how someone would reach it today, what it preserves, and what would be lost by removing it. Group candidates that share one product decision, then ask one concrete question.

When bounded tracing cannot settle a consequential retention or removal claim, flag it as requiring a human decision. State the exact claim, evidence checked, missing evidence, and the practical consequence of retaining or removing the code; ask the smallest question that resolves it. Keep that candidate unresolved rather than silently treating its comment as proof, and continue independent cleanup that the evidence supports.

Use these dispositions:

- **Delete:** no supported entry path, current data responsibility, external contract, or accepted future owner remains.
- **Isolate:** the code is useful evidence for a deferred feature but has no shipping entry point. Move it outside the installed product into an explicit prototype, experiment, or test-support area. Give it a purpose header and dedicated tests, and prove packaging excludes it.
- **Retain as an exception:** a real production, persistence, protocol, or explicit user-owned reason exists. Record the exact current reason and reopen condition; do not call it generally reachable.
- **Productize separately:** the user wants the feature. Create product work for a supported entry point rather than disguising feature completion as cleanup.

## Build the cleanup plan

Prefer one cleanup objective with dependency-ordered checkpoints. Split out separate work only when it represents an independently valuable product decision, a data migration with distinct authority, or an outcome that can genuinely complete on its own.

The durable plan must state:

- the current product effect and supported production roots;
- the exact deletion, isolation, and retained-exception decisions already made;
- the cheapest falsifying observations;
- persisted-data, compatibility, packaging, and migration boundaries;
- the concepts and owners likely to change;
- behavior-preservation checks for surviving product paths;
- the recursive fixed-point stopping condition below.

Order checkpoints around semantic dependencies, not file count:

1. Confirm provenance, reachability roots, current data, and user dispositions.
2. Isolate valuable not-yet-production evidence so it cannot keep production machinery alive.
3. Remove unwanted entry points and complete producerless or consumerless feature families.
4. Recurse through newly orphaned models, variants, persistence, serializers, tests, documentation, dependencies, lockfile entries, build and release configuration, and CI tooling.
5. Collapse and regroup the surviving structure, then rerun the complete compact inventory and inspect only new or changed candidates.

Do not freeze the initial candidate list as the whole scope: recursive discovery is part of the accepted outcome. Keep newly exposed work inside the cleanup objective only when it is a direct dependent of an approved removal. Preserve materially different product decisions as separate concerns.

Use the project’s existing planning system when it has one; otherwise keep a proportionate repository plan with the same decisions, dependencies, evidence, and stopping condition. If the user chooses Pinboard, read [references/pinboard-planning.md](references/pinboard-planning.md) and translate the plan and recursive dispositions into the canonical typed JSON brief; treat its generated Markdown view as read-only output. Do not require Pinboard or read that reference for a standalone cleanup.

## Remove one coherent family at a time

For each approved family:

1. Remove or isolate its outermost unsupported entry points.
2. Recompute production reachability immediately.
3. Follow the orphan chain through commands and arguments, state producers and consumers, records and fields, closed-family members, vocabulary values, branches, serializers, schema, readers and writers, error codes, helpers, tests, fixtures, examples, documentation, skills, package exports, direct and transitive dependencies, lockfiles, build and release configuration, and CI workflows.
4. Delete tests whose only purpose was proving deleted implementation machinery. Preserve or add tests only for observable surviving behavior, boundaries, migrations, and rejection contracts.
5. Run the cheapest relevant checks before continuing to the next family. A coverage drop can expose production code whose implementation-only tests disappeared; investigate that code instead of manufacturing tests or weakening the threshold.

Stop and return to the user when recursion reaches a different feature, supported persisted data, an external compatibility promise, or an ambiguous product choice.

For every surviving direct third-party dependency, identify a current production, test, validation, build, packaging, or CI consumer. Remove a dependency when its last supported consumer disappears, then regenerate the lockfile through the repository's package manager so orphaned transitive packages also leave. Do not retain a library, service integration, action, or setup tool merely because an abandoned feature once needed it.

Treat CI as executable product support. Every job, matrix entry, service, secret, permission, cache, generator, release step, and external action must protect a supported platform, package, entry point, or required repository check. Remove pipeline paths that only build, test, publish, or provision deleted behavior. Preserve assurance for surviving behavior; cleanup is not authority to weaken required evidence or platform coverage.

## Collapse the structure left behind

After removal changes the graph, inspect the surviving alternatives, wrappers, boundary representations, parameters and thematic ownership. When those structures need consolidation, read [references/structural-consolidation.md](references/structural-consolidation.md) for the complete residue inventory and repeatable folding procedure.

Regroup around current concepts, remove archaeological names and explanations, and rerun affected inventories after each fold. Preserve distinctions with independent consumers and required external or persisted shapes. Stop when another fold would erase a product distinction or add more conversion machinery than it removes.

## Handle persisted-state removal as a bridge burn

Before deleting a stored family or compatibility path, enumerate every known supported store and external consumer. When migration is authorized, prefer a temporary isolated migrator, recoverable backups, pre/post semantic equivalence, atomic replacement, verification through the installed product, and deletion of the migrator and obsolete compatibility code in the same cleanup outcome. Do not leave permanent archaeology for a predecessor that has no supported users.

Abort the bridge burn if live data, active authority, an unknown consumer, or a changed precondition violates the accepted migration contract.

## Converge to a fixed point

After the last change, rerun the complete inventory from the production roots. Finish only when one fresh full pass produces no new candidates and all of these are true:

- every production definition is accounted for by a supported root, a verified dynamic mechanism, supported persisted data or protocol compatibility, or one exact retained exception;
- every closed variant and branch has a real producer and consumer or an explicit boundary reason;
- routes affected by removal contain no pass-through owner, duplicated decision, or obsolete alternative that existed only for the deleted family;
- no production API exists only for tests, and no advertised entry point can only reject;
- surviving files and tests have meaningful, preferably symmetric conceptual ownership;
- names, comments, docs, examples, diagrams, schema labels, and suppressions affected by the cleanup describe only current behavior;
- every direct dependency and CI path has a named surviving consumer or assurance role, and regenerated lockfiles contain no packages retained solely by removed direct dependencies;
- package-content, behavior, persistence, type, lint, and repository metadata checks pass for the changed surface.

Report the deleted families, isolated prototypes, retained exceptions, migrations and recoverability, provenance conclusions the user asked for, and the evidence for the clean final pass. Do not hide unresolved candidates behind the phrase “fixed point.”

If the surviving repository remains difficult to understand or change safely after cleanup, offer `$repository-readiness` as a separate optional assessment when that skill is available. Ordinary cleanup completes without it; do not turn the deletion fixed point into a readiness prerequisite.
