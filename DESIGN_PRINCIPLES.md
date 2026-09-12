# Design principles

This is the reusable design method for maintainers and coding agents evolving Pinboard. Use it to keep decisions visible, place effects and conversions, choose a typed control-flow shape, and know when structural work is finished. [The architecture map](ARCHITECTURE.md) owns the concrete package and runtime responsibilities; this document owns the constraints used to shape and change them.

## Decisions and evidence

### Optimize for visible decisions

The primary reader must be able to answer what can happen, under which condition, and with which effect. Put those branches in one explicit owner. Move representation conversion and persistence mechanics aside only when their contract remains obvious at the call site.

Preserve independent conditions on neighboring behavior unless accepted scope changes them. Permission for one operation must not enable, disable, or bypass a sibling with a different condition. Test one mixed counterexample where the new behavior is allowed and the sibling remains forbidden.

File size and total lines are separate signals, not objectives. A reduction helps only when it concentrates real alternatives or removes repetition without hiding control flow.

Agent-facing schemas, values, and entry points are product surfaces when agents can use them to steer work. Unless a public API or CLI already makes the contract obvious, keep the consumer, semantic effect or deliberate non-effect, and owner discoverable from the definition or direct entry point. Remove a surface that survives only because a schema can carry it.

When context must survive several reasoning stages, prefer a strict semantic scaffold. Give outcome, provenance, scope, non-goals, acceptance criteria, reviewed sources, verification, and remaining work stable places when those distinctions matter. Validate shape, references, identity, and canonical bytes without claiming that structure proves semantic truth.

Use that context to propose affected code, documentation, architecture, and principles. Reviewed authorities and coverage should reveal likely owners without an exhaustive repository scan; read outward when changed meaning exposes another owner. This mapping remains judgment, confirmed by human acceptance and independent review. Add a hard-coded impact map only for a repository-owned deterministic contract, never as a second source of truth.

### Make architectural limitations explicit

Treat a limitation as an implemented constraint or operating assumption with a practical consequence, not as a synonym for every tradeoff or possible improvement. Classify the condition before deciding what to do:

- An **existing limitation** is already present. Preserve its current architecture entry and prior acknowledgement without interrupting the human again unless this change materially alters its consequence, affected boundary, or reopening condition.
- An **introduced or widened limitation** is created or made materially more restrictive by the proposed change. Evaluate it before implementation, even when the requested feature itself is already authorized.
- A **defect** violates accepted behavior, an invariant, or a supported contract. Fix or report it as a defect; do not relabel broken behavior as a deliberate limitation to obtain acceptance.
- A **speculative risk** has no evidence-backed effect on a supported path. Keep it out of current architecture and mandatory work unless an observable trigger turns it into a real decision.

A limitation is material when its consequence meaningfully changes supported scale, resource growth, correctness, durability, compatibility, deployment, platform support, trust, or the cost of removing a foundational constraint later. Use concrete evidence and the supported product boundary; do not invent numerical thresholds or adversarial scenarios.

Before choosing a local remedy, trace the proposed change to the foundation that creates the pressure. A patch that preserves, masks, or deepens an unacknowledged material limitation still requires the architecture decision even if its own diff is small. For example, avoiding one unnecessary file timestamp change does not settle a command whose underlying state read and publication boundary remains project-wide. A genuinely bounded local correction that leaves the foundation and its consequences unchanged proceeds without a checkpoint.

When a proposed change would introduce, widen, preserve, mask, or deepen an unacknowledged material limitation, the task that owns the outcome pauses before implementation and presents the decision in this order:

**Architecture checkpoint**

1. State the concrete current and proposed behavior, affected supported paths, operational or maintenance consequences, and observable reopening condition.
2. Distinguish what the decision enables from what remains outside accepted scope.
3. Give a recommendation and its evidence.

**Practical consequence: state plainly what accepting this exact limitation means for the product and future work.**

