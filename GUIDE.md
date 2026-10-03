# Using Pinboard in conversation

Pinboard helps you keep development work and its decisions available across conversations. You can ask your coding agent in ordinary language; it records the work, explains what can happen next, and brings product choices back to you. It works with local Git-backed repositories. The examples below describe requests you can adapt, not commands you must memorize.

Saving a **work item** records a goal for later. Starting it creates an **attempt** with an agreed brief. A separate coding agent reviews an implementation candidate before Pinboard records completion. You remain responsible for product decisions and what happens to reviewed repository changes. For the underlying model, see [How Pinboard works](HOW_IT_WORKS.md).

Replies are written for someone who may come back hours later. Before work starts, the agent says what it will produce and where it will land. Replies lead with what needs you. When something you might forget is still open, such as a decision, an unmerged change, or a review, a reply ends with a short “Still open” line. Words like done, saved, reviewed, and merged say what they cover, including whether a change is in `main`.

## Save an idea, or start it now

> I'm worried about invalid database records. Save that issue for later, and keep going with what we're doing.

The agent records a ready work item with the concern, evidence, and relationship to any current work. It gives you a link to the saved item when its readable view is available, then returns to the current task. Saving does not begin implementation or raise its priority automatically. If it is already recorded with the same consequence, the agent points you to that item.

A typical reply is: “Saved for later: the database issue is on the board, not started. I'll keep working on this change.” Saved means recorded; it does not mean scheduled or in a release.

> Let's fix the database issue now.

Here, saving is only the first step. The agent checks the goal you agreed on, dependencies, checkout, and any choice that could change the result. It prepares a brief and starts the work when those conditions are settled. You decide material changes to scope or behavior; routine implementation choices stay with the agent.

If the intended fix is unclear, it asks only what would change the result, each question with its recommendation: “Should this change prevent new invalid records, repair existing records, or both? I'd start with preventing new ones.” It also says where the work will land: “It will be done on its own branch and reviewed; nothing reaches `main` unless you choose.” Your answer shapes the goal before implementation.

## Decide what to do next

> What should we work on next, and why?

The agent shows current work and the saved priority order, then explains which unstarted items are ready or blocked by dependencies. It can recommend the next item using the recorded goal and evidence. A recommendation does not reorder the backlog or start anything.

For example: “API cleanup is first on your list and isn't waiting on anything. Logging still depends on another change. I'd start with API cleanup.” You can ask the agent to start it, choose another ready item, or change the order explicitly.

> Move the API cleanup ahead of the logging improvement.

The agent checks the current order and records the priority change you chose. Priority does not remove a dependency or interrupt an attempt already underway. You can also ask, “Do these two ideas belong together?” The agent presents the proposed coverage and effects on dependencies and order first; you decide whether it applies any change.

## Review a change and correct it

> Let's tackle the API cleanup next. Let me know what the review finds.

The agent works from the agreed brief, verifies the change, and saves the exact version it wants reviewed with its results. A separate coding agent reviews that version against the brief. The owning agent reports the review outcome and any material concern. A published pull request alone does not count as this review.

A review update could say: “The reviewer found a case the cleanup misses. I'll fix it and have the change reviewed again.”

If the reviewer finds an implementation defect, the agent can correct the same work and send the revised change for another review. If the finding would change the agreed product goal, architecture, or compatibility, the agent asks you to settle that decision before affected work continues. You choose what happens to a favorably reviewed change in the repository; Pinboard records completion only after its required review and authorized repository steps are satisfied.

## Pick up interrupted work

> Where did we leave off with the API cleanup? Please pick it up if the plan still fits.

The agent reads the saved goal, brief, current work, and review evidence, then explains the next supported step. It can continue active work, resume paused work, or restore the saved change into a suitable clean checkout when that recovery applies. If the agreed scope or Git history changed, it updates the brief through the supported path before continuing. A pause preserves the work; it is not proof of completion. The agent asks you only when a material choice or missing authority blocks the next step.

