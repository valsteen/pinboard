# Structural consolidation after removal

Use this reference when a removal leaves alternatives, wrappers, repeated boundary shapes or other structural residue to collapse. Work only through direct dependents of the approved removal and preserve independently required product and boundary distinctions.

After deletion changes the graph, search for structures that used to distinguish alternatives but no longer do:

- one-member label vocabularies or variants without an external serialized contract;
- base classes or protocols with one implementation and no substitution role;
- pass-through wrappers, single-use indirections, one-attribute accessors, and no-op conversions;
- parallel tuples, dictionaries, projections, or field-by-field comparisons that reproduce an existing canonical typed value without owning a distinct external representation;
- hand-written primitive validators or mapping walkers where one declarative boundary record can own conversion, constraints, unknown-field rejection, and error paths;
- downstream validation that repeats a field-local or same-record invariant already guaranteed by the deserialization model; keep a later check only when it combines independent sources or current external state, and challenge direct construction or internal-DTO use of boundary records that bypasses the decode contract;
- tests that repeat one fact at every layer without proving distinct wiring, representation, effects, failure handling, concurrency, or compatibility; keep the cheapest owning proof and rely on existing coverage for unchanged behavior instead of compensating for deleted code with test copies;
- identical aliases, redundant alternative sets, and a discriminator that duplicates the variant hierarchy;
- conditions whose alternatives now do the same thing, impossible branches, and commands that can only reject;
- fields copied through layers without a current producer and consumer;
- parameters that appear used only through discard assignments such as `_ = value`, warning suppressions, or comments defending their presence; trace callers to remove orphaned transport and resource sampling, while retaining signatures required by verified interfaces;
- empty or tiny files, modules, and test groups that no longer own a coherent concept.

Regroup by current concepts. Separate declarations from logic when each side has a meaningful thematic role; merge them when separation would create ceremonial files. Make test organization mirror the surviving production concepts whenever practical. Test helpers belong in tests, not in production APIs created solely for fixtures.

Run an archaeology pass over names, comments, error codes, schema labels, help text, examples, documentation, and tests. Remove wording that describes a predecessor, migration phase, plural capability that is now singular, or behavior the code can no longer perform. Collapse documentation around the surviving concepts, remove pages, sections, examples, diagrams, badges, and setup instructions whose feature or workflow was removed, and keep parallel documents consistent rather than leaving one stale version behind. Every advertised feature must trace to a supported entry point or explicitly labeled current limitation; do not turn deleted or never-shipped implementation into present-tense documentation or an invented roadmap. Remove stale lint, warning, ignore, and coverage suppressions with the ecosystem’s unused-suppression check when available.

For structural boilerplate, use one repeatable pass:

1. List collections traversed by neighboring projections. Group each collection once by the consumer key when repeated scans reconstruct the same relationship; keep the grouping local and explicit.
2. List records whose optional fields serve different operations. Replace them with the smallest flat variants that make supported combinations concrete, then require producers to construct and consumers to handle those variants exhaustively.
3. List mapping-shaped external values decoded field by field. Replace primitive accessor and validator families with one strict declarative record conversion when the format is structural; retain explicit code for custom grammars, relational state, and semantic policy.
4. For each duplicated closed classification, list every encoding and choose one owner nearest the behavior. Keep a label-only vocabulary when alternatives have the same data and meaning, use data-bearing variants when alternatives require different data, and leave context-dependent legality in the decision that owns the surrounding state.
5. Compare every branch that handles closed alternatives. Combine alternatives when their conditions, bound values, effects, and result are equivalent; remove a named alternative when a general branch already owns the same outcome. Preserve the alternatives themselves when another consumer, protocol, retry policy, or lifecycle decision distinguishes them.
6. Preserve an independently owned external or persisted shape with one explicit exhaustive boundary conversion. Trace same-shaped values through every call and adapter, folding layers that add no validation, policy, protocol, or independently reused operation.
7. Compare actual decision points and developer navigation before and after. Report justified exhaustive sites, explicit boundary conversion, edit sites for one representative sibling, dependency volume, and source-size change separately so a smaller file or dependency list cannot stand in for a simpler decision model.
8. Re-run these inventories after each fold. Stop only when a fresh pass finds no repeated traversal, nullable multi-operation record, hand-decoded structural mapping, orphaned same-shaped call trail, or equivalent alternative-handling branch in the accepted scope.

Stop collapsing when the remaining alternatives are a legitimate vocabulary, the distinction has an independent consumer, or the boundary conversion would cost more decision structure than the invalid combinations it prevents.