Ask a familiar approval question about that exact decision, such as “Do you want me to proceed with this limitation?” An ordinary explicit answer is sufficient; never require the human to repeat a legalistic acknowledgement phrase. Silence or generic authority for the surrounding task is not acceptance. Approval covers only the stated limitation and does not authorize adjacent features, hardening, or cleanup.

Once accepted, record the limitation as current truth in `ARCHITECTURE.md` in the same candidate that implements or preserves it, including its consequence and reopening condition. Keep the reusable method here and automatic routing in `AGENTS.md`; do not duplicate the limitations inventory or full procedure in those layers. Reopen an accepted limitation only when its recorded consequence or trigger changes materially.

## Boundaries and data flow

### Separate decisions, conversions, and effects

A decision determines whether an operation is legal and what accepted change it describes. It should not read files, issue SQL, obtain time, or depend on a concrete adapter.

A conversion changes representation without changing meaning. Prefer a plain typed function beside the boundary model or in the outer module that knows both representations. It must not acquire resources or perform unrelated work.

An effect reads or changes an explicitly supplied resource. Its signature names that resource and every value that influences the effect. A thematic SQLite function, for example, receives an existing connection and supplied time; it does not secretly open a database or read the clock.

Do not combine these roles merely to save a call. Do not separate them when the new boundary would add more translation machinery than the distinction removes.

### Make effect contracts locally complete

The caller should be able to determine whether a function can mutate state, perform external I/O, end a transaction, obtain ambient values, invoke caller-supplied behavior, or exit normally with an expected rejection.

Prefer these properties:

- resources and time are explicit parameters;
- transaction opening, commit, rollback, and close remain with one visible owner;
- cross-boundary functions return data instead of invoking callbacks;
- pure mapping functions depend only on their arguments;
- module-level contract text states the allowed effects and the effects it deliberately does not own;
- infrastructure and invariant failures are not silently normalized into ordinary outcomes.

Every internal parameter must serve current behavior, validation, conversion, or a required interface. When a parameter has no such consumer, remove it and any transport-only arguments or resource sampling left in its caller chain. A no-op assignment or explanatory comment cannot justify keeping it.

A helper that takes bread and cheese may return a sandwich. It must not also collect the mail, call another service, or decide whether the meal was authorized.

### Keep resource work proportional to the result

Trace data scope end to end: command input, application selection, domain processing, storage reads and writes, generated projections, and returned output. A focused operation names the subject and relationships its result needs, and no intervening layer widens that selection merely because a complete-state API or convenient aggregate already exists. Reads, comparisons, validation, mutations, and file replacement all follow the same semantic scope.

A result that intentionally describes the current portfolio or complete project may scale with that advertised result. Name that wider scope at the entry point and capability boundary, exclude unrelated retained data, and keep the large dimension as narrow as the result permits. Process large selected collections progressively when practical; if full materialization remains deliberate, record its consequence and reopening condition in `ARCHITECTURE.md`.

Do not make one hot file, hidden cache, log, or projection accumulate without a product-owned retention or segmentation decision. Immutable evidence and normalized rows may grow when provenance is the product requirement, but ordinary work must reach them through keys, bounded pages, or explicit whole-project operations. Tests should grow unrelated retained data and observe that focused operations neither read, rewrite, nor republish it.

### Make code and guide tell the same story

Make production code and tests readable by the next coding agent. Names, structure, and local contracts must expose purpose, input provenance, decisions, effects, expected failures, important constraints, and the next owner without author coaching or delivery history. Leave established findings at their owning code or contract, not only in an audit or conversation.

Repair misleading names and hidden control flow before adding prose. Comments are for verified invariants or reasons structure cannot express. Dynamic dispatch, selection tables, callbacks, and exceptions must reveal their wiring, supported alternatives, and exits at the responsible owner; preserve a genuinely open boundary. Consequential unresolved intent stays a human question, not a documented guess.

Reviewers trace the changed path's purpose, decisions, effects, and exits from local implementation and named authorities. Tests name the observable guarantee and violating regression. Prefer fixed inputs and behavioral outcomes; interaction counts require a supported requirement that depends on those interactions and must not preserve obsolete production work.