For example: “The change is ready, but it hasn't been reviewed yet. I can send the saved version for review.” A pause keeps its reason on the board, so a later conversation can still see what the work waits on. When paused work waits on your choice, the agent names the choice and its recommended default, then says what it will do once you answer.

## Try something without committing to it

> On a throwaway branch, make the parser skip comment lines. I'm not committing to merging it; I just want to see whether it's worth it.

The agent can track an experiment like any other work, including on a scratch board you name, and have it reviewed. Before it starts, it says where the result will stay: on its own branch, not in `main`. When you close the experiment, it says what closed and what did not reach `main`.

If you later ask to include the useful part, the agent says whether it is in `main` and where it lives, and prefers the already reviewed commit over redoing the work. When you keep an experiment's changes, the agent records where its scratch board is on the owning item, so a later question about that branch leads back to the board and its review. If it cannot find the earlier review, for example because the board was never recorded or no longer exists, it says so rather than assuming one. After you merge something yourself, it says whether the earlier review covered exactly what you merged: “The review covered exactly the commit you merged. Your merge commit itself wasn't reviewed, but it adds nothing else. I haven't seen CI results.”

## Explore parallel work

> What can we work on at the same time?

The agent shows what is ready together and why other items are excluded. This preview is read-only: it does not launch tasks or certify that each brief is ready.

A preview might say: “We can work on API cleanup and documentation at the same time. Logging needs the API change first.” You choose an exact subset or ask to launch all of the safe set.

> Please start both of those at the same time.

That request authorizes the displayed safe batch. The agent checks it again before each launch and reports which outcomes actually started. Bounded work belonging to one outcome can return through a subagent; genuinely separate outcomes can use separate conversations that you follow individually. If the safe set changes, the agent stops the remaining launches and brings the changed choice back to you. Separate checkouts reduce competing writes, but integration can still need review.

## Account for work that already happened

> I think this change was merged. Can you check whether anything is left to do?

The agent compares the repository result with the goal you agreed on, the saved change, and its review. For an agreed target, it reads the item’s integration status against that target’s current local content, which can recognize rebase and squash merges. The read does not fetch; `content-not-present` does not prove integration never happened after later overlapping edits or changed merge resolution. It checks whether review, your repository decision, or cleanup is still missing and continues the same work where possible. A merge or closed pull request alone does not prove the work is complete. If work was saved but never started, you can instead decide to close it as completed or dropped; the agent records that choice without inventing an implementation review.

An honest reply might be: “The change is in `main`, but no review covered it. I'd have a separate reviewer check the commit as it landed before the work is closed.” If the work was never started, your decision to mark it completed or dropped takes the shorter close route.

## Review a pull request owned by a person

> Can you review my pull request? I wrote the code; please tell me what you find as it changes.

This route is for a ready item whose PR author is a person rather than a Pinboard worker. When the review should be recorded on the board, the agent records a brief linking the PR to current requirements, expected behavior, consumers, owners, and repository criteria, and a separate reviewer checks that brief before the first recorded round. Each round records the full commit the reviewing agent observed, where it observed it, findings, verification limits, and what happened to earlier findings. The agent reports the findings to you in conversation; when it reviews only in conversation, it says that no review round is recorded on the board.

For example: “I found one concern in the commit I reviewed. I've since seen a newer commit. Would you like me to review that too, or finish with the review I've completed?” Your choice controls the next round or close; it does not turn an unreviewed commit into reviewed evidence.

If the agent observes a newer commit, you can request another round or direct it to stop at the last reviewed commit. Closing the review requires your direction, even if no round was completed. The final record retains remaining concerns and any newer observed but unreviewed commit. This route creates no implementation attempt or candidate, and the PR author does not become a Pinboard worker. Pinboard records what the reviewing agent observed; it does not verify the current remote head, hosted checks, or publication.
