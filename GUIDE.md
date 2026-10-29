# Using Pinboard in conversation

You talk to your coding agent the way you already do. Pinboard sits underneath and keeps a record in your repository: what you want built, what was decided, what is in progress, and what is still waiting on you. The situations below are the ones that come up most in day-to-day use, roughly in order of how often. The requests are examples, so phrase them however you like.

One habit shows up everywhere. Replies lead with whatever needs you, and when something could slip, such as an unmerged branch or a question you haven't answered, the reply ends with a short "Still open:" line. For the model underneath, see [How Pinboard works](HOW_IT_WORKS.md).

## Start an item and see it through

> Start working on the export bug in a worktree.

Before touching code, the agent says in one sentence what it will produce and where it will land: "This goes on its own branch in a separate worktree; nothing reaches `main` unless you choose." If something would change the result, it asks that one question with its recommendation. Then it writes down what you agreed, builds against it, and hands the finished change to a separate agent that did not write it. That reviewer checks the exact change against the same agreement. A defect goes back for a fix and another review. Anything that would change the goal comes back to you.

If you want a pull request, the agent pushes the branch and opens it as a draft, and keeps it a draft until review and checks are done. You merge it, then come back:

> Ok it's merged, you can close it and clean up.

The agent checks that the reviewed change really is in `main`, even after a squash or rebase, removes the worktree and branch, and closes the item: "Closed on the board, and the fix is in `main`." With an agent alone, a week later you'd be reading `git log` to work out what actually landed. Here the board tells you.

## Write it down and keep going

Ideas arrive at the worst moment. Halfway through the export bug you notice the sign-up flow is clumsy:

> The sign-up flow is awkward. Don't get distracted, just save it.

The agent saves it with what it noticed and goes straight back: "Saved for later: the sign-up flow is on the board, not started. Back to the export." If the same concern is already saved, it points you to that item instead of making a second one. A flaky CI test that keeps failing your build works the same way: say it's flaky, save it, and it becomes its own item rather than a detour.

Sometimes the thought belongs to the current work:

> Also, the export should keep the column order. Add that to what we're doing.

The agent updates what you agreed before carrying on, so the review checks the change against what you want now, not what you wanted this morning. And if an idea can't wait, "save that and start working on it" does both in one go.

Without a board, these end up in a notes file, a handover document, or a follow-up list that keeps growing until nobody reads it. Here each idea is a saved item you can find, reorder, or drop later.

## Make sure it's really done

A merged branch feels finished, and with an agent alone that is usually where the conversation ends. Often the work isn't done. Before merging, ask:

> Do all the concerns mentioned here have an owner?

The agent goes through every concern raised during the work and says where each one lives: fixed in this change, saved as its own item, or waiting on you. A broadly similar item doesn't count; it names the exact one. "Two are fixed here. The retry limit is saved as its own item. The naming question is yours; I'd keep the current names."

The same care applies when you look back. A plain "where do we stand?" can turn up something you'd forgotten: "The cache change is in `main`, but no review covered it. I'd have a separate reviewer check it as it landed."

When a review already covered the change, the agent reuses it rather than auditing everything again. A squash merge changes the commit, not the reviewed content, so it doesn't trigger a new review by itself.

## Stop, restart, or hand over

> I need to restart my session.

Go ahead. Saved, in-progress and in-review work is already on disk, so there's nothing to pause. In the new session:

> Where were we?

The agent reads the saved goal, the change and its review, then names the next step: "The export fix is ready but hasn't been reviewed yet. I can send it for review."

The same goes for switching tools. If you run out of usage in Claude Code, open Codex in the same repository and ask it to pick up the export bug. The new session continues from the record, not from a pasted summary. Only one agent works on an item at a time, so if the old session or one of its helpers might still hold the work, the new one waits for that to lapse or asks you before taking over.

A real pause happens when the work needs your answer before it can go on. The agent records the question and its recommendation on the board, so a later session can see what the work is waiting for. It also warns you before a pause would throw away a finished review.

## Park the rabbit hole

Halfway through a feature, the agent finds the parser can't support it without a redesign. Instead of quietly redesigning the parser inside your feature, it stops the affected work and tells you. You decide:

> Pause this. Save the parser redesign, do that first, then come back.

The feature is paused with that reason, the redesign becomes its own item with its own review, and when it lands the agent checks that the original plan still fits before resuming. Nothing about the feature is lost while you're away.

A tempting refactoring works the same way: save it, finish what you started, and pick it up when you're ready. Not every detour is a rabbit hole, though. If the agent left some mess in the change it's working on, it cleans that up as part of the same job. And before work starts, if something you agreed would block a clearly better result, the agent says so once, with the gain and cost, and keeps to the agreement unless you change it.

## Decide what comes next

> What should I work on next?

The agent reads your saved order and the dependencies: "API cleanup is first and isn't waiting on anything. Logging needs the API change first. I'd start with API cleanup." The order changes only when you change it: "Move the API cleanup ahead of logging."

You can bring a whole roadmap over ("put these on the board") and group items under a shared goal with a way to tell whether it was met. Pinboard then orders the remaining work by what has to land first.

Big, open-ended items are where work gets lost or never starts. Split them:

> This one is too vague. Make it one narrow fix, and save the research as a separate item for later.

The narrow fix can ship this week, and the research waits on the board instead of in your head. If two saved ideas look like the same thing, ask whether they belong together. The agent shows what combining them would change and waits for your yes.

## Run things in parallel

> Check which task would be safe to work on in parallel with this.

The agent shows which saved items can run together and why the others have to wait, for example because they need a change that hasn't landed. Looking changes nothing. When you say "start those two", it checks again right before each launch, runs each in its own worktree, and tells you what actually started. Work that belongs to what you're doing now comes back to this conversation. Separate work you want to follow on its own gets its own conversation. Separate worktrees keep agents from overwriting each other, but bringing the results together can still need review.

## Investigate or experiment without shipping

Some work is about finding out:

> Investigate why exports get slow on big projects. Keep notes; this will take a few sessions.

The agent keeps an investigation note in a place you pick outside the repository, with sources, dead ends and the next useful question, so a later session picks up where this one stopped, even after a big rebase. Findings don't turn into code changes until you decide they should.

Experiments get the same honesty about where things land:

> On a throwaway branch, make the parser skip comment lines. I'm not committing to merging it.

It stays on its branch. When you close the experiment, the agent tells you what didn't reach `main`. If you later want the useful part, it points to the already reviewed commit instead of redoing the work.

## Retire what no longer matters

> Cancel the old import item; the new one replaces it.

The agent shows the exact item and what cancelling means: it's marked dropped, its code and history stay, and anything that depended on it is named. If the replacement doesn't cover everything the old item promised, it tells you what would be left uncovered. Then it waits for you to confirm that specific item. An item that was never started can simply be closed, in your own words.
