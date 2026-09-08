---
name: repository-readiness
description: Assess whether an unfamiliar repository can be changed reliably by mapping semantic authority through consumers, projections, and validation, then make only authorized improvements. Use for onboarding vibe-coded, inherited, or long-lived multi-team codebases. Do not use for a known cleanup-only task or a routine guidance correction with an already settled owner.
---

# Make an unfamiliar repository safe to change

Build an evidence-backed map that lets a newcomer find the real owner of a change, its complete footprint, and the checks that keep it coherent. Diagnose before improving. Do not treat existing structure as intentional merely because it exists.

## Choose the assessment boundary

Use **representative mode** by default. Select one meaningful supported change path and enough neighboring consumers to test whether the same ownership story holds. State that the result is bounded and do not turn it into a whole-repository claim.

Use **whole-repository mode** only when the user explicitly asks for complete coverage. Enumerate every supported runtime and public entry point, agent and plugin surface, product and contributor document, generated projection and generator, development tool and configuration, packaging and dependency surface, CI path, and test-evidence surface. Assign each surface to a traced change path, a non-routing role, or an explicit unresolved disposition.

Assessment is read-only unless the user has also asked for improvements. Treat audit findings as evidence, not mutation authority.

Within either mode, inventory only the evidence-backed limitations and operating assumptions visible in the selected boundary. Representative evidence supports claims about that path and its traced neighbors, not the whole repository. Do not invent limitations from generic risk lists or treat the assessment as permission to add features, hardening, compatibility, or cleanup.

## Establish current authority

Start from the user's requested outcome and the repository's current product, architecture, contributor, and agent guidance. Then verify supported entry points through package metadata, runtime registration, configuration, generators, and actual consumers.

Classify relevant material as:

- established semantic authority;
- strong but inferred authority;
- consumer;
- generated or explanatory projection;
- validation evidence;
- historical, transitional, dead, or uncertain.

Code proves implementation, tests prove observed behavior, and documentation proves a claim only after its ownership is established. None of them alone proves product intent. Prefer improving an existing trustworthy map over creating a competing one.

Find the project's current-truth owner for deliberate limitations, if one exists. For each selected limitation, record the implemented boundary, practical consequence, affected supported paths and owners, evidence, deliberate reason, and observable reopening condition. Distinguish an acknowledged existing limitation from an introduced or widened limitation, a defect against accepted behavior, and a speculative risk. Report missing or conflicting ownership instead of filling it with inference.

## Trace authority to validation

For each selected change, start from a realistic user-level request and record:

1. **Authority:** where the product decision, schema, policy, configuration, or generator input lives.
2. **Consumers:** every supported implementation, configuration, documentation, or guidance surface that applies it.
3. **Projections:** generated or copied representations and the owner that produces them.
4. **Validation:** tests, checks, schemas, CI, or runtime evidence that detect divergence.

Trace outward from the authority and backward from likely entry points. Record plausible wrong paths, missing links, duplicated decisions, projection/authority confusion, and places that require repository history or tribal knowledge. Keep unresolved intent visible instead of guessing.

