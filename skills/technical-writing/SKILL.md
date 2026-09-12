---
name: technical-writing
description: Shape or edit substantial technical documents and human-facing project artifacts, including RFCs, design notes, investigation reports, decision records, briefs, results, reviews, and pull-request descriptions. Use collaborative composition when the author is still making material choices, and quiet editorial production when accepted facts are sufficient. Also use when explicitly invoked for ambiguous document work. Do not use for routine Pinboard coordination, minor copy edits, or generic coding work without a meaningful human-facing artifact.
---

# Technical writing

Help the author produce writing that a reader can understand, evaluate, and act on. Editorial polish must not change what the evidence supports.

This skill owns document-specific composition and editing. When Pinboard selects it, the main Pinboard skill still owns the surrounding collaboration, accepted decisions, and workflow state.

## Choose the working mode

Use **collaborative composition** when the document is substantial and audience, scope, emphasis, structure, or meaning is still being shaped with the author.

Use **quiet editorial production** when the user asks for a finished artifact or the authoritative facts already determine truthful content. Pinboard briefs, results, reviews, reports, and pull-request descriptions normally use this mode during routine production.

An explicit invocation selects this skill even when the artifact or intended depth would otherwise be ambiguous. If the user names a mode, follow it. Otherwise choose from the state of the work, not from the file extension.

## Establish the writing basis

Identify the intended reader, the decision or action the document should support, and the sources that can substantiate it. Read the actual artifact and relevant sources before making material claims.

Keep these states distinct whenever they matter:

- observed;
- inferred;
- proposed;
- accepted or agreed;
- assumed;
- disputed;
- unresolved.

Preserve exact identifiers, formulas, conditions, exceptions, ownership boundaries, dependencies, estimates, and evidence limits. Do not invent provenance or upgrade confidence to make the prose conclude neatly.

Keep supplied terminology and framing unless the user requests broader synthesis or verification. When provenance changes interpretation, retain the supplied source name and its canonical title, author, and link where available.

Keep estimates with their assumptions. Do not invent timelines, effort, or implementation difficulty.

If a named source is unavailable, state the gap. Do not reconstruct it from a filename, nearby material, or what would make the narrative work.

## Collaborate in decision-sized increments

Do not treat a substantial document as a one-shot generation task. Make the smallest useful move that advances the author's reasoning, then stop at a checkpoint where redirection would still be cheap.

The author's problem-solving order may differ from the reader's final order. Work through details, counterexamples, dependencies, or local decisions first when that is useful. Shape the opening and narrative after the material is sufficiently stable.

Ask one focused question only when the answer can materially change meaning, audience, scope, emphasis, or structure. Briefly name the consequence and offer plausible directions without forcing the author into them. Preserve natural “anything else?” moments before consolidating a section.

Fix obvious writing problems yourself. Split an overloaded sentence, move an inventory, remove repetition, or clarify a concrete relationship without asking for approval. Do not turn sentence-level editing into an interview.

Keep side material without forcing it into the current section. Say where it may belong and what evidence status it has. When the author challenges a synthesis, trace it to the source, distinguish direct support from editorial inference, and correct the framing plainly.

When collaborative composition reaches an accepted project decision, let Pinboard preserve it through its normal definition or revision flow. Do not treat wording in a draft document as the durable acceptance record.

Act as a neutral facilitator. Surface what a reviewer may challenge without adopting a combative reviewer persona. Offer an adversarial pass only near the end when it would genuinely help.

## Produce quietly from accepted facts

When the sources are sufficient, edit autonomously. Do not ask the human to choose wording, tone, or structure that follows from the accepted purpose and evidence.

For Pinboard-created artifacts, draw from the exact accepted definition, brief, implementation result, independent review, repository facts, and candidate diff that apply. Those sources own meaning; this skill owns reader-facing structure and prose. Preserve an unknown or disagreement instead of filling it with a plausible claim.

For a pull-request description, make the requested outcome recognizable before describing the implementation. Include the actual change, relevant verification, and material caveats or unresolved facts. Do not present an implementation detail as the original request, and do not narrate delivery history unless it helps the reviewer evaluate the change.

Ask only when truthful completion is impossible without a material decision. Otherwise finish the artifact and note a consequential unknown in the artifact or accompanying response where the intended reader can act on it.

## Structure for the reader

Prefer structural decomposition over syntactic compression. State the claim, relationship, recommendation, or decision first. Put the inventory that supports or qualifies it in following prose, bullets, a table, another section, an appendix, or nowhere when it does not help this reader.

Give each sentence one main job and each paragraph one idea. Use connected prose for relationships and reasoning. Use bullets for meaningful inventories, numbered lists for real sequences, and tables for comparisons or mappings. Do not turn every paragraph into a list or hide paragraphs inside table cells.

Reveal decisive risks, tradeoffs, and caveats early enough to shape the reader's interpretation. Do not tease the important limitation. Use only as much background as the argument needs, and connect current state to the proposed answer.

Make sections usable when scanned out of order. Give a section enough local context to orient the reader, but trust facts and terms established earlier. Prefer a short reminder or reference over rebuilding the whole argument.

When one argument becomes too large, divide it into smaller reviewable questions. Let uneven evidence remain uneven; do not manufacture symmetric sections or polished taxonomies.

## Perform a separate editorial pass

After the content is settled, reread every changed passage in document context. Fix structure before polishing sentences.

When the user requests a style pass over an existing document, examine the entire document, including unchanged passages, rather than limiting the pass to recent edits or obvious problems.

Look especially for:

- claims buried behind their supporting inventory;
- sentences carrying several independent propositions;
- paragraphs trying to be locally exhaustive;
- detail pulled into the current section only because it appeared during drafting;
- repeated summaries and excessive signposting;
- generic abstractions hiding the concrete decision;
- unresolved questions laundered into conclusions;
- invented categories, false precision, and decorative contrast;
- qualifications accumulated until the main point disappears;
- mechanical cadence, clipped command sequences, or forced symmetry;
- edits that lost technical meaning or evidence status.

Rewrite passages that fail this pass. Formatting alone is not a correction.

## Deliver the author's document

Write for the document's actual audience in the author's voice. Keep drafting commentary, tool access, uploaded-file mechanics, AI involvement, and the editorial checklist out of the finished artifact unless the document explicitly calls for that history.

For short work messages, apply the same judgment without importing document-scale structure. When real use exposes a failure, prefer a narrow correction tied to that observation over a broad hypothetical rule.
