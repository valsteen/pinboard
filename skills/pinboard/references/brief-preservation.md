# Cross-boundary brief preservation

Use [the coding-agent runtime adapters](runtime-adapters.md) for fresh reviewer launch and waiting. The preparation and review contract below is shared across integrations.

Use this procedure only for a typed `cross-boundary` checkpoint. It projects named architecture, plans, and accepted evidence into a reviewable execution contract before implementation. A checkpoint is `local` only when ownership and dependency direction, stored and wire identities, and independently owned consumers remain unchanged, and one production entry point can observe the complete changed path. If any condition is false, use `cross-boundary`. A local checkpoint does not add contracts, reviewed-authority coverage, lifecycle declarations, or an independent brief review.

For broad autonomous work, require the Pinboard workflow's short human-facing scope confirmation before preparation begins. Compile the canonical brief to preserve that stated outcome, principal read and touch surfaces, approximate magnitude, and surprising exclusions; never use the private brief to introduce a consequential narrowing or widening that the human did not see. The confirmation is declarative and does not repeat an authorization question already answered.

## Freeze semantics and select stable authorities

Before writing contracts or coverage rows, freeze the accepted outcome, assurance model, constraints, non-goals, provenance, compatibility, current architecture decisions, and explicit uncertainties. Apply the main skill's [assurance boundary](../SKILL.md#keep-assurance-inside-accepted-product-scope): distinguish in-scope defects and accepted assurance obligations from material new obligations and speculative risks. Derive stable semantic authorities from those facts rather than guessing the implementation diff. Select the decision, policy, architecture, or external-contract owners needed to challenge the proposed behavior and verification. The brief must make likely owners and uncertainty discoverable; it does not need to predict every file implementation will touch.

Keep the preparation read proportional to the proposed result. For a routine cross-boundary documentation or guidance change, select the smallest authoritative sections and real consumer or generated projection that can disprove the planned story. Do not require unrelated production owners, complete repository traversal, or a fixed-point file inventory. Tests may prove a contract but do not replace its semantic owner. `pinboard brief-sources` proves the bytes selected by a manifest; it does not prove semantic completeness.

Widen during preparation only when the accepted change itself crosses architecture, persistence, wire, lifecycle, compatibility, trust, or a dynamic-consumer boundary, when the material-limitation method requires its foundation, or when explicit uncertainty prevents a truthful contract. At such a boundary, include the relevant decision or effect owner, direct caller or consumer, independently required conversion, and cheapest real evidence that can falsify agreement. Do not turn that focused trace into a repository-wide pass unless the accepted scope explicitly selects one.

Challenge the selected authorities before canonical publication. Commission one bounded read-only reviewer as a subagent using the accepted definition, architecture declaration, draft contracts, explicit uncertainty, source manifest, and the minimum search or inspection needed to test their roles. Its results return automatically to the owning task; do not create or message another task for this subordinate review. The reviewer identifies missing stable authorities, unsupported roles, unresolved uncertainty, and any concrete widening trigger. Resolve those findings before compiling the brief. This draft check does not replace the required independent review of the canonical checkpoint.

Encode and measure the selected authority set in a temporary strict `pinboard-brief-sources/v1` JSON manifest with one row per exact selector:

```json
{
  "schema": "pinboard-brief-sources/v1",
  "sources": [
    {
      "authority_id": "architecture",
      "selector": "ARCHITECTURE.md#Dependency direction",
      "families": ["ownership", "dependencies"]
    }
  ]
}
```

Use one selector with several families instead of repeating or nesting the same selection. Run `pinboard brief-sources --file <manifest> --json` before reading any selected body. Correct overlap errors, inspect selected byte counts, spans, digests, and batches, and preserve that exact output as the strict `pinboard-brief-source-plan/v1` input. Emit each batch once in ascending order with `pinboard brief-sources --plan <plan> --emit-batch <index>`; emission reads the plan and only the source files represented in that batch.

Preserve each selector and selected digest as its read receipt. Across corrections, reuse exact unchanged receipts and reread only changed owners plus neighboring records whose meaning depends on them. If output truncates, continue from the first unread boundary without replaying returned content.

If the selected set plus working headroom cannot fit, narrow selectors, split only a semantically independent checkpoint, or move mechanical comparison into validated tooling. Do not omit an accepted requirement, prohibition, explicit uncertainty, lifecycle sibling, or consumer merely to fit context.

## Compile canonical JSON

