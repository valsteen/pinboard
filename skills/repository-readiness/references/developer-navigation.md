# Trace developer navigation

Use this lens when a command or closed-variant family crosses several production owners, or when dynamic dispatch makes the next implementation owner difficult to predict. It audits structural navigation and repeated routing ownership. For the separate human-comprehension pass that compares a product overview with the story told by code, use [storytelling readability](storytelling-readability.md).

In representative mode, trace one supported value and simulate one sibling. In explicitly selected whole-repository mode, repeat the method for every distinct routing shape and complete the category-by-category surface inventory below. A representative path may establish one shape but cannot stand for the complete surface.

For whole-repository work, inventory every supported product, agent, documentation, tooling, packaging, CI, and test-evidence surface first. Derive every distinct routing shape from that inventory, then require one English-only trace and one sibling simulation per shape.

## Trace one supported value

Choose a representative command or variant with a real production entry point and effect. Follow it through boundary validation, product decisions, representation conversions, effects or persistence, and presentation. At every site, classify the owned responsibility or mark it as same-meaning pass-through or remapping. Names, imports, and direct calls should make the next owner predictable.

Repeat for another family only when it has a distinct routing shape, such as state mutation versus read projection or direct versus borrowed authority. Stop adding traces when a new shape exposes no new owner category. After a concrete alternative is selected, carry the exact typed value directly until another owner has a different decision or representation to own.

## Audit what names promise

In representative mode, inspect the names on the traced path. In whole-repository mode, build a complete name surface instead of sampling identifiers. For each in-scope name, ask what an ordinary English reading claims about provenance, timing, success, durability, authority, capability, intelligence, autonomy, universality, prestige, and identity. Compare that claim with the responsibility the implementation, boundary, or product actually demonstrates. Symmetric spelling across sibling paths does not clear a name whose meaning is false in both places.

Record every mismatch with its selector, claimed meaning, demonstrated responsibility, mismatch, normal reading cost, affected consumers, and evidence. Then use the [storytelling readability](storytelling-readability.md) classification and disposition method. A stable or identity-sensitive name remains a reported finding even when immediate rename would be unsafe.

## Generate candidates mechanically

Use this complete-surface procedure only in explicitly selected whole-repository mode. In representative mode, search only the traced family and its sibling for the same signals.

Choose the cheapest available path without changing the required coverage:

- **Analyzer path:** Use a repository-declared syntax or semantic analyzer when it can enumerate a category completely. Useful categories include trivial callable bodies, equivalent match arms, exhaustive pass-through matches, and duplicated branch structures.
- **Language-portable fallback:** For every category the available analyzers leave partial or unsupported, use an existing syntax tool or a disposable extractor outside the repository to enumerate the relevant constructs across the complete production surface. The procedure is shared across languages, but the extractor must understand the concrete language's syntax. Save a compact structural ledger in private scratch space and inspect its groups and failures rather than printing source bodies. Do not add a production dependency or permanent parser for the audit.

Before fingerprinting, build an independent construct-universe inventory for every production language and category in scope. Derive it from a language-aware parser or analyzer when available; otherwise use a separate lexical or token sweep from the fingerprint extractor. The fingerprint extractor may consume the resulting selectors, but it must not define its own denominator. Record the syntax families and branch-bearing keywords or operators the inventory recognizes. Include both statement and expression forms of branching, such as conditional expressions as well as `if`, `match`, `switch`, or `when`; include exception handlers, callable bodies, loops and comprehensions, dispatch defaults, and protocol, interface, or trait declarations. Language-specific spelling may differ, but an unrecognized construct is a coverage failure rather than evidence that the category is empty.

When no parser establishes the universe, conserve lexical cues across the complete token stream rather than searching line-oriented headers. Account for every occurrence of each recorded branch-bearing keyword or operator, including occurrences inside multiline and nested expressions, as exactly one construct selector or one explicit non-construct use with a reason. Reconcile cue counts by file and syntax family before fingerprinting. An anchored search, a count of only line-leading tokens, or a second extractor that shares the first extractor's eligibility rules is not an independent denominator.

