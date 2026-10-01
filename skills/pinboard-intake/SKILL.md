---
name: pinboard-intake
description: Preserve one newly proposed piece of project work as a ready item on the pinboard. Use when the user explicitly asks to add, queue, intake, preserve, save for later, or send a prerequisite, bug, cleanup, feature, contradiction, or clarification for later work, including when no board exists yet. Do not use merely because a conversation explores an idea.
---

# Add to the pinboard

Convert one explicit concern into immutable proposal facts and a same-identity ready item. Do not claim that saving it made it active or current work.

Use [the shared runtime adapters](../pinboard/references/runtime-adapters.md) for native MCP discovery, coding-agent identity and optional messaging. Intake persistence remains the correctness boundary.

Intake may be standalone or embedded in ongoing Pinboard work. Standalone intake may end after its persistence receipt only when the user did not also ask to begin the new work. Before embedded intake, retain the main skill's [continuation anchor](../pinboard/SKILL.md#safe-boundaries). Intake changes queue state but preserves active attempts, so return control to that anchor after persistence and any explicitly requested notification handling.

## Preserve immediate-start intent

When the same request says `start`, `begin`, `work on`, `implement`, `fix now`, or otherwise clearly asks for immediate execution, treat intake as the first atomic step rather than the requested outcome. After persistence and before work-brief composition, load the complete main Pinboard skill through the runtime's advertised native coordinator skill loader (`Skill` for `pinboard:pinboard` in Claude). If that loader is unavailable, read the actual sibling `../pinboard/SKILL.md` completely; unavailable complete content stops the continuation. A `$pinboard` mention is not a loaded skill. Load its [preparation and dispatch phase](../pinboard/references/preparation-and-dispatch.md) to prepare and activate the same-identity ready item, then use `$pinboard-deliver` to complete its accepted work. Do not end with a save-for-later receipt merely because the user explicitly named `$pinboard-intake`.

