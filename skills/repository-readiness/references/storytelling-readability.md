# Proof-read code as a product story

Use this optional pass only after the human chooses it. Its outcome is not “more comments” or one preferred architecture. Its outcome is a supported flow whose overview and implementation let a computer-literate newcomer tell the same accurate story.

## Establish the reading contract

Choose the agreed coverage mode. For a representative-flow pass, select one supported path and the smallest set of current documentation that claims to explain it. For a whole-repository pass, first enumerate every supported runtime and entry-point surface, agent skill and metadata surface, product and contributor document, generated projection and generator, development tool and configuration, packaging and dependency surface, CI assurance path, and test-evidence surface. Identify which sources own the current product story and which files are generated projections. Do not treat an old plan, test description, architecture fashion, or generated page as truth merely because it is easier to read.

In whole-repository mode, derive the distinct narrative shapes from differences in responsibility order, ownership, effects, normal exits, or next owner. Do not equate a command count, file count, or architecture layer with a shape. Record one uncoached English-only trace for every distinct shape: use ordinary verbs, value provenance, effects, expected exits, presentation, and the next owner without relying on implementation-language types or syntax. Continue until every enumerated surface is assigned to a trace, an independently explained non-routing role, or an explicit retained disposition.

Agree on the reader: someone who understands ordinary software ideas but may not know the implementation language, type system, framework conventions, or repository history. The reader may follow names and calls, but should not need type inference to discover whether a step reads, validates, decides, writes, refreshes, or presents.

Keep this evidence private unless the human requests a durable report. Record the selected entry point, overview, trace, unresolved questions, candidate repairs, and stop condition compactly.

## Get an uncoached reading

Have a fresh reader start with the overview, then follow the representative code path. Do not explain the intended architecture first. Ask the reader to retell:

- what entered the system and when it became an exact request;
- what information was merely observed;
- which caller-supplied claims were resolved against that observation;
- where current state became authoritative for the change;
- where legality was decided and how an expected rejection leaves;
- where an accepted decision became a durable effect;
- which later work is replaceable or repairable;
- what is finally presented to the caller.

At each call, ask four plain questions: What verb describes this step? Where did this value come from? What can end normally here? Which owner should I read next? Capture uncertainty rather than teaching around it. A guide that supplies answers absent from the code is evidence of a missing-manual problem, not a successful reading.

## Treat names as promises

A name is a claim about the thing it denotes. For every name in the agreed surface, ask what an ordinary English reading promises about:

- provenance and timing: observed, supplied, retained, proposed, locked, current, or final;
- success and durability: requested, decided, accepted, written, committed, or merely repairable;
- authority and responsibility: who may decide, change, persist, repair, or present;
- demonstrated capability: intelligence, autonomy, universality, plurality, prestige, or another implied power;
- identity: whether the spelling is incidental implementation language, a stable external contract, or a deliberate product or personal choice.

Compare that promise with demonstrated responsibility. A name is misleading when a normal reader must mentally negate or reinterpret it to tell the true story. Mechanical consistency does not excuse a contradiction: two sibling values called `current` can still be proposed replacements, and a value called `accepted` can still contain a rejection.

Keep one compact record for every mismatch:

```text
selector | claimed meaning | demonstrated responsibility | mismatch | normal reading cost | affected consumers | evidence | classification | disposition | reopen condition
```

Classify before changing anything:

- **Internal incidental:** an owned implementation or current-story presentation name with no compatibility or deliberate identity role.
- **Stable contract identity:** a public API, CLI, wire, schema, storage, history, or compatibility spelling whose consumers may depend on it.
- **Deliberate identity:** a product, project, personal, or emotionally meaningful name whose value is not reducible to technical precision.

Choose exactly one disposition:

- **Direct rename:** repair a low-risk internal name and retain the finding in the review evidence. Reopen if the old contradiction returns or the value's responsibility changes again.
- **Truthful local translation:** preserve a stable external spelling, translate it once at the nearest owner into vocabulary that tells the internal truth, and record the protected contract and consumers. Reopen when those consumers migrate or the boundary versions.
- **Migration or versioning deferral:** retain the finding and name the protected contract and consumers, migration or version cost, nearest truthful translation owner, safe reopening condition, and evidence that would justify reconsideration. Risk postpones the rename; it does not erase the mismatch.
- **Human decision:** describe a deliberate identity neutrally, distinguish harmless identity from a misleading technical promise, explain the concrete reading cost, and ask the human to retain, reinterpret, or rename it. Reopen only on that disposition or on evidence that its product meaning or consumer expectations changed.