The product overview and one representative implementation path should tell the same ordered story to a computer-literate reader. Use verbs for work and provenance nouns for values. For a state-changing path, the diagnostic grammar is: decode an exact command, observe context, resolve supplied claims into a requested change, reread locked state, decide legality, project the accepted change, commit it, refresh replaceable views, and present the result. Omit absent stages and combine stages one owner genuinely performs.

Keep distinctions that answer different questions visible. An observed snapshot is not locked state; a supplied claim is not resolved authority; an accepted decision is not a durable commit; a failed view refresh does not undo authority; expected rejection and infrastructure failure leave differently. Names, composition, and resource boundaries should reveal these facts before comments or type inspection.

Names are behavioral promises about provenance, timing, success, durability, authority, and demonstrated capability. Change misleading internal names directly. Stable public, wire, schema, storage, history, or compatibility spellings require truthful boundary translation or an explicit migration or versioning decision. Product and personal identities require human disposition. Keep a naming problem and its reading cost visible until disposition and safe reopening are explicit.

Use named arguments and role-specific names when same-shaped values cross a meaningful boundary. Split a multi-stage function only where the pieces own clear verbs, inputs, effects, and exits; do not manufacture a workflow framework merely to regularize the sequence. Function length, smell categories, duplication counts, and split-by-default style are weak prompts. Restructure when sequence, effect boundary, or next ownership becomes easier to predict, and keep a cohesive longer function when splitting would scatter one responsibility. Never trade clear layer ownership for a smaller function or lower duplication count.

Review guide and code in both directions. Change whichever tells the less accurate story until vocabulary, order, effects, and exits agree. A fresh reader should be able to trace one real path from the overview; if not, repair the earliest misleading owner: code first, current-story documentation second, recurring contributor guidance last.

Ordinary delivery uses one representative path. Repository Readiness offers the stronger repository-wide lens only when explicitly selected: enumerate every supported runtime, agent, documentation, tooling, packaging, CI, and test-evidence surface; derive each distinct narrative shape; reconcile code and documentation; give each shape a plain-language trace and sibling simulation; close its eight semantic receipt categories; then repeat the inventory once. A repository-wide claim requires that fresh pass to find no new candidate.

Story ambiguity does not authorize behavior change. Check accepted requirements, observable tests, and consumers; when intent remains unresolved, name the product question and prefer behavior-preserving structure or vocabulary. Stop ordinary work when the representative path is coherent, matching siblings use the same grammar, and another refactor would only restate a distinction or add ceremony. Use the complete-surface fixed point only for an explicitly selected repository-wide pass.

### Put glue at the outer owner

Dependencies point toward policy. Domain code owns product vocabulary and pure legality. Application code sequences use cases through storage-independent capabilities. Adapters implement those capabilities. Interfaces decode external input, compose concrete implementations, and present results.

When an operation genuinely needs two layers, the outer layer that already knows both owns the conversion or composition. Do not make both inner layers import each other, and do not create a shared module that makes every participant own the cross-dependency.

### Resolve configured resources once

A configured location or concrete effect capability is part of an operation's input even when every current installation happens to use the same value. Repeating a path literal or reconstructing the same adapter in several consumers creates an unnamed ambient dependency: each site can drift, tests can accidentally exercise a different resource, and the real composition boundary becomes difficult to see.

Resolve deployment layout once at the nearest stable outer boundary, construct each effectful capability once for its invocation and lifetime, and pass the exact value inward. Inner workflows receive the narrow configured path, store, repository, connection, or protocol they use; they do not rediscover it from a broader root or instantiate a concrete replacement. State-independent routes should not construct stateful capabilities at all.

Prefer ordinary required parameters and a small immutable configuration record. Do not introduce module globals, mutable singletons, service locators, registries, optional dependency parameters, default constructors, or a dependency-injection framework. Split the composition only when collaborators genuinely have different lifetimes or owners, and keep that distinction explicit at their caller.

### Constrain composition fan-out