Choose the accepted local or cross-boundary scope, then run `pinboard tool-contract --brief-starter <local|cross-boundary> --json` and prepare a strict `pinboard-work-brief/v2` JSON candidate by copying its complete `starter` object. For every applicable path in `structural_choices`, choose exactly one returned variant and replace the starter value at that path with its complete template. The initial tagged value is only the first valid template, not a fixed semantic choice. Then replace every semantic or identity `null` only with accepted scope, reviewed authorities, observed consumers, and explicit verification; preserve all other fields, and do not omit keys or hand-author a partial approximation. Read the selected checkout's actual branch and full Git revision immediately before filling `branch` and `base_revision`; never invent or abbreviate them. The structural choices, canonical byte rules, relational constraints, and fact-validation boundary are construction aids, not semantic evidence. Query `pinboard tool-contract --operation brief/publish --json` only when the complete generated validation schema or publication operation facts are needed. Pinboard decodes the completed candidate directly into frozen records with unknown fields forbidden, validates its cross-references, canonicalizes it with sorted object keys and one final LF, and publishes the immutable accepted `.json` artifact through `pinboard brief publish --file <candidate> --json`. Publication does not resolve branch or base revision against Git and does not prove semantic claims. This JSON artifact is the sole semantic brief. The generated Markdown attempt view is read-only output and must not be edited or parsed as input.

The root record contains:

- schema and artifact identity: `schema`, positive `artifact_revision`, `attempt_id`, `item_id`, `branch`, `base_revision`, and `owner_task_id`;
- accepted scope: positive `revision` and lowercase SHA-256 `digest`;
- human context: `title`, `outcome`, `supported_production_roots`, `product_decision_and_provenance`, `testing_strategy`, `scope`, `bootstrap`, `compatibility`, `non_goals`, and `remaining_work`;
- one tagged `checkpoint` record.

Use the existing human-context and checkpoint fields to preserve the material-limitation assessment; do not add a consent field or side-channel receipt. When no relevant unacknowledged material limitation is crossed, state that evidence-backed result in `product_decision_and_provenance`. When a limitation is accepted, that field records the exact decision and ordinary explicit human acknowledgement, while scope and non-goals bound its effect. The checkpoint must be `cross-boundary`, its architecture impact must be `update-required` for the current-truth owner, and its contracts, reviewed authorities, coverage, criteria, and verification must account for every affected owner and consumer plus the observable reopening condition. Generic authorization for the surrounding task is not equivalent provenance.

Use those same existing fields to preserve the supported assurance model. A mandatory verification or blocking guarantee must trace to accepted scope or an exact contract or criterion, applicable repository policy, an exact reviewed product-authority family, a named production consumer, or an observed supported-path failure. Broad correctness prose, an exploratory failing test, public narrative, and newly written skill prose are not product authority for another guarantee. If a proposed material expansion lacks support, resolve the main skill's concrete human decision before placing it in scope, contracts, criteria, or verification. Do not add a schema field unless a paired or otherwise accepted behavioral run demonstrates that these existing fields cannot preserve the distinction; first name the exact schema, codec, projection, tool-contract, review-job, compatibility, and architecture widening required.

Challenge any proposed local fix against its foundation. If it preserves, masks, or deepens an unacknowledged material limitation, resolve that architecture checkpoint before preparation rather than treating the compensating layer as a complete bounded fix. A defect against accepted behavior remains a defect, and a speculative risk without evidence on a supported path does not become mandatory scope.

Use one built-in design lens while compiling the brief: prefer existing canonical typed values and direct composition. Introduce a helper, wrapper, protocol, projection, or conversion only when it owns a current semantic boundary, meaningful complexity, reuse, or genuine substitution. Do not recreate an existing concept as a parallel tuple, dictionary, or field-by-field mirror. This lens shapes the proposed implementation; make it a mandatory contract or verification only when accepted scope or reviewed repository policy authorizes it.