These examples test the method rather than prescribe vocabulary:

| Situation | Finding and disposition | Reopen condition |
| --- | --- | --- |
| A private temporary says it is current or accepted before currency or success has been established | Keep the selector and evidence, then rename or delay that binding so the local story states its demonstrated role | The contradictory spelling returns or the stage semantics change |
| A supported wire field overstates what the internal value represents | Keep the external field and finding; translate once at the decoding owner into truthful local vocabulary | All consumers adopt a new version or compatibility ownership ends |
| A stored or historical label is misleading but changing it requires coordinated migration | Defer explicitly with the protected stores and readers, migration cost, nearest translation owner, and required equivalence evidence | The versioned migration is authorized and the named evidence is available |
| A product or personal name sounds more capable or prestigious than the implementation | Report the concrete reading cost without treating the identity as defective; let the human decide whether identity or technical clarity wins | The human chooses a disposition or demonstrated capability and audience expectations materially change |

The repository reviews that motivated this test provide two retrospective checks:

- The authority-core selectors used before/after names that claimed current durable state for expected retained and merely proposed authority, and an “accepted” name that claimed success before rejection was excluded. That made maintainers of all three authority families read success and durability too early. The accepted review is the evidence. These were internal incidental names, so direct rename and binding after failure exclusion was safe; reopen if that premature claim returns or the stages change.
- The journey-diagram arrows and labels claimed that repairable file projections supplied authoritative current state, while the implementation independently reread SQLite before presentation. That made guide readers and maintainers assign authority to the wrong owner. The accepted guide review is the evidence. This was current-story presentation without a compatibility identity, so its generator changed directly; reopen if the dataflow changes or a generated diagram again routes authority through repairable output.

Those repairs belong to their concrete owners. Preserve the questions and disposition method, not their replacement spellings as a universal recipe.

## Use a stage grammar as a probe

For a changing command, this sequence is a useful probe:

```text
decode exact input
  → observe context
  → resolve supplied claims into a requested change
  → reread locked current state
  → decide
  → project the accepted change
  → commit
  → refresh replaceable views
  → present committed state
```

Do not force these names or stages onto the repository. A different architecture may combine them, order them differently, or have no views or transaction. Translate the probe into the product's actual responsibilities, then make absent and combined stages explicit. The important test is whether one story explains both code and documentation without hiding effects.

Pay special attention to pairs that code often blurs:

- decoded input versus a domain request;
- observed context versus locked current state;
- caller-supplied authority versus resolved authority;
- validation of shape versus a decision using current state;
- accepted decision versus committed effect;
- authoritative storage versus generated projection;
- expected rejection versus infrastructure failure;
- committed state versus the value finally rendered.

## Compare documentation and code in both directions

Write the overview's stages and the code's stages side by side. For each stage, record its verb, input provenance, owner, effect, expected exits, and next stage. A mismatch is real when either side invents, omits, reorders, or renames a meaningful responsibility.

Do not assume documentation wins. Correct the overview when it describes an obsolete or idealized flow. Correct code when its names or composition conceal current behavior. Correct both when they share the same shape only after expert interpretation. Generated documentation changes at its source and is regenerated; it is never patched as the authority.

Do not convert narrative surprise into a correctness claim. Check accepted requirements, observable behavior tests, and current consumers before recommending a semantic change. An unusual time sample, authorization label, commit abstraction, or latest-state presentation may be deliberate even when it is poorly explained. When product intent is unresolved, record the question and offer behavior-preserving naming, composition, or documentation options; ask the human before changing behavior.

## Repair the earliest misleading owner

Prefer the smallest repair that makes the next step predictable:

1. Rename effectful helpers with verbs and concrete product objects.
2. Give values provenance names such as `observed_state`, `requested_change`, `locked_state`, `accepted_decision`, `targeted_mutation`, and `committed_state` when those distinctions exist.
3. Use named arguments where several same-shaped values otherwise require positional decoding.
4. Recompose a long function when another arrangement makes the ordered product story or next owner clearer. Extract only functions that own meaningful inputs, effects, and exits; inline or regroup helpers when their separation creates a scavenger hunt.
5. Move a responsibility when the reading exposes genuine mixed ownership; explain the proposed boundary and obtain the human's decision before expanding improvement scope.
6. Update the current-story overview to the same vocabulary and order.
7. Add durable contributor guidance only when the method should govern future delivery and no earlier executable owner can enforce it.

