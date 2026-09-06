---
name: pinboard-deliver
description: Deliver exactly one active pinboard attempt from its accepted brief, current definition identity, and renewable lease. Use when the item, checkout, definition, acceptance criteria, and verification are already recorded. Do not use for intake, portfolio selection, broad audits, design exploration, or acceptance review.
---

# Deliver from the pinboard

Deliver the accepted checkpoint of one active attempt: implement its complete scope, verify it, leave a durable result, and return it accurately for review.

Direct human invocation to start a named Pinboard item is not an attempt-establishment failure. When no already prepared active attempt was supplied, follow the main skill's [human task-start route](../pinboard/SKILL.md#route-human-task-starts-through-pinboard): give its one gentle clarification, suggest ordinary Pinboard wording, and continue through Pinboard when the item and outcome are clear. Do not enter the worker checks merely to surface missing internal preparation, and do not stop after explaining the route when Pinboard can continue.

## Establish the attempt

1. Resolve the pinboard executable relative to the installed plugin as `../../scripts/pinboard`.
2. Run `pinboard status --json` and require authority `sqlite-v4`. Stop if validation fails or another authority is reported; never infer current state from generated views or archived files. Acquire or validate the user-supplied attempt lease, then run `pinboard actions --role worker` with its lease identity and fencing generation.
3. Require the user-supplied attempt to be present and active. Other disjoint attempts may also be active; their presence alone is not a user-facing condition. Stop if state is invalid, the supplied attempt is absent, its item and attempt records disagree, or another unexpired owner holds it. Follow the main skill's [contention-reporting rule](../pinboard/SKILL.md#match-detail-to-the-question) instead of guessing or silently revoking it.
4. Read the attempt's accepted canonical `.json` brief fully and read `pinboard item definition --item-id <item> --json`. Require the brief's accepted revision and digest to match that current definition before continuing. Its generated Markdown view is inspection convenience, not an editable or parseable contract. Then read only the project guidance, accepted definition, accepted knowledge, and, for a local checkpoint, the source authorities the JSON record names. For a cross-boundary checkpoint, treat its reviewed selectors and digests as exact preparation receipts rather than preloading every selected body. Before editing each changed contract, derive and read from the dispatched source checkout its minimum concrete implementation set: the decision owner, effect or persistence owner, direct callers and consumers, retained boundary conversions, and the cheapest real boundary evidence capable of falsifying agreement. Do this even when the brief is complete. Read the smallest additional exact source when implementation needs its detail, the brief is ambiguous or incomplete, a reviewed digest changes, correction evidence changes an owner, a neighboring contract depends on it, or the worker must edit or trace it. Before loading several or potentially large sources, create a temporary `pinboard-brief-sources/v1` manifest and run `pinboard brief-sources --file <manifest> --json` to measure that set. Read each emitted batch once in order. Preserve exact selectors and selected digests; reuse unchanged receipts and reread only changed owners and dependent neighbors. If output truncates, continue at the first unread batch or line without replaying returned content. Preparation and independent brief review still use their complete-source procedures; dispatch digest validation and separate final candidate review remain unchanged.
5. Inspect the checkout, branch/worktree, base revision, and unrelated user changes before editing.

When exact CLI syntax, authority fields, payload shape, or retry safety is unclear, use the installed `pinboard tool-contract --json` index and then its selected operation or action detail. Do not inspect Pinboard implementation source or guess optional fields. On `pinboard-rejected-operation/v1`, follow the declared retry disposition and any fresh same-subject action receipt. Never replay a mutation after `status: committed-effect`; inspect the named changed surfaces and current attempt continuation first.

Confirm the attempt identity, current definition revision and digest, stable checkpoint ID, and execution environment without rewriting its semantics. Treat the dispatched starting revision and permissions as brief declarations that may narrow the work; they do not grant authority beyond the user and execution environment, and Pinboard does not enforce them. The canonical JSON brief remains the sole source for execution ordering, deferrals, and verification, while the matching immutable definition owns accepted item semantics. Ask only when missing information would change product behavior, architecture, scope, compatibility, or verification expectations.

When reacquiring an attempt returned from review, read the durable correction reason and `review.md` before editing. Keep the same accepted brief, branch, evidence, and attempt identity. Treat the earlier `result.md` as preserved history, not a current readiness claim; refresh it only after the corrected candidate is stable and all required checks pass.