When a selected change extends a decision surface with interacting conditions or operations, read [Preserve independent decision gates](references/developer-navigation.md#preserve-independent-decision-gates). When a command, closed family, or dynamic route makes the next implementation owner difficult to predict, read the full [developer-navigation.md](references/developer-navigation.md) routing audit. When code and its overview tell different stories, names make false promises, or a newcomer cannot accurately retell the flow, read [storytelling-readability.md](references/storytelling-readability.md). Load none of these references for an ordinary authority trace that does not need their lens.

## Leave code understandable to the next agent

Assess comprehension on every selected path, including production code and its tests. Follow names, calls, and local contracts to establish purpose, input provenance, decisions, effects, expected failures, important constraints, and the next owner. Record where understanding requires reconstructing history, tracing hidden wiring, or accepting an unsupported comment. Apply this criterion within the chosen assessment boundary; it does not turn a representative scan into a whole-repository audit.

Look for misleading names, comments that defend behavior without current evidence, dynamic dispatch or selection tables whose alternatives and wiring are hidden, and exception paths that conceal rejection, recovery, or partial effects. These are investigation prompts, not automatic defects. Verify what each mechanism supports before proposing a change; retain justified open dispatch and exceptional boundaries when their contracts are locally discoverable.

For each comprehension finding, identify the exact owner, the question a reader could not answer, what the evidence established, and the smallest durable repair. Recommend clearer names, simpler composition or explicit routing where justified, or a local comment or contract for an important reason that code structure cannot express. Keep consequential unresolved intent as an explicit human decision with the missing evidence and practical consequence. Do not turn a plausible explanation into a fact.

In assessment-only work, recommend the concrete repair and its owning location. When improvements are authorized, apply the repair within scope and reread the affected path without relying on the investigation notes. The next agent should not need to repeat that investigation to understand current behavior. An audit explanation alone does not repair opaque code; stop when the understanding is locally discoverable and further edits would merely restyle clear work.

## Present the diagnosis

Scale the result to the selected mode. Include:

- an executive assessment of how safely a newcomer can make the selected change;
- the authority → consumers → projections → validation map;
- a walkthrough from the user request to the owning implementation and checks;
- evidence-backed risks and misleading alternatives;
- comprehension findings with their unanswered questions, established evidence, concrete repair owners, and unresolved intent;
- evidence-backed current limitations and operating assumptions within the selected boundary, including consequences and reopening conditions;
- unresolved human decisions;
- small safe improvements and larger follow-ups kept separate; and
- awkward structures that evidence says should remain alone.

Do not produce a readiness score or certification. A useful result makes evidence, uncertainty, and the next owner discoverable.

## Improve only what is authorized

When the user has asked for improvements, apply the smallest changes whose authority and complete footprint are established. Good candidates include correcting an existing map, adding a local pointer from a misleading projection, exposing an existing generator, reconciling stale explanatory documentation, or removing a clearly deceptive obsolete path with no supported consumer.

When agents are supported consumers, leave the selected change path usable by a fresh agent with less repository familiarity. Prefer an installed self-describing command, generated strict schema or starter, typed mismatch with safe retry semantics, and one representative production trace over a prose-only inventory or a need to inspect implementation source. Exercise at least one wrong input, stale observation, and partial-effect failure when those outcomes exist. This does not make semantic judgment mechanical: document where product meaning, source authority, independent review, or runtime capability still requires a stronger model or a human decision.

When authorized improvement includes durable limitation handling, establish complementary owners rather than copied boilerplate: current architecture or equivalent documentation owns implemented facts; a design authority owns the reusable classification, materiality, acknowledgement, and reopening method; scoped agent guidance provides only the automatic route; and specialized workflows retain only their boundary-specific behavior. Use the target repository's vocabulary and evidence. Do not copy Pinboard-specific limitations into another project.

Before implementing an improvement that would introduce, widen, preserve, mask, or deepen an unacknowledged material limitation, present one architecture checkpoint using the target project's method. Begin with **Architecture checkpoint**, give numbered consequences and a recommendation, place a bold practical-consequence summary immediately before an ordinary approval question, and proceed only after an explicit answer to that exact decision. Do not require a forced phrase, treat generic improvement authority as acknowledgement, or let the answer expand the authorized improvement scope. Do not repeat an unchanged acknowledged limitation unless its consequence changes materially.

Pause for a human decision when outward behavior, compatibility, persisted data, product identity, or architectural responsibility remains ambiguous. Validate every changed authority, consumer, and projection through the repository's existing checks. Stop when the selected change path is discoverable and another edit would merely restyle clear work or settle unsupported intent.

Confirmed unsupported residue belongs to `$slop-cleanup` when that skill is available; report the candidate and obtain deletion authority rather than copying its recursive removal workflow here. A durable AI-guidance ownership problem may use `$maintaining-agent-guidance` when available. Pinboard may preserve an accepted improvement campaign when the user already uses it. These are optional enhancements: never require or invoke a sibling skill, create work, or initialize Pinboard merely to complete this assessment.