Do not add a pipeline abstraction, registry, callback system, inheritance hierarchy, or layer vocabulary merely to make the diagram symmetrical. Symmetry means comparable responsibilities read comparably. It does not mean unrelated paths must acquire identical machinery.

Function length, lint smell categories, duplication reports, and split-by-default style are weak evidence. Use them to ask a question, not to decide the structure. Prefer one longer coherent owner over several shorter functions that scatter one decision or effect. Preserve duplicated-looking code when the copies belong to independent owners; consolidate it only when the same behavior or decision genuinely needs one owner.

## Stress-test the family, not one polished example

Trace at least one sibling operation that shares the same responsibilities. It should use the same verb grammar and provenance distinctions without duplicating a routing fact in every layer. When the flow is a command or closed family, use [developer navigation](developer-navigation.md) at the selected representative or whole-repository depth.

Simulate adding one sibling. Count the owners a developer must discover and edit. Keep a site when it owns input decoding, validation, policy, representation conversion, an effect, or presentation. Recommend folding a site when it only repeats a fact already selected elsewhere. A low edit count is evidence of navigation cost, not proof of readable responsibility. In whole-repository mode, repeat this simulation for every distinct narrative shape.

## Close the repair loop

A readability repair can itself leave narrative scaffolding: one-line helpers, renamed duplicate vocabularies, pass-through conversions, parallel projections, or variants that merely restate an already selected fact. Treat those as part of the pass rather than as later debt.

For a representative-flow pass, inspect every changed owner and its adjacent producer and consumer after the coherent readability repair. Remove new pass-throughs, duplicated vocabularies, and obsolete projections whose disposition is already authorized, then reread the same flow and sibling. Finish only when the reading remains clear without that ceremony and the focused repair exposes no new candidate.

For a whole-repository pass, complete the readability repairs, then run a fresh full surface, narrative-shape, and semantic sweep across the original declared roots. Apply authorized findings and reread every affected narrative shape until a fresh complete pass produces no new in-scope candidate. A finding never grants permission to change behavior, compatibility, or product identity.

## Close whole-repository semantics separately

A whole-repository result includes one separate disposition receipt for each semantic category required by the developer-navigation reference. Do not combine categories even when one analyzer or search supports several:

1. Same-meaning remaps and pass-through delegation.
2. Repeated traversal and projection scans.
3. Duplicated mode, route, effect, or presentation selection.
4. Equivalent closed-family branches.
5. Defaults, fallbacks, optional handlers, and inherited behavior.
6. Test-only public roots and their private helper chains.
7. Base and composite protocol overlap.
8. Archaeological names, defaults, versions, and capability labels.

Each receipt names its method, exact scope, candidate count and selectors or explicit zero, and disposition. After repairs, rerun the complete surface and shape inventory from scratch once. Finish only when that fresh pass produces no new in-scope candidate and every surface, shape, sibling simulation, bidirectional documentation comparison, and semantic receipt still closes.

## Let the human choose the durable rule

Present the observed reading failure, the smallest repair, any responsibility-level refactor, and the tradeoff of making the method durable. Ask the human which recurring standard they want future implementers and reviewers to follow. Place the accepted rule at the earliest stable owner: executable structure where possible, project design principles for a reusable method, a product overview for the current story, and a thin contributor route when automatic consultation is needed.

Do not turn one repository's layer names or stage vocabulary into a universal rule. Preserve the reason, the reader test, and the stop condition rather than a frozen implementation recipe.

## Validate the outcome

Run behavior, expected-rejection, transaction, concurrency, generation, link, and metadata checks appropriate to the changed surface. Do not add unit tests that assert prose wording. Regenerate authoritative documentation projections and inspect meaningful visual output rather than relying only on a generator exit code.

Repeat the fresh reading after the changes. In representative-flow mode, finish when the reader can tell one accurate ordered story from overview through code, sibling paths are symmetric where their responsibilities match, authoritative and repairable effects remain distinct, and another refactor would only rename already clear work or introduce ceremony. In whole-repository mode, also require the complete fresh no-new-candidate pass above; one polished trace cannot satisfy that claim.

When validating this reference itself, compare two context-isolated readers on the same historical repository snapshot and identical task. Give one reader no readiness guidance and the other only the copied Repository Readiness skill directory. Compare whether they recover the intended sequence, documentation mismatches, effect boundaries, architecture-neutral advice, and bounded next changes. Report only the demonstrated scenario; one successful comparison does not prove universal effectiveness.