The fallback must explicitly fingerprint single-expression callables, every arm of every inventoried branch construct, complete branch structures, collection traversals, dispatch defaults, and protocol declarations. For each category, its ledger records the universe method, fingerprint method, exact roots, universe count, fingerprinted count, explicit exclusions with selectors and reasons, extraction failures, selectors, normalized structural fingerprint, and duplicate groups. Reconcile the independently counted denominator as `universe = fingerprinted + excluded + failed`; any mismatch blocks the receipt. Exclude only constructs that are provably outside the named category or production roots, never constructs the extractor cannot classify.

Normalize formatting and comments while retaining identifiers that can change meaning, including call targets, fields, constructors, type names, and literals. Normalize locally bound names only when the extractor proves their corresponding bindings; report stronger normalization as a separate similarity lead rather than structural equality. Compare callable fingerprints for attribute access, constructor reapplication, and unchanged delegation. Group branch-arm fingerprints within one branch and complete-branch fingerprints across owners. Group repeated iteration sources and consumer keys within each function or neighboring projection.

A fallback receipt is complete only when its independent universe and lexical-cue conservation reconcile, every non-excluded construct has a fingerprint, and extraction failures are zero. Before freezing the ledger, cross-check the universe with the independent parser, analyzer, lexical, or token count and report any construct family present in one method but absent from the other. When a richer analyzer becomes available after a deliberately blind fallback evaluation, compare its complete construct counts and selectors with the frozen universe category by category before accepting the fallback certificate; candidate-family agreement alone is insufficient. Header enumeration, raw search-result counts, representative declaration reads, and semantic spot checks are candidate-generation aids, not evidence that bodies were compared. If either method cannot classify a construct, record its selector as a coverage gap and do not claim a fixed point.

Keep structural coverage and candidate precision separate. A reconciled universe proves that the constructs were visited, not that every smell predicate selected the same leads as a richer analyzer. Compare candidate families in both directions when an enrichment path exists, inspect every enrichment-only lead, and record whether the difference is a retained false positive, a fallback predicate gap, or a useful analyzer-dependent candidate. Without an enrichment path, close each required semantic row from the full fingerprint category rather than treating a narrow candidate query as proof of zero findings.

Keep the artifact compact. A suitable `structural-fallback/v1` record has one summary per category and stores bodies only as hashes plus short normalized-shape labels:

```text
category | universe method | fingerprint method | roots | universe | fingerprinted | excluded | failures | duplicate selectors | disposition
```

Query only duplicate groups, unmatched selectors, and extraction failures into model context. Semantic inspection still decides whether equal fingerprints reveal ceremonial repetition or independently owned behavior.

The analyzer path saves context and time; it does not grant stronger stopping evidence. The same semantic receipt and fixed-point conditions apply to both paths.

Search the complete production surface for these signals, then verify each with the trace and sibling simulation rather than treating the signal as proof:

- a route, tag, or enum immediately remapped to a correspondingly named typed value;
- duplicate or near-duplicate exhaustive branches over the same family;
- an exhaustive branch whose arms all read the same common field or invoke the same operation;
- repeated `match`, `isinstance`, ternary, or membership tests over the same closed family along one use-case path;
- a closed family handled through `else`, `case _`, mapping defaults, inherited behavior, optional callbacks, or a default registry handler;
- separate vocabularies with identical members and consumers that appear to assign the same meaning.

Before closing this pass, sweep the complete production surface for several shapes that ordinary reachability counts systematically miss:

- methods or functions whose body only returns one attribute, reapplies a constructor, or delegates unchanged arguments;
- the same effect, transition, or presentation choice selected independently by neighboring modes or routes;
- a test-only public root and the private helper chain that exists only beneath it;
- base protocols plus composite protocols whose required members overlap without distinct substitution consumers;
- defaults, version labels, compatibility names, and plural capability names that describe a predecessor rather than the supported product.

For each shape, record the search or analyzer, scope, candidate selectors or zero result, and disposition in the compact ledger. A representative trace can validate a design, but one trace is not evidence that these other shapes were searched.

Before claiming a fixed point, include one separate receipt row for every category below. Do not combine categories under an umbrella claim even when one search contributes evidence to several rows.