Report through the main skill's [human's picture](../pinboard/SKILL.md#keep-the-humans-picture-current) during that continuation. Preserve every higher-level required first-use skill disclosure, keep each one concise and outcome-oriented, and add no separate Pinboard explanation of companion-skill selection or internal routing.

Intake remains a thin caller of the main Pinboard interaction owner. Do not duplicate its collaboration rules or select `$technical-writing` merely because the proposal has a human-readable label or generated summary. When the same user request materially creates or revises a document, let the main Pinboard route make that separate selection.

Immediate-start language authorizes continuing now; it does not prove that the human agreed with an unspoken outcome, landing place, or magnitude. Give the main skill's [start sentence](../pinboard/SKILL.md#keep-the-humans-picture-current) before preparation, then continue without asking redundant permission.

Ask one quick confirmation only when the human phrasing leaves a material choice between queueing for later and beginning now. An explicit immediate-work verb is sufficient and needs no confirmation. Intake remains standalone when the request only asks to add, queue, preserve, or save work for later.

An explicitly requested notification remains subordinate to this continuation. Sending or reporting it does not complete an immediate-start request or move ownership of that outcome to the notified task.

## Preconditions

1. Before selecting the first deferred Pinboard tool schema, read and follow the shared runtime adapter's [Packaged connection and first setup](../pinboard/references/runtime-adapters.md#packaged-connection-and-first-setup) procedure. It owns connection-first native discovery and any required setup. Call Pinboard operations only through those connected MCP tools, never through Bash, Python, or `.pinboard/state.sqlite3`.
2. Resolve both roots before the first call. `project_root` is the selected checkout. Unless the user or an existing Pinboard receipt selected another work root, a normal checkout uses `<project_root>/.pinboard`; never substitute the checkout, its parent, or a containing fixture directory. Call `pinboard_overview` with those exact roots.
3. Require authority `sqlite-v7`. Intake is a direct trusted-local project action; its task and host values are audit attribution, not credentials, and it does not require a lease.
4. If the workflow or required MCP tool is unavailable, stop. Do not infer shared state from titles, recency, nearby tasks, branches, or old audit files. When overview reports no initialized board at the default work root, nothing can be saved yet: tell the human the concern is not saved and ask whether to set up Pinboard in this project, following the [setup rule](../pinboard/references/setup-and-transitions.md#start-from-executable-state). Never create board files or directories by hand, and never claim to remember the concern instead.
5. Before constructing attributed proposal fields, read and follow the `Task and host identity` row in the [shared runtime adapters](../pinboard/references/runtime-adapters.md#packaged-connection-and-first-setup) to determine the current source task identity. If that source is unavailable, ask the human for the exact task ID rather than inventing one.

## Resolve conditional follow-up authority

Language such as “if that is a production defect, follow it up” authorizes exactly one bounded intake only if current evidence proves the named condition. Test the condition before preparing a proposal:

- false or unproved: create nothing and report no saved follow-up;
- exact observation and consequence already recorded: reuse the exact durable owner and create nothing;
- proved and new: create at most one `follow-up` or `independent` proposal, whichever the evidence supports.

This conditional authority does not authorize a prerequisite relation, preparation, activation, implementation, notification, or unrelated work. Record the condition evidence, the concern's relationship to current work, and the smallest useful next decision. When intake is embedded in delivery, return to the retained continuation anchor immediately after the one permitted disposition.

## Prepare one proposal

If any proposal field or relation shape is uncertain, read the advertised `pinboard_proposal_create` input schema. Do not infer the schema from an old example or inspect Pinboard source.

Create a bounded JSON proposal containing:

- `schema`: `pinboard-proposal/v2`;
- unique kebab-case `proposal_id`;
- `created_at`;
- exact `source_task_id`;
- recognizable `user_label`;
- concrete `trigger`;
- bounded `evidence` selectors;
- `why_it_matters`;
- `relation.kind`: `independent`, `prerequisite`, `follow-up`, `duplicate`, `contradiction`, `clarification`, or `planned-replacement`;
- `relation.item`: the related item identity for `prerequisite`, `follow-up`, `duplicate`, `contradiction`, and `planned-replacement`; for `planned-replacement`, this is the affected work that the new proposal would replace; use `null` for `independent` and `clarification`;
- `relation.replacement_cost`: for `planned-replacement`, the concrete practical cost of continuing the affected work before switching to the proposed replacement;
- current product or repository `effect`;
- exact `unlock`;
- observed `urgency_evidence`, never an invented priority;
- freshness-sensitive assumptions in `freshness_assumptions`;
- `checkout_policy`: `main`, `isolated`, or `coordinator-selected`;
- `obligations`: at least one object with a kebab-case `obligation_id`, a `statement` of the outcome implementation must produce, and `deferral_policy` `allowed` or `forbidden`;
- optional one-based `position`; omit it to place the ready item at the back of live work.

Keep the obligations about the product or repository outcome that implementation must produce. A user request to use isolation, obtain independent review, integrate or publish an accepted candidate, clean up disposable checkouts or branches, and terminally close the item authorizes the owning coordinator's outer workflow; it is not implementation scope and must not become a proposal obligation. Preserve that authority in the current task context and follow it after candidate review. Use `checkout_policy` for the selected checkout rule rather than restating isolation as an obligation.

Use `follow-up` when the new ready item depends on the related item. Use `prerequisite` when the live related item depends on the new ready item; persistence advances that target item's immutable definition history as well as its relational dependency projection. Use `planned-replacement` when the proposed intake item would replace its affected `relation.item`; proposal creation records the ready item and explicit replacement relation together or accepts neither. Use `duplicate`, `contradiction`, or `clarification` to preserve proposal origin for later evaluation rather than inventing a dependency. Encode `relation.item` as JSON `null` for `independent` and `clarification`; the other relations require a string identity. Every new proposal also creates definition revision 1 from its immutable facts, so do not add parallel semantic prose after intake.

When the concern itself is a readiness gap, name the affected agentic-readiness capability in `why_it_matters` or `effect`.

Do not create work merely because a question was asked. Require an explicit request to preserve or submit the concern.

Before creating a proposal, distinguish exact prior coverage from a merely related theme. If the exact observation and consequence already exist in a known canonical item or proposal, do not create a duplicate merely to produce a receipt. Report `already recorded` with the exact durable selector and current state. If only a broader item exists, treat the exact concern as unrecorded.

## Persist, then deliver

Before a first default initialization or after `SQLITE_READONLY`, follow the shared runtime adapter's [Codex protected project writes](../pinboard/references/runtime-adapters.md#codex-protected-project-writes) rule. A normal checkout uses relative `.pinboard`; a linked worktree or explicit root uses only the exact absolute effective work root reported by recovery. Do not substitute the whole shared repository or treat a denied proposal write as saved intake.

1. Call `pinboard_proposal_create` with the structured proposal, exact project and work roots, and current actor task and host identities. No temporary proposal file is needed.
2. Treat `pinboard-mcp-proposal-result/v2` with status `committed` or `committed-with-warning` as proof that both the proposal facts and ready item persisted. Use its exact `proposal_id`, `position`, `item_state`, and `committed_revision`; do not scrape human output.
3. Follow the returned effect and retry disposition. An unchanged rejection may be corrected as directed; a committed effect must be inspected rather than replayed.
4. After that success, announce the generated item summary as the readable accepted-definition view only when `<work-root>/views/items/<proposal-id>.md` is confirmed available, using a concise purpose label and a native clickable link. On every later user-facing reference to that item, keep its human-facing label linked to the confirmed view under the main Pinboard skill's shared readable-artifact rule. If the command reports a generated-view warning or the file is unavailable, preserve the successful intake receipt without a broken link; after a successful refresh or rebuild confirms availability, announce it then. Do not send a standalone re-announcement after an unchanged refresh.
5. For explicitly requested delivery in Codex, read and follow the Codex-only `references/codex-transport.md`. For explicitly requested delivery in Claude Code, follow only the bounded optional-messaging behavior in the shared runtime adapters; do not read or apply the Codex transport leaf.
6. Notify the requested eligible task or teammate with the proposal ID, shared work root, and confirmed item-view link when available. Repository persistence, not messaging, is the correctness boundary.
7. Report delivery only when the user requested it or when its outcome materially changes confidence, current work, or the next action.

When a JSON-capable operation returns `pinboard-rejected-operation/v1`, use its stable code, mismatch facts, effect disposition, and retry classification. An unchanged rejection may be corrected or refreshed as directed. A committed-effect result must be inspected rather than replayed, even though the requested operation did not finish.

For embedded intake, resume the invoking task before the surrounding turn ends. If context compaction obscured the conversation, re-read the anchor's active or paused item, attempt, proposal, or exact selector rather than inventing continuation state. Complete the promised action when it remains in scope; otherwise surface its exact blocker or durably defer it at an exact owner.

When delivery was explicitly requested but transport or the requested target is unavailable, or delivery fails, retain the ready item and report the requested delivery outcome. Without an explicit delivery request, do not inspect transport, send, retry, or report notification state. Any later task can discover the item through overview or status, so never ask the human to relay it or authorize lease revocation merely to reduce notification latency.

## Result language

Report through the main skill's [concern receipts](../pinboard/SKILL.md#reconcile-material-concerns-before-reporting-them) and [the human's picture](../pinboard/SKILL.md#keep-the-humans-picture-current). Intake adds only its proposal facts: after committed creation, the owner is the new ready item `<proposal-id>` at position `<n>` on the named board, not started. Link `<proposal-id>` to its item view only when that Markdown is confirmed available; otherwise keep the receipt accurate without a link.

The `Saved for later` forms apply only when intake is the terminal action requested. For immediate-start intent, keep the persistence receipt and, only when its readable view is confirmed available, its accepted-definition-summary link subordinate while continuing the same turn. Keep that confirmed link on every later item reference. An unavailable item summary does not undo persistence or stop immediate-start continuation; report the work as started only after the normal Pinboard activation succeeds.

Use `now` only after committed proposal creation; it means this turn before the update. Notification delivery never upgrades persistence into priority. If persistence happened in response to the user's question, say that directly instead of implying the exact concern was present earlier. When delivery is user-requested or materially affects the result, report it after the durable outcome without implying that optional transport changes persistence.

When proposal creation fails, apply the main skill's unresolved `not recorded` rule: name the exact failure classification as the cause, the durable state as not saved with no owner, the current-work impact, and this task or the human as the next owner.

Treat a stale proposal view or a change in the requested target's identity or availability during explicitly requested delivery as expected concurrency. Re-resolve that same requested target and retry once when doing so needs no new authority. If requested delivery remains unavailable after persistence, stop notification work with no human action because the ledger is authoritative; this does not end an embedded caller's surrounding turn. If retry needs new authority, changes scope, or overrides another owner, ask exactly one concrete approval question. If persistence was never authorized, ask whether to preserve or dismiss the concern. Never tell the human to contact or notify the requested task.

When transport detail is material, distinguish these precise lifecycle outcomes:

- proposal prepared but not persisted;
- proposal persisted, delivery unavailable;
- proposal persisted and notification delivered;
- ready proposal later started, blocked, or deferred;
- proposal later merged into an existing item;
- proposal rejected.

Report those later outcomes only after the matching current action or authority operation commits.