Being outermost permits a dependency direction; it does not justify collecting unrelated work. Keep the process entry point as a small composition root that owns only complete input decoding, one exhaustive route, and final result presentation. Put each cross-layer workflow in a thematic outer module named for the use case it composes.

Judge fan-out per module as well as per layer. A thematic composition module may know several concrete collaborators when all of them serve one visible operation. It becomes an architectural octopus when it owns unrelated command families, conversions, resource lifetimes, or presentation rules merely because it is allowed to import them.

Keep composition modules acyclic. Prefer direct module-qualified calls so navigation reveals the exact owner. Mechanically constrain the entry point's outward dependencies and the interface package's cycles; do not rely on directory placement as proof that the graph is clear.

Keep the production dependency graph mechanically checkable. Move uninstalled experiments to test-only prototypes rather than granting production code a reverse dependency for a hypothetical consumer.

## Types and control flow

### Make expected exits explicit

Use a typed result for outcomes a caller can act on: rejected command, proposal, or dispatch input; unavailable action; missing selected work; stale compare-and-set; or rejected lifecycle transition.

A decoder may raise while data is still an untyped external representation. If invalid input is an advertised outcome, the boundary owner catches that exact parser failure and returns the use case's typed failure. A command, proposal, dispatch, or domain rejection does not become exceptional merely because parsing observed it first.

Keep failures exceptional when execution cannot proceed as designed: unreadable accepted internal files outside an advertised rejection contract, SQLite I/O or locking, corrupt persistence, and programming-contract violations. Catch only exceptions owned by an advertised boundary contract and keep the conversion visible; transaction ownership rolls back both expected and exceptional failures.

A custom exception's nearest contract must name the decoding, infrastructure, persisted-invariant, programming-contract, transaction, or cohesive partial-publication failure that prevents continuation. If the immediate caller would catch it only to reconstruct the same returned failure, return that frozen failure directly in the result alias. Use ordinary early returns, not fluent result APIs, decorators, or boolean chains.

### Make impossible states unrepresentable

Supported typed code must not encode impossible states as exceptions, result variants, sentinels, fallbacks, placeholder initializers, coupled optionals, or fabricated malformed values. Use concrete records, nominal identifiers, closed unions, and exhaustive matching so invalid construction is rejected statically or has no callable surface.

A clean strict type check proves only invariants expressed by those types. Do not add guards or tests solely for states supported typed code cannot construct. Runtime validation remains necessary for external input, `Any` or cast boundaries, persisted relationships, filesystem and database effects, concurrency, staleness, and infrastructure failure.

Never fabricate required persistence in a read fallback. Absence is valid only when the owning contract says so; otherwise the reader rejects the missing invariant. The schema initializer owns every row required for a valid empty database and seeds it in the same transaction. Prove new seed invariants from fresh initialization, not populated fixtures.

Validate structured input once in the record that deserializes or converts it. Annotations own field and shape constraints; post-init validation owns same-record cross-field invariants that cannot be declarative. Consumers trust successful conversion. Validate later only when combining independent sources or current external state such as database identity, revision, time, filesystem state, or artifact agreement. Production decodes strict boundary records and converts them to plain domain or application values; it does not reuse direct boundary construction as an internal DTO path.

Test an invariant at the cheapest owner that can disprove it. Another layer earns a test only for distinct wiring, representation, effects, failure handling, concurrency, or compatibility. The same rule applies to signatures: removing a defensive branch is incomplete while an optional, broad union, general authorization value, or test helper still admits the state. Use separate closed variants when required data differs rather than reconstructing the distinction from nullable fields.

### Expose predicate-shaped types before extending them

A **predicate-shaped type** represents one semantic fact only as a boolean wall over fields of a broader record. **Validation accretion** adds another condition after each forgotten field. These are judgment smells. Lossless generic persistence, transport, export, or presentation remains ordinary record handling until it assigns semantic meaning to selected fields.

Before adding another condition to a multi-field semantic predicate, name the type it claims to prove, its owning boundary, the smallest conversion, and the footprint. Make a proportionate local conversion autonomously. Ask for human judgment when it crosses owners, dependency direction, persistence or wire contracts, public behavior, or broad-change thresholds; present current effect, proposed refactor, footprint and risk, and the smaller fallback.