When a cross-boundary checkpoint extends behavior governed by interacting conditions or operations, read and apply [Preserve independent decision gates](../../repository-readiness/references/developer-navigation.md#preserve-independent-decision-gates). Project its condition inventory, sibling trace, mixed counterexample, and focused test or equivalent evidence into the checkpoint's existing contracts, criteria, coverage, and evidence. Do not load the method for an ordinary change with no conditional interaction.

When a checkpoint introduces or changes a command or closed-variant family across several production owners, or places dynamic dispatch on that path, read and apply [developer-navigation.md](../../repository-readiness/references/developer-navigation.md). Project its representative trace, sibling simulation, justified exhaustive sites, and explicit dynamic-wiring failure mode into the checkpoint's existing contracts, criteria, coverage, and evidence. This is a bounded prevention lens for the changed family, not authority for a repository-wide readiness assessment.

Every checkpoint has a stable kebab-case `checkpoint_id`, separate human `title`, `outcome_description`, architecture impact, nonempty acceptance criteria, nonempty mandatory verification, and explicit deferrals. Its `boundary` tag selects one closed shape:

- `local` contains the common fields only;
- `cross-boundary` additionally requires `outcome: independently-buildable`, nonempty contracts, reviewed authorities, authoritative coverage, and one lifecycle partition.

When a checkpoint owns structural cleanup, its typed scope and acceptance criteria must name the readable typed consolidation, the direct orphan families to recurse through, and the evidence from one fresh fixed-point pass. Reference the repository's recursive-cleanup procedure for the method instead of duplicating it here; the generated Markdown projection remains non-authoritative.

When that cleanup collapses a closed classification, the contract must identify every duplicated encoding, equivalent alternative-handling branch, proposed canonical owner, and independently required boundary shape. It must distinguish label-only vocabularies, data-bearing alternatives, and context-dependent decisions; require equivalent branches to share one handler without erasing alternatives that another consumer distinguishes; and require exhaustive conversion at retained wire, storage, or presentation boundaries. Apply the developer-navigation reference at the complete depth explicitly selected by the accepted cleanup contract in addition to the bounded prevention lens above. Acceptance evidence must report core decision points, justified exhaustive sites, explicit boundary conversion, representative sibling edit sites, dependency volume, and source-size change separately, then name the fixed-point pass and the product distinctions deliberately retained. Reject a brief that prescribes one representation category for every closed family, treats exhaustiveness as a layer-by-layer quota, rejects dynamic dispatch without considering its ownership, or treats smaller source or dependency counts as proof of a simpler decision model.

Architecture impact is tagged by `kind`:

- `none` records a reason ownership and dependency direction are unchanged;
- `read-only` records one project-relative authority selector and conformance reason;
- `update-required` records the authority selector that must change in the same candidate and why.

Each contract records `invariant`, `authority`, `consumer`, `failure`, `verification`, `revalidation`, and a tagged `authorization_basis`. Each mandatory verification record has an `obligation` and the same authorization basis. Use exactly one basis:

- `accepted-scope` with the current `item_id` and positive `scope_revision`;
- `authority`, `repository-policy`, or `existing-consumer` with an exact reviewed `authority_id` and `family`.

Dispatch checks basis reference integrity against the current SQLite attempt. The independent reviewer owns semantic truth: reject a syntactically valid source used under the wrong role, such as code cited as repository policy, a validator cited as a production consumer, or newly written prose cited as product authority. Verify that every mandatory tool, threshold, platform, compatibility obligation, and hardening check is required by accepted scope or selected authority bytes. Keep proportionate exploratory checks outside the mandatory list.

Each reviewed authority records a unique kebab-case `authority_id`, exact `selector`, selected-byte `reviewed_sha256`, and one or more unique kebab-case `families`. A selector is a project-relative file, optionally followed by `#` and one literal unique Markdown H1–H6 heading. Whole-file digests use unchanged bytes. Heading digests use the heading through the line before the next heading of equal or higher level, with LF line endings and one final LF.

Give every reviewed authority family exactly one coverage record. It names `authority_id`, `family`, `distinction`, `consumer`, `counterexample`, and one tagged `owner` disposition:

- `contract` with an exact contract invariant;
- `acceptance` with an exact criterion number;
- `deferred` with an exact deferral ID;
- `not-applicable` with a concrete reason.

Never defer or mark not applicable an in-scope prohibition. Missing coverage is a brief defect, not implementation discretion.

Lifecycle partition is tagged by `kind`. Use `not-applicable` with a reason when the checkpoint changes no related lifecycle operations. Use `required` with one record per related operation when adjacent operations consume related states. Each record names `operation`, `source_state`, `authority`, `evidence`, `effects`, and one cheapest `illegal_sibling`.

## Review the compiled contract

Commission one read-only reviewer as a fresh-context subagent after compiling the checkpoint and before implementation. Its results return automatically to the owning task; do not create or message another task for this subordinate review. The reviewer task identity must differ from the attempt owner and every draft authority reviewer. Give the reviewer the canonical checkpoint and the same source plan. It must:

- confirm that the selected authorities preserve the accepted outcome, constraints, stable decisions, and explicit uncertainty; when a concrete widening trigger exposes another semantic owner, return one bounded correction package and stop before row-by-row review;
- inspect every selected source exactly once and classify its claimed semantic role;
- verify every contract, criterion, mandatory verification basis, architecture declaration, and semantic source role;
- reject a mandatory or blocking assurance obligation that lacks an accepted basis, and reject both unsupported expansion and a proportionality-based waiver of accepted hardening;
- trace every reviewed family to exactly one coverage owner;
- test each cheapest counterexample and every lifecycle sibling;
- reject unsupported, absent, ambiguous, or contradictory coverage rather than asking the implementer to infer it.
- verify that every material limitation named or exposed by the checkpoint has exact accepted acknowledgement and a coherent current-architecture update, and reject generic task authority, an unacknowledged foundational limitation hidden by a compensating layer, or a local checkpoint that introduces such a limitation.

Correct the JSON candidate when coverage is incomplete, increment its artifact revision, and republish it before producing new digest-bound review evidence against the corrected accepted artifact. Use the same independent reviewer to verify bounded corrections by default; changed artifact bytes alone do not justify another fresh full review. Reuse unchanged source receipts, reread changed owners, and inspect only neighboring records whose meaning depends on the correction. When a correction exposes an ordinary caller, projection, or generator, add its owner and relevant siblings as one bounded relationship set rather than one file per round. Commission another fresh reviewer when the correction changes accepted scope, architecture direction, lifecycle semantics, persistence or migration behavior, or whenever concrete evidence identifies a material blind spot that the prior reviewer cannot independently challenge.

## Prepare and publish review evidence

Canonical encoding uses the application JSON codec with sorted object keys. The brief artifact adds one final LF. Digest inputs do not add presentation bytes:

- `checkpoint_sha256` is SHA-256 of the canonical encoded checkpoint record;
- `reviewed_authority_set_sha256` is SHA-256 of the canonical encoded ordered tuple of reviewed-authority records;
- each `reviewed_sha256` remains the digest of the selected source bytes.

Prepare strict `pinboard-work-brief-review/v2` JSON with `attempt_id`, stable `checkpoint_id`, both digests, independent `reviewer_task_id`, `status: complete`, `verdict: ready`, and one coverage result per brief coverage record. Each result repeats the exact `authority_id`, `family`, and tagged owner, records `verdict: covered`, and states the concrete `counterexample_result`.

Pass the candidate to `pinboard dispatch` with `--brief-review <candidate-file> --review-id <kebab-case-review-id>`. Dispatch validates and canonicalizes it before application-owned publication. It creates the immutable artifact once, reuses byte-identical evidence, and preserves differing collisions as rejected evidence. Omit both publication arguments when exact accepted ready evidence already exists. Publication arguments are cross-boundary-only and never change the canonical prompt.

Dispatch reselects the current action and accepted brief, verifies the stable checkpoint ID, canonical checkpoint and ordered-authority digests, selected-source digests, reviewer independence, exact coverage, and immutable evidence before returning the launch prompt.

## Reuse during implementation review

Use the compiled map again against the frozen candidate. Account for every criterion, contract and authorization basis, mandatory verification entry, coverage record, and lifecycle sibling. Every piece of blocking review feedback must cite one accepted owner or applicable reviewed repository rule. Classify anything else as a brief omission, authority contradiction, unresolved product decision, or new capability. Compare the architecture declaration with the final diff, requiring the named authority change in the same candidate when it is `update-required`. Re-run the material-limitation classification against the implementation so a newly exposed cutoff, fallback, security assumption, scale boundary, or foundational workaround cannot enter through generic authority; an unaccepted finding stops only its affected work and returns to the owning task for scope correction or a product decision.

When an exact accepted checkpoint package is supplied, treat its evidence as historical claims rather than a blanket waiver. Give every family one complete disposition:

- **Reused:** the owner, assumptions, neighboring contracts, and consumers remain unchanged, and the reviewer names that relationship basis.
- **Revalidated:** the owner or behavior changed, so the relevant source and check were rerun against the current candidate.
- **Stale:** a changed shared boundary, assumption, neighbor, or consumer invalidates the older observation even when the selected file hash is unchanged; reread the affected relationship before deciding its current disposition.

For correction review, independently compare the caller-selected return receipt candidate and the candidate identified in current `review.md` with the current candidate. The receipt preserves the historical candidate and reason; the review file is mutable and is not linked to that receipt. Stop without a verdict if either required identity is absent, cannot be resolved, has no comparison range, or diverges. Resolve every prior finding and return one complete disposition rather than silently dropping findings or repeating the whole semantic audit without cause.
