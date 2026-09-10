# Design principles

This document describes how Pinboard keeps decisions visible while moving repetitive boundary work out of their way. `ARCHITECTURE.md` owns the concrete package map and runtime responsibilities. This document owns the method used to shape and evolve that map.

## Optimize for visible decisions

The primary reader is a person trying to answer: what can happen here, under which condition, and with which effect? Put those branches together in one explicit owner. Move representation conversion and mechanical persistence details aside only when their contract remains obvious from the call site.

When extending behavior, preserve the independent conditions on that behavior and its neighbors unless accepted product scope explicitly changes them. A new permission for one operation must not silently enable, disable, or bypass a sibling governed by a different condition. The cheapest proof is usually one mixed counterexample where the new behavior is allowed while the independently gated sibling remains forbidden.

File size is a signal, not the objective. A smaller orchestration file is useful when it concentrates the real alternatives. A lower total line count is useful when it removes repetition without hiding control flow. Measure those outcomes separately.

Treat agent-facing schemas, values, and entry points as product surfaces when agents can adopt them to steer work. Unless a public API or CLI already makes the contract obvious, keep the intended consumer, semantic effect or deliberate non-effect, and owner locally discoverable from the definition or its direct entry point. Remove an unused or unclear surface rather than preserving a value that survives only because a schema can carry it.

When an agent-facing artifact must survive several reasoning stages, prefer a strict semantic scaffold to an unstructured prose packet. Give outcome, provenance, scope, non-goals, acceptance criteria, reviewed sources, verification, and remaining work stable named places when those distinctions matter. Mechanically validate required shape, cross-references, identity, and canonical bytes. Do not claim that a validator can prove the prose under a label is semantically true.

Let the agent use that structured context to propose which code, documentation, architecture, or durable principles a decision affects. Reviewed authorities and coverage should make likely owners discoverable without requiring every change to restart an exhaustive repository scan; read outward when the new prose exposes an owner the brief did not anticipate. That cross-surface mapping remains reasoning, not enforcement: the model can miss or invent a connection, and human acceptance and independent review confirm whether the relationship is real. Add a hard-coded impact map only when the repository owns an actual deterministic contract; otherwise it replaces useful judgment with a second source of truth.

## Make architectural limitations explicit

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

## Separate decisions, conversions, and effects

A decision determines whether an operation is legal and what accepted change it describes. It should not read files, issue SQL, obtain time, or depend on a concrete adapter.

A conversion changes representation without changing meaning. Prefer a plain typed function beside the boundary model or in the outer module that knows both representations. It must not acquire resources or perform unrelated work.

An effect reads or changes an explicitly supplied resource. Its signature names that resource and every value that influences the effect. A thematic SQLite function, for example, receives an existing connection and supplied time; it does not secretly open a database or read the clock.

Do not combine these roles merely to save a call. Do not separate them when the new boundary would add more translation machinery than the distinction removes.

## Make effect contracts locally complete

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

## Keep resource work proportional to the result

Trace data scope end to end: command input, application selection, domain processing, storage reads and writes, generated projections, and returned output. A focused operation names the subject and relationships its result needs, and no intervening layer widens that selection merely because a complete-state API or convenient aggregate already exists. Reads, comparisons, validation, mutations, and file replacement all follow the same semantic scope.

A result that intentionally describes the current portfolio or complete project may scale with that advertised result. Name that wider scope at the entry point and capability boundary, exclude unrelated retained data, and keep the large dimension as narrow as the result permits. Process large selected collections progressively when practical; if full materialization remains deliberate, record its consequence and reopening condition in `ARCHITECTURE.md`.

Do not make one hot file, hidden cache, log, or projection accumulate without a product-owned retention or segmentation decision. Immutable evidence and normalized rows may grow when provenance is the product requirement, but ordinary work must reach them through keys, bounded pages, or explicit whole-project operations. Tests should grow unrelated retained data and observe that focused operations neither read, rewrite, nor republish it.

