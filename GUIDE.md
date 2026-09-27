# Using Pinboard in conversation

Pinboard helps you keep development work and its decisions available across conversations. You can ask your coding agent in ordinary language; it records the work, explains what can happen next, and brings product choices back to you. It works with local Git-backed repositories. The examples below describe requests you can adapt, not commands you must memorize.

Saving a **work item** records a goal for later. Starting it creates an **attempt** with an agreed brief. A separate coding agent reviews an implementation candidate before Pinboard records completion. You remain responsible for product decisions and what happens to reviewed repository changes. For the underlying model, see [How Pinboard works](HOW_IT_WORKS.md).

## Save an idea, or start it now

> Save the database concern for later. Keep working on the current task.

The agent records a ready work item with the concern, evidence, and relationship to any current work. It gives you a link to the saved item when its readable view is available, then returns to the current task. Saving does not begin implementation or raise its priority automatically. If it is already recorded with the same consequence, the agent points you to that item.

A typical reply is: “Saved the database concern for later. The current task continues; ask me to start the saved item when you want to work on it.”

> Use Pinboard to fix the database concern now.

Here, saving is only the first step. The agent checks the accepted goal, dependencies, checkout, and any choice that could change the result. It prepares a brief and starts the work when those conditions are settled. You decide material changes to scope or behavior; routine implementation choices stay with the agent.

If the intended fix is unclear, it may ask: “Should this change prevent new invalid records, repair existing records, or both?” Your answer becomes part of the accepted goal before implementation.

## Decide what to do next

> What should we work on next, and why?

The agent shows current work and the saved priority order, then explains which unstarted items are ready or blocked by dependencies. It can recommend the next item using the recorded goal and evidence. A recommendation does not reorder the backlog or start anything.

For example: “API cleanup is first in the saved order and has no open dependency. Logging is blocked by its prerequisite. I recommend API cleanup next.” You can ask the agent to start it, choose another ready item, or change the order explicitly.

> Move the API cleanup ahead of the logging improvement.

The agent checks the current order and records the priority change you chose. Priority does not remove a dependency or interrupt an attempt already underway. You can also ask, “Compare these two saved items and show whether they belong together.” The agent presents the proposed coverage and effects on dependencies and order first; you decide whether it applies any change.

## Review a change and correct it

> Start the accepted API cleanup. Show me what the separate reviewer finds.

The agent works from the accepted brief, verifies the change, and submits an exact candidate with its result. A separate coding agent reviews that candidate against the brief. The owning agent reports the review outcome and any material concern. A published pull request alone does not count as this review.

A review update could say: “The separate Codex reviewer found one defect in the accepted behavior. I’ll correct it in this attempt and submit the new candidate for another review.” In Claude Code, the update names a separate Claude Code reviewer instead.

If the reviewer finds an implementation defect, the same attempt can return for correction. The worker addresses the finding, verifies a new exact candidate, and a separate reviewer checks it again. If the finding would change the agreed product goal, architecture, or compatibility, the agent asks you to settle that decision before affected work continues. You choose the repository disposition of a favorably reviewed change; Pinboard records completion only after its required review and authorized repository steps are satisfied.

## Pick up interrupted work

> Where did the API cleanup stop? Continue it if the agreed scope is still current.

The agent reads the saved item, attempt, brief, and current evidence, then explains the next supported step. It can continue an active attempt, resume a paused one, or restore the exact accepted candidate into a suitable clean checkout when that recovery applies. If the accepted scope or Git lineage changed, it updates the brief through the supported path before continuing. A pause preserves the work; it is not a new item or proof of completion. The agent asks you only when a material choice or missing authority blocks the next step.

For example: “The change is implemented, but the separate review has not started. I can continue with that review using the saved candidate.”

## Explore parallel work

> Which saved items can run independently right now?

The agent shows what is ready together and why other items are excluded. This preview is read-only: it does not launch tasks or certify that each brief is ready.

A preview might say: “API cleanup and documentation can run together. Logging waits for the API change.” You choose an exact subset or ask to launch all of the safe set.

> Launch all the safe work in parallel.

That request authorizes the displayed safe batch. The agent checks it again before each launch and reports which outcomes actually started. Bounded work belonging to one outcome can return through a subagent; genuinely separate outcomes can use separate conversations that you follow individually. If the safe set changes, the agent stops the remaining launches and brings the changed choice back to you. Separate checkouts reduce competing writes, but integration can still need review.

## Account for work that already happened

> This change may already be merged. Check whether the saved item is complete and finish its record if the evidence supports it.

The agent compares the current repository result with the accepted goal and any protected candidate and review. It identifies missing review, disposition, or cleanup evidence and follows the same attempt where possible. A merge or closed pull request alone does not make an accepted attempt complete. If an item never had an attempt, you can instead explicitly decide to close that unstarted item as completed or dropped; the agent records that decision without inventing an implementation review.

An honest reply might be: “The change is merged, but this accepted attempt has no completed candidate review. I’ll check whether its exact candidate can be reviewed before recording the item as complete.” If the item was never started, your explicit completed-or-dropped decision takes the shorter close route.

## Review a pull request owned by a person

> Use Pinboard to review my pull request. I own the code; report findings for each commit you review.

This route is for a ready item whose PR author is a person rather than a Pinboard worker. The agent records a brief linking the PR to current requirements, expected behavior, consumers, owners, and repository criteria. A different task reviews that brief before the first review round. Each round names the full commit observed by the coding harness, its observation source, findings, verification limits, and what happened to earlier findings. The agent reports the findings to you in conversation.

For example: “I reviewed `<full commit SHA>` and found one concern. The coding harness observed a newer commit that has not been reviewed. Should I review that commit too, or close at the last reviewed commit?” Your choice controls the next round or close; it does not turn an unreviewed commit into reviewed evidence.

If the agent observes a newer commit, you can request another round or direct it to stop at the last reviewed commit. Closing the review requires your direction, even if no round was completed. The final record retains remaining concerns and any newer observed but unreviewed commit. This route creates no implementation attempt or candidate, and the PR author does not become a Pinboard worker. Pinboard records the harness observation; it does not certify the current remote head, hosted checks, or publication.

**Availability:** This human-owned PR review route is present in the current source checkout. An older installed Pinboard plugin may not expose it, so update that installation before asking it to run this flow. Do not treat the source guide as proof that an older installed version can perform the review.