Convert once at the semantic owner. Every source field is validated, carried into the value, or explicitly irrelevant. Pass closed variants downstream so Pyrefly checks construction and exhaustiveness. Keep runtime checks for external or stored facts, independent relationships, current state, concurrency, and production wiring.

Use each tool only for its guarantee. Pyrefly checks the types expressed after the boundary is chosen. Ruff or an opt-in check may surface a shape but cannot find every semantic consumer. Runtime tests prove observable integration. Semantic review identifies likely sites, judges the boundary and footprint, and challenges omissions; none prevents every workaround.

### Prefer closed, direct Python

For a closed family, use concrete records or flat unions, exhaustive `match` statements, and direct function calls. The source should reveal which variant calls which effect.

When a module owns a coherent vocabulary, import the module and qualify its members at use sites. This keeps the owner visible and prevents long member-import lists from disguising cross-module coupling.

Do not replace a closed decision or boundary conversion with a handler registry, reflective attribute access, inheritance-based dispatch, or a callback pipeline merely to shorten the branch. Dynamic dispatch is appropriate when behavior is genuinely open at runtime or the caller should not know the concrete implementation. Generics and protocols are useful when they preserve exact types across a real reusable boundary; they are not reasons to create one.

### Make exhaustive sites earn their place

Exhaustiveness belongs at an owner that must distinguish a real closed family or convert an independently required wire, storage, or presentation shape. It is not a quota for every layer traversed by that family. After one owner has selected a concrete alternative, pass that typed value directly until another owner has a different decision or representation to own.

Stress-test navigation whenever a command or variant family crosses several production owners. Trace one representative value from its supported entry point to its effect, then simulate adding one sibling. Record every place a developer must discover and every place they must edit. A site earns retention when it adds validation, policy, protocol conversion, presentation, or an effect; a route enum, conversion table, wrapper variant, or exhaustive branch that only restates an already selected fact is duplicate ownership.

Use that result to collapse same-meaning remaps and improve names and direct call paths. Do not introduce reflection, registries, callbacks, or polymorphism merely to reduce the number of exhaustive matches. The target is a navigable closed design: each necessary distinction is explicit once per owner, and a developer can predict the next owner without reconstructing a parallel routing system.

Apply the same ownership test to read paths. When neighboring projections repeatedly scan one collection by the same key to reconstruct the same relationship, build one local explicit index and reuse it for that operation. Keep separate traversals when they answer different questions or when sharing the index would give it a broader lifetime or owner than the operation requires.

### Choose dispatch by ownership and failure mode

The primary hazard is implicit fallback, not dynamic dispatch by itself. For every dispatch site, ask whether the alternatives are closed, whether this owner must distinguish them, where a new alternative should force an edit, and what happens for an unsupported value. Reject catch-all `else` branches, mapping `.get()` defaults, optional handlers, inherited default implementations, and generic registrations that silently accept an unknown alternative.

| Situation | Preferred shape | Why | Avoid |
| --- | --- | --- | --- |
| Incoming payload, CLI leaf, storage row, or protocol tag selects a closed representation | Decode directly to an exact record or tagged union; use one exhaustive `match` when coupled fields select among records | Validation and completeness belong at the boundary, and a new supported shape must update that owner | General namespaces past the boundary, stringly route enums, permissive fallback records |
| A closed product decision, effect, persistence projection, or presentation genuinely differs by variant | Exhaustive `match` with `assert_never` in that owner | The branch is the readable specification and fails static checking when the family changes | Handler dictionaries, catch-all branches, repeated matches in pass-through layers |
| A selecting site has already chosen a concrete same-shaped command model | Carry inert type metadata or the typed value and use one common operation | Navigation stays direct without restating the closed choice | Executable callbacks hidden in parser state, a second route enum, name-to-name conversion tables |
| Pure in-module code delegates behavior that the caller should not distinguish | Direct method or function call; a small protocol is acceptable when every concrete implementation is explicitly wired nearby | The behavior owner is one jump away and another exhaustive caller adds no completeness | Base-class fallback behavior, reflective lookup, optional callback defaults |
| A library, plugin, driver, or dependency-inversion seam is intentionally open | Protocol, required callback, abstract interface, or explicit registry at one composition root | Dynamic dispatch expresses the supported extension boundary | A default implementation that makes an unregistered or incomplete implementation appear supported |
| A closed key selects inert data rather than behavior | Prefer an enum-keyed total record or exhaustive function when completeness matters; use a mapping only when it is genuinely data-driven and missing keys fail explicitly | Data tables can be clearer than control flow, but Python mappings do not prove totality | `.get()` fallbacks, default dictionaries, a behavior registry disguised as data |