## Make code and guide tell the same story

Make code ready for the next coding agent. This applies to production code and tests: names, structure, and necessary local contracts must reveal purpose, input provenance, decisions, effects, expected failures, important constraints, and the next behavior owner. A reader should be able to follow the current implementation without author coaching or reconstructing delivery history. Investigation during a change should leave the established understanding at its owning code or local contract, not only in a private audit or conversation.

Repair misleading names and hidden control flow before adding explanatory prose. Use comments for a verified invariant or non-obvious reason that structure cannot express clearly; check their claims against current behavior and supported consumers. When dynamic dispatch, selection tables, callbacks, or exceptions obscure possible paths, expose their wiring, supported alternatives, and exits at the responsible owner. Preserve a justified open boundary rather than forcing it into a closed branch. Flag consequential unresolved intent for a human decision instead of documenting a guess as fact.

Reviewers must demonstrate that the next agent can read the changed code by tracing its purpose, decisions, effects, and exits from the local implementation and named authorities. For tests, explain the observable guarantee and the regression that would violate it. Prefer fixed inputs and behavioral outcomes; an interaction-count assertion requires a named supported requirement that depends on those interactions, and must not preserve otherwise obsolete production work.

Treat explanatory code as part of the delivered product. A computer-literate reader should be able to start from the product overview, enter one representative implementation path, and retell the same ordered story without first learning Python's type system or guessing what a noun-shaped helper might do.

Use verbs for work and provenance nouns for values. For a state-changing path, a useful reading grammar is: decode an exact command, observe context, resolve caller-supplied claims into a requested change, reread locked current state, decide legality, project the accepted change, commit it, refresh replaceable views, and present the committed result. This is a diagnostic vocabulary, not a mandatory pipeline. Omit absent stages, combine stages that one owner genuinely performs together, and use the repository's own architectural language.

Keep distinctions visible when they answer different questions. An observed snapshot is not locked current state. A supplied claim is not resolved authority. An accepted decision is not yet a durable commit. A failed replaceable-view refresh does not undo an authoritative change. Expected rejection and infrastructure failure leave through different paths. Names, local composition, and explicit resource boundaries should reveal those facts before comments or type inspection are needed.

Treat names as behavioral promises. Read them for provenance, timing, success, durability, authority, and demonstrated capability; mechanical symmetry does not make a false promise truthful. A misleading internal name can change directly. A stable public, wire, schema, storage, history, or compatibility spelling needs a truthful boundary translation or an explicit migration or versioning decision. A deliberate product or personal identity requires human disposition. In every case, keep the finding and its reading cost visible until its disposition and safe reopening condition are explicit.

When several same-shaped values cross a meaningful boundary, prefer named arguments and role-specific local names. When one function performs several conceptually different stages, split it only where the resulting functions own clear verbs, inputs, effects, and exits; do not manufacture a workflow framework to make the sequence look regular.

Treat function length, a linter's smell category, duplicate-detector output, and split-by-default style as weak prompts. Restructure when a different composition makes the product sequence, effect boundary, or next owner easier to predict. Keep or merge a longer cohesive function when splitting it would distribute one responsibility or force the reader to reconstruct its order across files. Never trade clear layer ownership for a locally smaller function or a lower duplication count.

Review the guide and code in both directions. The guide is not automatically right, and it must not become a missing manual for opaque code. Change whichever side tells the less accurate story until their vocabulary, order, effects, and expected exits agree. Then ask a fresh reader to read the overview and trace one real path without coaching. If they cannot say what happens next and why, repair the earliest misleading owner: code structure or vocabulary first, current-story documentation second, and durable contributor guidance only for a recurring method.