## Stay inside the attempt

- Edit only what the attempt requires.
- Keep one writer per checkout. Disjoint attempts may proceed concurrently in separate checkouts.
- Preserve unrelated user changes.
- Follow the repository's own testing, formatting, lint, documentation, and safety guidance.
- Treat a stale instruction as an instruction defect before reshaping working code around it.
- Do not edit generated views, SQLite authority, preparation or attempt leases, or another item's lifecycle state outside the executable workflow.
- Do not accept or complete your own item.
- Prepare the stable candidate for independent review, then follow the current ownership and review-return rules below. `pinboard dispatch` prepares the exact launch prompt but does not create the worker task.

Worker diff inspection, requirement mapping, and fresh verification are pre-review evidence. They do not replace independent review.

For a cross-boundary attempt, map every observable behavior and behavior-defining test to an authorized Contract row before adding it. The row's `Authorization basis` must resolve to the current accepted scope or to an exact reviewed authority family. Ordinary internal choices that preserve supported behavior—such as helper names, local refactors, and equivalent algorithms—need no separate provenance record.

When cross-boundary implementation extends behavior governed by interacting conditions or operations, read and apply [Preserve independent decision gates](../repository-readiness/references/developer-navigation.md#preserve-independent-decision-gates) through the accepted contracts and evidence. Do not load the method for an ordinary change with no conditional interaction.

When the accepted checkpoint introduces or changes a command or closed-variant family across several production owners, or places dynamic dispatch on that path, read the shared [developer-navigation lens](../repository-readiness/references/developer-navigation.md) and implement the accepted trace and sibling-change shape. If implementation exposes another same-meaning routing site, hidden fallback, or dynamic wiring owner absent from the accepted brief, treat it as a brief omission rather than silently spreading or removing the distinction.

When the canonical brief explicitly selects the optional engineering-health baseline, read the shared [engineering-health baseline](../pinboard/references/engineering-health-baseline.md) and apply it only through the brief's authorized contracts and verification. Do not infer the selection from perceived complexity, architecture impact, or the number of changed files.

Run every basis-bearing entry in the accepted `Verification` section as a mandatory check. Do not add a tool, threshold, platform promise, compatibility obligation, or hardening check to that mandatory list unless the brief gives it an accepted basis. Proportionate exploratory checks remain available when they help implementation, but they do not become acceptance obligations merely because they were run or suggested during review.

When a useful addition has no authorized row, stop widening that part of the implementation and report the unsupported addition. Continue independent in-scope work when possible. Create an intake proposal only when the user explicitly authorizes preservation; proposal creation does not authorize implementation. Do not invent a ledger state or transition to represent the discovery.

If additional work is useful but not required, invoke `$pinboard-intake` only when the user explicitly wants it preserved. Otherwise mention it in the result without creating shared state.

When authorized intake is nested inside delivery, retain the attempt ID, current accepted objective, and next promised implementation or verification action as a continuation anchor. After the proposal is persisted and optional delivery handling ends, return to that action. Intake alone does not pause or reprioritize the attempt. Before the final result, account for every announced pending action as completed, durably deferred at an exact owner, or blocked by one exact decision.

If a discovered problem blocks the attempt:

1. stop widening the implementation;
2. preserve the current commit/worktree and verification;
3. write `blocker.md` in the active attempt directory with the observation, affected criterion, completed work, and safest next action;
4. return the blocker to the owning task with one concise purpose label and one native clickable link to that Markdown;
5. use `$pinboard-intake` to propose a prerequisite when explicitly requested;
6. use the worker-visible `report-blocker:<attempt>` affordance to report that preserved evidence; it is advisory and has no mutation payload;
7. leave shared lifecycle mutation to the owning task, which must select the exact project action `block:<attempt>` only for dependencies already accepted in the current definition or `pause:<attempt>` when no accepted dependency condition applies; a newly accepted dependency requires a complete item revision and revised-brief recovery, and `block-item:<item>` is only for unstarted intake work.

## Implement and verify

Use the repository's selected testing mode. Prefer the smallest evidence that can disprove the important failure, then run the broader changed-surface gate required by the attempt.

Continue through the complete accepted checkpoint while in-scope work can proceed without user input. A green internal seam, implementation milestone, or long-running turn does not by itself warrant commentary; follow the main Pinboard skill's user-facing detail threshold. Once implementation begins, end delivery only with a stable candidate and `result.md` ready for review, a `blocker.md` naming the exact condition that prevents further work, or an explicit user request for a partial stop or background execution. If the execution host forces an earlier return, say so plainly, preserve the exact continuation point, and state whether any user action is required.

Immediately before writing or refreshing `result.md`, account for every user direction and repository change after the current accepted brief or exact candidate cut point. If any change alters accepted semantics, do not present or submit the stale candidate. Return control to the owning task so it can replace the complete definition, publish the matching brief, clear stale review identity through the state-appropriate project transition, and obtain a new exact-candidate review. Work that implements the unchanged brief and evidence that supports it may proceed normally.

Before review:

1. finish the complete accepted checkpoint;
2. run every command required by the attempt, without replacing it with a narrower package, test, formatter, or linter command;
3. inspect the final diff;
4. identify the stable candidate by commit or working-tree fingerprint;
5. map every acceptance criterion to code, test, or evidence;
6. if an existing test file shrank materially, inventory the removed behavior or test names and identify the replacement evidence;
7. when acceptance claims lifecycle wiring, prove it through the production entry point rather than only through an internal primitive;
8. write `result.md` in the attempt directory.

When `result.md` is new or materially refreshed, return it to the owning task with one concise purpose label and one native clickable link. Do not repeat an unchanged result link in later status updates.

The result must record:

- candidate identity and changed files;
- concise implementation result;
- acceptance-criterion evidence;
- verification commands and outcomes;
- any material test removal and its replacement evidence;
- production-entry-point evidence for lifecycle claims;
- for a cross-boundary checkpoint, startup reviewed-source count and bytes, each changed contract's concrete implementation source set, every on-demand read and trigger, accepted-decision coverage, discovered defects, implementation outcome, and the separate final-review outcome or its pending status;
- preserved unrelated changes;
- new concerns or exact unknowns;
- whether the attempt is ready for review or blocked.

Report source reduction and correctness separately; fewer reads are not acceptance evidence.

When the accepted checkpoint is one of several recorded for the item, report its exact candidate and the brief's remaining-work boundary without claiming that the whole item is complete. Do not turn an internal implementation seam into an unrecorded checkpoint. Checkpoint acceptance belongs to the owning task after review; the worker does not archive its own checkpoint evidence or resume the next checkpoint.

## Return the candidate for review

`result.md` makes the candidate durably ready for review. It does not by itself notify another task. The worker return includes exactly one purpose-labelled native clickable link to the current result so the owning task can inspect and surface the implementation evidence; a corrected candidate links the refreshed result once.

After the owning task submits the exact candidate, run `pinboard attempt inspect --attempt-id <attempt> --json` and follow its derived continuation. When it names review, run `pinboard review-job --attempt-id <attempt> --candidate-revision <candidate> --json` and launch its prompt unchanged as one fresh-context, candidate-read-only subagent. The job binds the current candidate, canonical accepted brief, outcome owner, and current nonempty `result.md` path and digest; the reviewer verifies those identities again before use. Rendering it is read-only and does not accept or complete the candidate.

Apply the [current-responsibility review route](../pinboard/SKILL.md#coordinate-review-responsibility-and-checkout-use). By default, the task that owns this outcome commissions that reviewer and processes its complete verdict. The review result returns automatically to the owning task. In user-facing updates, call this `review by a separate Codex reviewer`; reserve `ready for your review` for an actual human review request. An exact `source_thread_id`, prior dispatch, scope clarification, or earlier message is not sufficient reason to wake another task.

Do not send a task-to-task completion or review message for subordinate work. If this attempt belongs to a separate task because it is a genuinely independent outcome, report its result and request decisions in that task's own conversation. If subagent creation is unavailable, leave the exact candidate in review and report the missing runtime capability; do not create or wake a user-owned task, hand routine ownership to a parent task, or substitute the implementer as reviewer.

Do not claim canonical completion until the owning task applies the completion transition after review.

If the attempt was returned for correction, report the new candidate normally. Do not present the return itself as a new concern or imply that the earlier review was accepted. The compact human outcome is: `Correction ready — <candidate>; the same attempt has been resubmitted for review by a separate Codex reviewer.`