| Category | Required scope |
| --- | --- |
| Same-meaning remaps and pass-through delegation | Complete production surface and every traced route |
| Repeated traversal and projection scans | Neighboring consumers of each production collection |
| Duplicated mode, route, effect, or presentation selection | Complete production surface for each closed family |
| Equivalent closed-family branches | Every branch over each inventoried closed family |
| Defaults, fallbacks, optional handlers, and inherited behavior | Every closed dispatch mechanism and protocol implementation |
| Test-only public roots and their private helper chains | Production definitions referenced from tests and their transitive callees |
| Base and composite protocol overlap | All production protocols and their concrete substitution consumers |
| Archaeological and misleading names, defaults, versions, identities, and capability labels | Every inventoried production, API, wire, schema, storage, history, generated, presentation, documentation, metadata, and tooling name |

Each row must name the method or analyzer, exact scope, candidate count with selectors or an explicit zero, and disposition. A fallback row must also link its complete structural-ledger counts and zero-failure receipt. A candidate mentioned elsewhere in the report does not satisfy the row unless this evidence is present.

The last signal needs extra care. Identical spelling does not prove identical product identity. Retain nominally separate vocabularies when they prevent cross-family assignment, have different transition rules or consumers, or may evolve independently as distinct product concepts. Collapse them only when they name the same fact and all current owners interpret that fact identically.

## Simulate one sibling change

Choose one plausible sibling with the same boundary and lifecycle shape. List every site that would need an edit. Keep a site when it owns validation, policy, an independently required representation conversion, an effect, persistence, or presentation. Fold a site when it only repeats a selection made earlier. Preserve nominal distinctions when another consumer or protocol genuinely distinguishes them.

Keep exhaustive branching where a closed boundary or owner genuinely distinguishes the family. Use direct calls or a required protocol when the caller should not distinguish implementations. Use dynamic dispatch only for a deliberately open family with one visible wiring owner, an explicit required surface, and explicit failure for missing support. Do not hide incomplete handling behind a catch-all, mapping default, optional callback, inherited implementation, or fallback handler.

## Preserve independent decision gates

Use this method when a change extends behavior whose availability or effect interacts with conditions on the same or neighboring behaviors. Before editing, enumerate the relevant existing conditions, such as permission, readiness, dependency, freshness, feature, or environment checks. Trace the changed behavior and at least one sibling from their decision owner to observable effects. A new permission for one behavior does not relax another condition unless accepted product scope explicitly changes it.

Seek the cheapest mixed counterexample where the new behavior is allowed while an independently gated sibling remains forbidden. When several conditions and operations interact, a small condition/operation table or state/action matrix can make that case visible. Do not impose a matrix or state-machine model on ordinary code with no such conditional interaction.

Preserve the distinction with one focused observable test at the cheapest real boundary, or with the cheapest equivalent static or integration evidence when a test is not appropriate. Stop when each behavior remains governed by its own conditions and the mixed counterexample would fail if the implementation accidentally coupled them.

## Mock example: CLI leaf routing

Suppose one command currently travels through:

```text
argparse leaf
  -> CliRoute enum
  -> route-to-command decoder match
  -> typed command union
  -> exhaustive command-family router
  -> handler
```

Simulating a sibling command shows that `CliRoute` and the route-to-command match repeat the parser leaf's selection. They own no additional validation or product decision. Let an ordinary parser leaf carry inert metadata naming its exact command model. Keep a small explicit decoder branch only for leaves whose coupled options genuinely select between multiple command records. Retain the typed command union and the exhaustive command-family router: the union prevents invalid handler inputs, and the router still owns a real closed dispatch decision. Do not attach executable decoder callbacks to parser state; that hides the construction path behind dynamic behavior and makes static navigation worse.

The same test can justify superficially repeated work. Selecting an advertised capability before decoding its variant-specific payload and validating it again under a write lock are different observations when concurrency may intervene. Retain both, name the distinction clearly, and remove only remapping that does not own it.

## Evidence and stop condition

After a focused repair, inspect each changed owner and its adjacent producers and consumers for pass-throughs, obsolete routes, or duplicated decisions introduced or exposed by the change, then rerun the trace. For whole-repository coverage, rerun the complete routing-shape, construct, and semantic inventories over the original declared roots until a fresh pass produces no new in-scope candidate.

Report the before-and-after traces, sibling edit sites, retained exhaustive sites with their owned distinction, applicable semantic sweep receipts, and removed pass-through sites. In representative mode, stop when the sibling adds edits only at owners with distinct responsibilities and the route remains predictable. In whole-repository mode, also require every distinct routing shape to have an English-only trace and every sweep above to have a concrete zero-result or disposition receipt.