Use that representative path for ordinary feature delivery. Repository Readiness packages the same story test as an optional diagnostic lens. When a human explicitly chooses its repository-wide mode, enumerate every supported runtime, agent, documentation, tooling, packaging, CI, and test-evidence surface and derive every distinct narrative shape from their actual responsibilities. Reconcile code and documentation in both directions for the complete set, give each shape one plain-English trace and sibling simulation, close the developer-navigation method's eight semantic receipt categories separately, and repeat the full inventory once after repairs. A repository-wide claim requires that fresh pass to find no new candidate; a polished representative path is not a substitute.

Story ambiguity is not authority to change behavior. Check accepted requirements, observable tests, and existing consumers before calling an unusual sequence a defect. When intent remains unresolved, name the product question and prefer a behavior-preserving structural or vocabulary repair until the product owner decides.

Stop ordinary delivery when the representative path reads coherently, sibling paths use the same grammar where their responsibilities match, and another refactor would only restate an already visible distinction or impose ceremony that the product does not need. Use the stronger complete-surface fixed point only for an explicitly selected repository-wide pass.

## Put glue at the outer owner

Dependencies point toward policy. Domain code owns product vocabulary and pure legality. Application code sequences use cases through storage-independent capabilities. Adapters implement those capabilities. Interfaces decode external input, compose concrete implementations, and present results.

When an operation genuinely needs two layers, the outer layer that already knows both owns the conversion or composition. Do not make both inner layers import each other, and do not create a shared module that makes every participant own the cross-dependency.

## Resolve configured resources once

A configured location or concrete effect capability is part of an operation's input even when every current installation happens to use the same value. Repeating a path literal or reconstructing the same adapter in several consumers creates an unnamed ambient dependency: each site can drift, tests can accidentally exercise a different resource, and the real composition boundary becomes difficult to see.

Resolve deployment layout once at the nearest stable outer boundary, construct each effectful capability once for its invocation and lifetime, and pass the exact value inward. Inner workflows receive the narrow configured path, store, repository, connection, or protocol they use; they do not rediscover it from a broader root or instantiate a concrete replacement. State-independent routes should not construct stateful capabilities at all.

Prefer ordinary required parameters and a small immutable configuration record. Do not introduce module globals, mutable singletons, service locators, registries, optional dependency parameters, default constructors, or a dependency-injection framework. Split the composition only when collaborators genuinely have different lifetimes or owners, and keep that distinction explicit at their caller.

## Constrain composition fan-out

Being outermost permits a dependency direction; it does not justify collecting unrelated work. Keep the process entry point as a small composition root that owns only complete input decoding, one exhaustive route, and final result presentation. Put each cross-layer workflow in a thematic outer module named for the use case it composes.

Judge fan-out per module as well as per layer. A thematic composition module may know several concrete collaborators when all of them serve one visible operation. It becomes an architectural octopus when it owns unrelated command families, conversions, resource lifetimes, or presentation rules merely because it is allowed to import them.

Keep composition modules acyclic. Prefer direct module-qualified calls so navigation reveals the exact owner. Mechanically constrain the entry point's outward dependencies and the interface package's cycles; do not rely on directory placement as proof that the graph is clear.

Keep the production dependency graph mechanically checkable. Move uninstalled experiments to test-only prototypes rather than granting production code a reverse dependency for a hypothetical consumer.

## Make expected exits explicit

Use a typed result when a caller can reasonably act on an expected outcome. Examples include rejected command, proposal, and dispatch requests; unavailable actions; missing selected work; stale compare-and-set writes; and rejected lifecycle transitions.

A low-level decoder may raise while data is still an untyped external representation. When invalid input is an advertised outcome of an installed use case, its boundary owner catches that exact parser failure and returns the use case's typed failure. Do not give a nominal command, proposal, dispatch, or domain rejection exception status merely because a parser first observed it.

Let failures remain exceptions when execution cannot proceed as designed. Examples include unreadable accepted internal files outside an advertised rejection contract, SQLite I/O and locking failures, corrupt persisted relationships, and violated programming contracts.

Do not catch every exception and convert it to a result. Catch only the exact boundary exceptions owned by an advertised failure contract. Do not use a decorator or framework to hide that conversion. Return and propagate expected failures in ordinary typed control flow; let genuine infrastructure and programming failures remain visibly exceptional. The transaction owner rolls back both categories.

