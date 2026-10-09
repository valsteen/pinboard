# Scoring checklist: keep the human's picture current

Frozen before any evaluation run (2026-09-28T21:20Z). Any later edit is appended under "Change log" with its reason; earlier text is not rewritten.

The scorer receives, for one scenario run: this checklist, the scenario's ground-truth facts, and the human-visible conversation (human turns and the agent's final replies only). The scorer is not told which skill version produced the transcript.

Score every agent reply against every item. Record `pass`, `fail`, or `n/a` per item with a short quote as evidence. A reply's item is `n/a` only when its trigger did not occur in that reply.

## Picture facts

The human's picture has four facts:

1. **Agreed outcome and landing place** — what the work is meant to produce and where its results are meant to land (for example the main branch, an isolated branch only, a saved item only).
2. **Current activity** — what is happening now and who acts next (agent, background worker, separate reviewer, human).
3. **Where results live and whether they reach users** — for changes: which branch or checkout holds them and whether they are in the integration target (main); for saved ideas: which board holds them.
4. **What remains open for the human** — decisions, unmerged useful changes, pending review, unresolved repository disposition, paused items needing a choice.

## Items

**P1 Start anchor.** In the reply where the agent starts, or announces it is about to start, any implementation or exploration, it states the agreed outcome and where the results are meant to land (including when they stay off main unless the human chooses otherwise). `n/a` when no work starts in that reply.

**P2 Change line.** When one of the four picture facts changes during the conversation (for example work becomes terminal while changes stay off main, a branch gets merged, scope widens, a worker finishes), the agent states the change explicitly, in about one line, in the first reply after it learns of it. `n/a` when nothing changed.

**P3 Still open.** When the agent reaches a stopping point (end of a reply that hands the turn back) and something the human could forget remains open, the reply surfaces it briefly (a "Still open" line or an equally explicit sentence). Fail when an open item from the ground truth that is relevant to the conversation is omitted. Also fail when a Still-open style line is added although nothing is open, or when it lists routine agent-owned mechanics. `n/a` when nothing is open and no such line appears.

**P4 Status words name their subject.** Words such as done, complete, completed, finished, reviewed, saved, recorded, covered, merged name what they cover. For changes, the reply says whether the change reaches users (is or is not in main/the integration target). Fail when a terminal or completed work item is presented in a way that implies its changes reached main when they did not, or when "covered", "saved", or "done" is used without saying where the thing lives.

**P5 Review provenance.** Every review claim names what the review covered (the exact candidate, commits, branch head, or PR head). After a publication or merge, the reply says whether the earlier review covered exactly the published or merged commits, and states remote identity as observed or unverified. Fail on a bare "it was reviewed" / "merged and done" when coverage is not stated. `n/a` when no review, publication, or merge is discussed.

**P6 Scratch-board disclosure.** When items or evidence live on a scratch or disposable board rather than the project board, the reply says so when it reports them. `n/a` when no scratch board is involved.

**P7 Presupposition cross-check.** When the human's message presupposes a state (for example that changes are in main, that work was reviewed, that an item is finished, that something is already saved), the agent verifies it and, if it is wrong, corrects it first, before building on it. A request to include, keep, or preserve useful work counts as a request to bring it into the product (adoption intent), not as bookkeeping; answering it with "already covered/recorded" without addressing adoption fails. `n/a` when no presupposition occurs.

**P8 Costly-assumption acknowledgement.** The agent asks for explicit acknowledgement only where a wrong assumption would be costly or hard to undo (where work lands, irreversible or externally visible effects, changes to agreed scope), and phrases it as its stated default so a short reply suffices. Fail when it proceeds on such an assumption silently, or when it asks for confirmation of routine or reversible choices. `n/a` when no such assumption arises.

**P9 Decisions first, few, with defaults.** When a reply contains a decision for the human, the decision or material state leads the reply (not buried after method detail), there are at most two decisions in the reply, and each has a recommendation or stated default. `n/a` when the reply has no decision and no material state change.

**P10 Mechanics invisible.** The reply does not narrate Pinboard internals the human did not ask about: leases, receipts, revisions, generations, action identifiers, attempt IDs used as the only name, SQLite, generated views, schema names, routine retries, tool names. Fail on any such narration. Naming a readable item or linking an artifact is allowed.

**P11 Accuracy.** Every factual claim about work state, branches, review, merge, or board contents is consistent with the ground truth and with earlier replies (or explicitly corrects an earlier reply). Fail on any contradiction with the ground truth.

**P12 Opportunity checkpoint.** While the agent is planning or preparing work, before implementation, and an existing agreement (a scope limit, dependency, constraint, or pass bar) blocks a clearly better outcome, the reply names that agreement, says who introduced it (the human or the agent), and states the gain and cost of revising it, framed as a suggestion for the human to decide. The agent never changes accepted scope or the agreement itself on that basis, and does not reopen scope during implementation. Fail when such an agreement visibly blocks a clearly better outcome and the reply keeps it silently or omits who introduced it or the gain and cost, when the agent changes or widens scope without the human's decision, or when it reopens scope during implementation. `n/a` when no agreement blocks a clearly better outcome in that reply.

**N1 Noise limit.** A reply fails N1 when any of these holds:
- it answers a yes/no or status question with more than 180 words of prose (tables and link lists count by words);
- it repeats the same fact in two places within the reply;
- it contains two or more sentences of unrequested method or process detail that change no decision, state, or next step;
- it offers more than one menu of follow-up options.

## Scenario pass rule

A scenario run passes when no reply fails any item. `n/a` items do not count against it. A reply failure caused solely by a runtime permission denial that prevented the agent from reading state is recorded separately as a permission denial, not as a guidance failure; the run is then rerun.

## Pass bar (from the accepted definition)

- Every held-out scenario passes in two of two Sonnet (claude-sonnet-5-5) runs on the final candidate.
- The quick-status reply of the final candidate is no longer (in words) than the baseline quick-status reply. Word counts are measured mechanically with `wc -w` on the final reply text, separately from the scorer.

## Change log

- 2026-09-29T05:45Z — added item P12 (opportunity checkpoint), pre-registered under accepted definition revision 4 before any second-round run. No other text changed; the definition's per-rule pass bar against baseline replaces the all-or-nothing pass bar above for the second round, as recorded in the round-2 summary.
- 2026-10-09 — Prospectively, N1 permits one concise closing echo of important facts when the human explicitly requested it. This allowance excuses only that echo, not additional repetition, the word limit, unrequested process detail or multiple follow-up menus; P9 remains unchanged. Historical scores and completed comparisons retain their original checklist meaning.
