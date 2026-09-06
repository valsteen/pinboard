---
name: pinboard-intake
description: Preserve one newly proposed piece of project work as an intake item on the pinboard. Use when the user explicitly asks to add, queue, intake, preserve, or send a prerequisite, bug, cleanup, feature, contradiction, or clarification for later work. Do not use merely because a conversation explores an idea.
---

# Add to the pinboard

Convert one explicit concern into immutable proposal facts and a same-identity intake item. Do not claim that intake made it ready, active, or current work.

Intake may be standalone or embedded in ongoing Pinboard work. Standalone intake may end after its persistence receipt only when the user did not also ask to begin the new work. Before embedded intake, retain a compact continuation anchor containing the pre-intake objective, the next promised action, and the exact durable owner selector. Intake changes queue state but preserves active attempts, so return control to that anchor after persistence and any explicitly requested notification handling.

## Preserve immediate-start intent

When the same request says `start`, `begin`, `work on`, `implement`, `fix now`, or otherwise clearly asks for immediate execution, treat intake as the first atomic step rather than the requested outcome. After persistence, continue through `$pinboard` to admit, prepare, and activate the same-identity item, then use `$pinboard-deliver` to complete its accepted work. Do not end with a save-for-later receipt merely because the user explicitly named `$pinboard-intake`.

Follow the main Pinboard skill's user-facing detail threshold during that continuation. Preserve every higher-level required first-use skill disclosure, keep each one concise and outcome-oriented, and add no separate Pinboard explanation of companion-skill selection or internal routing.

Immediate-start language authorizes continuing now; it does not prove that the human agreed with an unspoken magnitude interpretation. When the work is broad, route through Pinboard's one-sentence scope confirmation before preparation: state the outcome, principal read and touch surfaces, approximate magnitude, and any surprising exclusion, then continue without asking redundant permission. Ask only if that sentence exposes a real unresolved choice.

Ask one quick confirmation only when the human phrasing leaves a material choice between queueing for later and beginning now. An explicit immediate-work verb is sufficient and needs no confirmation. Intake remains standalone when the request only asks to add, queue, preserve, or save work for later.

An explicitly requested notification remains subordinate to this continuation. Sending or reporting it does not complete an immediate-start request or move ownership of that outcome to the notified task.

## Preconditions

1. Resolve this plugin's executable relative to this file as `../../scripts/pinboard`.
2. Run `pinboard status --json` from the repository checkout.
3. Require authority `sqlite-v4`. Intake is a direct project action using the invoking task and host identity; it does not require a lease.
4. If the workflow or executable is unavailable, stop. Do not infer shared state from titles, recency, nearby tasks, branches, or old audit files.
5. Determine the current source task identity from trusted task context. If the environment does not expose it, ask the human for the exact task ID rather than inventing one.

## Resolve conditional follow-up authority

Language such as “if that is a production defect, follow it up” authorizes exactly one bounded intake only if current evidence proves the named condition. Test the condition before preparing a proposal:

- false or unproved: create nothing and report no saved follow-up;
- exact observation and consequence already recorded: reuse the exact durable owner and create nothing;
- proved and new: create at most one `follow-up` or `independent` proposal, whichever the evidence supports.

This conditional authority does not authorize a prerequisite relation, admission, preparation, activation, implementation, notification, or unrelated work. Record the condition evidence, the concern's relationship to current work, and the smallest useful next decision. When intake is embedded in delivery, return to the retained continuation anchor immediately after the one permitted disposition.

## Prepare one proposal

If any proposal field or relation shape is uncertain, read `pinboard tool-contract --operation proposal --json` and construct the artifact from its strict generated schema. This static read does not open the ledger. Do not infer the schema from an old example or inspect Pinboard source.

Create a bounded JSON proposal containing:

- `schema`: `pinboard-proposal/v1`;
- unique kebab-case `proposal_id`;
- `created_at`;
- exact `source_task_id`;
- recognizable `user_label`;
- concrete `trigger`;
- bounded `evidence` selectors;
- `why_it_matters`;
- `relation.kind`: `independent`, `prerequisite`, `follow-up`, `duplicate`, `contradiction`, or `clarification`;
- `relation.item`: the related item identity for `prerequisite`, `follow-up`, `duplicate`, and `contradiction`; `null` for `independent` and `clarification`;
- current product or repository `effect`;
- exact `unlock`;
- observed `urgency_evidence`, never an invented priority;
- freshness-sensitive assumptions in `freshness_assumptions`;
- optional one-based `position`; omit it to place the intake item at the back of live work.

Use `follow-up` when the new intake item depends on the related item. Use `prerequisite` when the live related item depends on the new intake item; persistence advances that target item's immutable definition history as well as its relational dependency projection. Use `duplicate`, `contradiction`, or `clarification` to expose a review flag rather than inventing a dependency. Encode `relation.item` as JSON `null` for `independent` and `clarification`; the other relations require a string identity. Every new proposal also creates definition revision 1 from its immutable facts, so do not add parallel semantic prose after intake.