A custom exception needs more than an effectful implementation site. Its nearest contract must identify the decoding, infrastructure, persisted-invariant, programming-contract, transaction, or cohesive partial-publication failure that prevents normal continuation. If an immediate caller would catch the exception only to reconstruct the same returned failure, return that frozen failure directly and include it in the function's result alias. Keep short-circuiting visible with ordinary early returns; do not replace them with a fluent result API, decorator, or boolean chain.

## Make impossible states unrepresentable

Supported typed code must not represent impossible states through exceptions, result variants, sentinels, fallback branches, placeholder initializers, optional-parameter coupling, or tests that fabricate malformed typed values. Encode the valid combinations in concrete records, nominal identifiers, closed unions, and exhaustive matching so an invalid construction or call is rejected statically or has no callable surface.

A clean strict type check is evidence for invariants expressed by those types. Do not add runtime guards or malformed-object tests solely to execute a state that supported typed code cannot construct. Runtime validation and failure surfaces remain necessary where the fact is genuinely runtime-owned: deserialization and other external input, `Any` or cast boundaries, persisted relational state, filesystem and database effects, concurrency and staleness, and infrastructure failure.

Do not fabricate required persisted state in a read fallback. A fallback is valid only when absence is an explicit supported value in the owning contract; otherwise the read boundary rejects the missing invariant. Making malformed storage appear usable merely postpones the failure until a writer relies on the fact that the reader concealed.

The database initialization owner that creates a schema also owns every row required for that schema's valid empty state and seeds those rows in the same initialization transaction. When a schema change or feature introduces required seed data, update that owner and prove the invariant from fresh initialization rather than relying on populated fixtures.

Validate structured input once, in the boundary record that deserializes or converts it. Put field-local and shape constraints in its annotations, and put invariants among fields of that record in its post-init validation when they cannot be expressed declaratively. After successful conversion, consumers trust those facts instead of repeating them. Later validation is justified when it combines independent sources or current external state, such as database identity or revision, time, filesystem state, or agreement between artifacts.

Production code decodes or converts strict boundary records; it does not manually instantiate them as internal data-transfer objects. Decoder constraints are part of the boundary contract, so direct construction can create a value without proving the same facts. Use plain domain or application records for internal values after the boundary conversion.

Test an invariant at the cheapest owner whose failure can disprove it. Do not mirror the same fact through unit, command, persistence, and presentation tests merely because a value crosses those layers. Another test earns its cost only when it proves distinct integration wiring, representation, effects, failure handling, concurrency, or compatibility; rely on existing coverage for unchanged downstream behavior.

Apply this rule to signatures as well as implementations. Removing a defensive branch is incomplete while an optional parameter, broad input union, general authorization value, or fabricated test helper still admits the invalid combination. Preserve product distinctions with separate closed variants when their required data differs; do not recover the same distinction later with nullable fields or repeated validation.

## Expose predicate-shaped types before extending them

A **predicate-shaped type** appears when one semantic fact is represented only by a boolean wall over fields of one broader record. **Validation accretion** appears when successive forgotten-field fixes add one more condition to that wall. These are judgment smells, not mechanically complete classifications. Generic code that losslessly persists, transports, exports, or presents the complete record remains ordinary record handling until it assigns semantic meaning to selected fields.

When an implementer or reviewer is about to add one more condition to an existing multi-field semantic predicate, stop before silently extending it. Name the exact semantic type that the predicate claims to prove, identify its owning boundary, and estimate the smallest conversion plus its footprint. Perform a bounded local conversion autonomously when it is proportionate. Ask for human judgment before the refactor materially crosses owners, dependency direction, persistence or wire contracts, public behavior, or the repository's existing broad-change thresholds. Present the current effect, the proposed refactor, the estimated footprint and risk, and the smaller fallback; do not ask for abstract permission.