At Python dynamic seams, make completeness observable: protocols declare the required surface, every supported implementation supplies it explicitly, wiring is centralized and discoverable, and unsupported inputs fail at the boundary. Abstract base methods should not provide a usable fallback body. When those properties cannot be seen locally and the family is closed, prefer an exhaustive branch.

## Structure and stopping

### Split by theme and preserve useful symmetry

Group models, conversions, reads, and effects by the product concept they serve. Corresponding layers should use corresponding themes when the responsibilities genuinely match, so a reader can predict where lifecycle, proposal, artifact, or authority behavior lives.

Symmetry is a navigation aid, not a quota. A layer may keep a specialized module when only that layer has the responsibility. Do not create empty or ceremonial counterparts merely to make directory listings align.

Avoid generic `utils`, `writer`, `manager`, and `handlers` modules. A module earns its name from the concept and contract it owns.

### Budget durable explanation

Reader attention is finite. When adding durable documentation, first identify the reader, the decision or action the material supports, and its smallest authoritative owner. New material should replace, consolidate, or displace lower-value detail at that owner rather than accumulate beside it.

Net growth is justified only when a genuinely new reader need has no existing owner. An exhaustive hybrid document such as the architecture map or this design method preserves every operative semantic distinction, not every paragraph accumulated while discovering it. Semantic completeness may require detail; it does not require repeated rationale, delivery history, or locally complete inventories at each consumer. Do not impose a rigid word or line quota, because compression is not evidence that a contract survived.

Stop when the reader can make the supported decision from one authoritative account and further removal would hide a necessary distinction. Reopen the document when behavior, audience, ownership, evidence, or a material limitation changes; ordinary desire to add context is not enough.

### Collapse to a fixed point

Use a reversible pilot before applying a new decomposition broadly:

1. Identify the decision surface and one repetitive family around it.
2. State the proposed function and module contract, including hidden effects it forbids.
3. Move the smallest representative family and preserve observable behavior.
4. Measure decision visibility, result paths, dependency edges, conversions, and total source separately.
5. Stop if the extraction duplicates legality, adds dynamic indirection, or makes the effect contract harder to see.
6. If it succeeds, apply the same ownership rule to every matching family.
7. Recompute imports, callers, dead variants, tests, and documentation after each collapse.
8. Recount every affected closed family. Treat a one-member vocabulary or variant hierarchy as a cleanup candidate: replace it with the concrete value or result it now represents unless an independently owned external or persisted contract requires that exact singleton shape.
9. Repeat until a fresh pass finds no matching residue.

Stop when another fold would erase a product distinction, scatter one exhaustive decision, create a generic dumping ground, or add more conversion machinery than repeated ownership it removes.

### Evaluate the result on independent axes

Do not use one metric as a proxy for architecture quality. Report at least:

- where the core decisions are and how many places own them;
- which expected outcomes are explicit result paths;
- which boundary conversions remain and where they live;
- production dependency direction and import fan-out;
- source lines by thematic owner and in total;
- observable behavior, concurrency, rollback, and fresh-reload evidence.

A successful change may reduce the orchestration file while increasing explicit result propagation. That trade is acceptable only when the new lines make control flow or ownership clearer and no smaller plain-Python expression preserves the same guarantees.