Do not create work merely because a question was asked. Require an explicit request to preserve or submit the concern.

Before creating a proposal, distinguish exact prior coverage from a merely related theme. If the exact observation and consequence already exist in a known canonical item or proposal, do not create a duplicate merely to produce a receipt. Report `already recorded` with the exact durable selector and current state. If only a broader item exists, treat the exact concern as unrecorded.

## Persist, then deliver

1. Write the proposal to a temporary file outside canonical work state.
2. Run `pinboard proposal --file <path> --task-id <current-task> --host-id <current-host>`.
3. Treat `OK PROPOSAL_CREATED <proposal-id> position=<n> state=intake` as proof that both the proposal facts and intake item persisted.
4. After that success, mention the generated item summary once as the readable accepted-definition view only when `<work-root>/views/items/<proposal-id>.md` is confirmed available, using a concise purpose label and a native clickable link. If the command reports a generated-view warning or the file is unavailable, preserve the successful intake receipt without a broken link; after a successful refresh or rebuild confirms availability, mention the link once. Do not re-announce it after an unchanged refresh.
5. Read `references/codex-transport.md` only when the user explicitly requested delivery to another task and Codex task messaging is available.
6. Notify the requested eligible task with the proposal ID and shared work root. Repository persistence, not messaging, is the correctness boundary.
7. Report delivery only when the user requested it or when its outcome materially changes confidence, current work, or the next action.

When a JSON-capable operation returns `pinboard-rejected-operation/v1`, use its stable code, mismatch facts, effect disposition, and retry classification. An unchanged rejection may be corrected or refreshed as directed. A committed-effect result must be inspected rather than replayed, even though the requested operation did not finish.

For embedded intake, resume the invoking task before the surrounding turn ends. If context compaction obscured the conversation, re-read the anchor's active or paused item, attempt, proposal, or exact selector rather than inventing continuation state. Complete the promised action when it remains in scope; otherwise surface its exact blocker or durably defer it at an exact owner.

When delivery was explicitly requested but transport or the requested target is unavailable, or delivery fails, retain the intake item and report the requested delivery outcome. Without an explicit delivery request, do not inspect transport, send, retry, or report notification state. Any later task can discover the item through overview or status, so never ask the human to relay it or authorize lease revocation merely to reduce notification latency.

## Result language

Keep the active work as the main topic and lead with the practical outcome:

- After `OK PROPOSAL_CREATED`, say `Saved for later — <concern> is now <proposal-id> at intake position <n>; current work <continues | is blocked by it>.` When the generated item Markdown is confirmed available, add one compact link labelled `Accepted definition summary`; otherwise keep the persistence receipt accurate without linking an unavailable view.
- For exact prior coverage, say `Saved for later — <concern> was already recorded at <selector and state>; current work <continues | is blocked by it>.`
- When the user explicitly dismisses the concern, say `Not saved — <concern> was dismissed at your request; no follow-up remains.`

The `Saved for later` forms apply only when intake is the terminal action requested. For immediate-start intent, keep the persistence receipt and, only when its readable view is confirmed available, its accepted-definition-summary link subordinate while continuing the same turn. An unavailable item summary does not undo persistence or stop immediate-start continuation; report the work as started only after the normal Pinboard activation succeeds.

Use `now` only after `OK PROPOSAL_CREATED`; it means this turn before the update. Notification delivery never upgrades persistence into admission or priority. If persistence happened in response to the user's question, say that directly instead of implying the exact concern was present earlier. When delivery is user-requested or materially affects the result, report it after the durable outcome without implying that optional transport changes persistence.

When proposal creation fails, `not recorded` is an unresolved state, not a terminal receipt. Give one compact formal announcement containing:

- `Cause`: the exact failure classification;
- `Durable state`: not saved and no owner;
- `Current work`: blocked or continuing;
- `Next owner`: this task for a safe retry, or the human for one named decision.

Treat a stale proposal view or a change in the requested target's identity or availability during explicitly requested delivery as expected concurrency. Re-resolve that same requested target and retry once when doing so needs no new authority. If requested delivery remains unavailable after persistence, stop notification work with no human action because the ledger is authoritative; this does not end an embedded caller's surrounding turn. If retry needs new authority, changes scope, or overrides another owner, ask exactly one concrete approval question. If persistence was never authorized, ask whether to preserve or dismiss the concern. Never tell the human to contact or notify the requested task.

When transport detail is material, distinguish these precise lifecycle outcomes:

- proposal prepared but not persisted;
- proposal persisted, delivery unavailable;
- proposal persisted and notification delivered;
- intake proposal later accepted in place as ready, blocked, deferred, or retained intake;
- proposal later merged into an existing item;
- proposal returned for evidence;
- proposal rejected.

Report the latter four only after applying the matching direct project transition.