When an exact conversion is chosen, convert once at the owning semantic boundary. Give every current source field one visible disposition: validate it, carry it into the semantic value, or explicitly mark it irrelevant to that fact. Pass exact closed variants downstream so Pyrefly can check complete construction and exhaustive handling. Preserve runtime checks where facts remain owned by stored or external data, relationships among independent values, current state, concurrency, or production wiring.

Use tooling only for the guarantee it can truthfully provide. Pyrefly checks the exact types expressed after the boundary is chosen. Ruff or a small opt-in local check may surface a suspicious shape or verify a chosen converter, but it does not discover every semantic consumer. Runtime tests prove observable boundary and integration behavior. Semantic review recognizes likely predicate-shaped sites, judges the proposed boundary and footprint, and challenges omissions; none of these layers makes deliberate or accidental workarounds impossible.

## Prefer closed, direct Python

For a closed family, use concrete records or flat unions, exhaustive `match` statements, and direct function calls. The source should reveal which variant calls which effect.

When a module owns a coherent vocabulary, import the module and qualify its members at use sites. This keeps the owner visible and prevents long member-import lists from disguising cross-module coupling.

Do not replace a closed decision or boundary conversion with a handler registry, reflective attribute access, inheritance-based dispatch, or a callback pipeline merely to shorten the branch. Dynamic dispatch is appropriate when behavior is genuinely open at runtime or the caller should not know the concrete implementation. Generics and protocols are useful when they preserve exact types across a real reusable boundary; they are not reasons to create one.

## Make exhaustive sites earn their place

Exhaustiveness belongs at an owner that must distinguish a real closed family or convert an independently required wire, storage, or presentation shape. It is not a quota for every layer traversed by that family. After one owner has selected a concrete alternative, pass that typed value directly until another owner has a different decision or representation to own.

Stress-test navigation whenever a command or variant family crosses several production owners. Trace one representative value from its supported entry point to its effect, then simulate adding one sibling. Record every place a developer must discover and every place they must edit. A site earns retention when it adds validation, policy, protocol conversion, presentation, or an effect; a route enum, conversion table, wrapper variant, or exhaustive branch that only restates an already selected fact is duplicate ownership.

Use that result to collapse same-meaning remaps and improve names and direct call paths. Do not introduce reflection, registries, callbacks, or polymorphism merely to reduce the number of exhaustive matches. The target is a navigable closed design: each necessary distinction is explicit once per owner, and a developer can predict the next owner without reconstructing a parallel routing system.

Apply the same ownership test to read paths. When neighboring projections repeatedly scan one collection by the same key to reconstruct the same relationship, build one local explicit index and reuse it for that operation. Keep separate traversals when they answer different questions or when sharing the index would give it a broader lifetime or owner than the operation requires.

## Choose dispatch by ownership and failure mode

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

## Split by theme and preserve useful symmetry

Group models, conversions, reads, and effects by the product concept they serve. Corresponding layers should use corresponding themes when the responsibilities genuinely match, so a reader can predict where lifecycle, proposal, artifact, or authority behavior lives.

Symmetry is a navigation aid, not a quota. A layer may keep a specialized module when only that layer has the responsibility. Do not create empty or ceremonial counterparts merely to make directory listings align.

Avoid generic `utils`, `writer`, `manager`, and `handlers` modules. A module earns its name from the concept and contract it owns.

## Collapse to a fixed point

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

## Evaluate the result on independent axes

Do not use one metric as a proxy for architecture quality. Report at least:

- where the core decisions are and how many places own them;
- which expected outcomes are explicit result paths;
- which boundary conversions remain and where they live;
- production dependency direction and import fan-out;
- source lines by thematic owner and in total;
- observable behavior, concurrency, rollback, and fresh-reload evidence.

A successful change may reduce the orchestration file while increasing explicit result propagation. That trade is acceptable only when the new lines make control flow or ownership clearer and no smaller plain-Python expression preserves the same guarantees.
