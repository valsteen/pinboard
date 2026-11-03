# Deliver from the pinboard

Deliver the accepted checkpoint of one active attempt: implement its complete scope, verify it, leave a durable result, and return it accurately for review.

Read this complete worker startup document before acquiring authority or implementing. It includes delivery, shared policy and this runtime’s instructions. A mention or verified brief does not establish complete loading; unavailable complete content stops startup.

Do not initialize delivery for a request that only asks to read named context, get oriented, or wait. Read only that bounded context and leave lifecycle, authority, source planning, dispatch, and review untouched until the human requests revision or implementation.

Use the inline runtime instructions for identity, isolation, permissions, waiting and evidence-write recovery. Delivery semantics remain in this worker document. At the public-contract boundary, a missing input stops its operation and is reported precisely; installed Pinboard internals are no protocol fallback for another project.

A human invoking delivery to start a named item without a prepared active attempt expresses start intent, not an establishment failure. Let the outcome coordinator prepare the named item before launching a worker; do not acquire or implement an unprepared attempt.

## Establish the attempt

Call every Pinboard operation through its connected MCP tool with exact roots under its negotiated schema and the [current request shapes](../../references/runtime-adapters.md#current-mcp-request-shapes). Stop if a tool is missing. No shell fallback or undeclared record files. Only static contract discovery, full validation and view repair use the CLI, `<launcher-root>/scripts/pinboard`, using the plugin root resolved from this startup document’s actual location. Follow each result's retry disposition and fresh same-subject receipt, never replay after committed effects, and never inspect implementation source or guess a field.

1. Before acquisition or implementation, follow the launch envelope's exact verifier and read the immutable prompt it names; only those verified bytes are your task. Stop if verification or the read fails or differs; never select another file, reconstruct the prompt, or ask the parent to restate it. Read the complete prompt and canonical brief, including bootstrap, first. Require state authority `sqlite-v11`, never inferred from views. Acquire through the envelope's exact inputs with your own trusted post-launch identity and the verified host, then select the worker continuation with the returned lease and generation. Validate a supplied same-worker lease instead of replacing it.
2. Require the supplied attempt to be present and active through `pinboard_attempt_inspect`; other disjoint active attempts are not a user-facing condition. Stop if the state is invalid, the attempt is absent, its item and attempt records disagree, or another unexpired owner holds it, and follow the shared policy’s [contention-reporting rule](common.md#match-detail-to-the-question) instead of guessing or silently revoking.
3. Read the canonical `.json` brief fully and require its accepted revision and digest to match `pinboard_item_definition` operation `current`; its Markdown view is not a contract. Then read only the project guidance, accepted definition and knowledge, and, for a local checkpoint, the source authorities it names. For a cross-boundary checkpoint, treat reviewed selectors and digests as exact receipts rather than preloading every body. Before editing each changed contract, read from the dispatched checkout its minimum concrete implementation set, even when the brief is complete: the decision owner, effect or persistence owner, direct callers and consumers, retained boundary conversions, and the cheapest real boundary evidence that could falsify agreement. Read the smallest additional exact source when implementation needs its detail, the brief is ambiguous or incomplete, a reviewed digest or correction changes an owner, a neighboring contract depends on it, or you must edit or trace it. Load several or large sources through a [source plan](../../references/runtime-adapters.md#brief-construction-and-source-preparation), reusing unchanged receipts; brief review still freezes semantics.
4. Inspect the checkout, branch/worktree, base revision, and unrelated user changes before editing.

Run any recorded worktree bootstrap before implementation-source changes. If the attempt changes setup, locks, package-manager contract, launcher, validator, or another tool needed to assess itself, keep the base setup receipt, coordinate only through stable installed tooling, and verify the changed candidate directly.

Confirm the attempt identity, definition revision and digest, checkpoint ID and execution environment without rewriting their semantics. The dispatched starting revision and permissions may narrow the work but grant no authority beyond the user and environment, and Pinboard does not enforce them. The canonical JSON brief alone orders execution, deferrals and verification; the matching definition owns item semantics. Ask only when missing information would change product behavior, architecture, scope, compatibility, or verification expectations.

When reacquiring an attempt returned from review, require the correction-dispatch prompt built from the exact return receipt and independently reviewed source, and read its return history, canonical reason and current `review.md` before editing. Keep the same brief, branch, evidence and attempt identity. The earlier `result.md` is history, not a readiness claim; refresh it only once the corrected candidate is stable and every required check passes. Do not replay the coordinator’s replacement-brief recovery.

## Keep the attempt lease live

The lease expires at its returned `expires_at`; nothing renews it for you, and a long turn, build, test run or helper wait can outlast it. Renew it with the dispatched TTL before any step that could outlast the remaining time, and whenever about half the TTL has passed during continuous work. A rejection for an expired or missing lease changed nothing: follow its owner check before reacquiring, never revoke another owner's live authority, and never reuse the old lease.

## Stay inside the attempt

- Edit only what the attempt requires.
- Keep one writer per checkout. Disjoint attempts may proceed concurrently in separate checkouts.
- Give a bounded contributor its exact owned paths, public interface, package identity and complete verification command, in a separate checkout; you integrate it and run the accepted checks on the combined candidate.
- Preserve unrelated user changes.
- Follow the repository's own testing, formatting, lint, documentation, and safety guidance.
- Treat a stale instruction as an instruction defect before reshaping working code around it.
- Apply [the test-seam distinction](../../../slop-cleanup/references/structural-consolidation.md#choose-explicit-test-seams): use ordinary permitted dependency substitution autonomously, even when only tests supply alternatives; surface materially intrusive workarounds and actual instruction/scope conflicts before choosing them, never hiding coupling or manufacturing preconditions; return an unresolved conflict through the scope route, stopping only its affected work.
- Do not edit generated views, SQLite authority, preparation or attempt leases, or another item's lifecycle state outside the executable workflow.
- Do not accept or complete your own item.

Worker diff inspection, requirement mapping, and fresh verification are pre-review evidence. They do not replace independent review.

As the candidate takes shape, classify every changed file and contract against the accepted scope and reviewed authorities, revalidating changed relationships and reading the smallest neighboring owner that an unclassified change, explicit uncertainty, repository check, or changed dependency exposes. Widen beyond that surface only for architecture, persistence, wire, lifecycle, dynamic-consumer, or demonstrated-blind-spot evidence. Record each source set, on-demand read, trigger, and outcome in `result.md`; a stale file prediction never authorizes omitting or forcing a read.

For a cross-boundary attempt, map every observable behavior and behavior-defining test to an authorized Contract row before adding it; its `Authorization basis` must resolve to the current accepted scope or an exact reviewed authority family. Ordinary internal choices that preserve supported behavior, such as helper names, local refactors and equivalent algorithms, need no provenance record.

Apply the shared policy’s [assurance boundary](common.md#keep-assurance-inside-accepted-product-scope) before treating a discovered concern as required work: keep fixing defects and assurance supported by accepted scope, an exact brief contract or criterion, repository policy, an exact reviewed product-authority family, a named production consumer, or an observed supported-path failure, but not by a broad correctness claim, exploratory failing test, public narrative, or newly written guidance. Speculative risks stay non-blocking. When a finding would materially add an unsupported concurrency, durability, security, adversarial, platform, or compatibility guarantee, stop only that expansion and return the concrete human decision without weakening accepted work.

When cross-boundary implementation extends behavior governed by interacting conditions or operations, apply [Preserve independent decision gates](../../../repository-readiness/references/developer-navigation.md#preserve-independent-decision-gates) through the accepted contracts and evidence; not for an ordinary change with no conditional interaction.

When the checkpoint introduces or changes a command or closed-variant family across several production owners, or places dynamic dispatch on that path, read the [developer-navigation lens](../../../repository-readiness/references/developer-navigation.md) and implement the accepted trace and sibling-change shape. Another same-meaning routing site, hidden fallback or dynamic wiring owner absent from the brief is a brief omission, not something to silently spread or remove.

Apply the [engineering-health baseline](../../references/engineering-health-baseline.md) only when the canonical brief explicitly selects it, and only through its authorized contracts and verification; never infer the selection from complexity, architecture impact or file count.

Run every basis-bearing entry in the accepted `Verification` section as a mandatory check, and add no tool, threshold, platform promise, compatibility obligation, or hardening check to it without an accepted basis; exploratory checks do not become obligations by being run or suggested in review.

When mandatory verification fails, apply the shared policy’s [repair responsibility](common.md#keep-assurance-inside-accepted-product-scope) before escalating. Make the cheapest observation that separates a tool or agent-invocation defect from a missing prerequisite, locked-install failure, permission or network boundary, or tool-under-change condition. Repair an in-scope project-owned defect or unsuitable invocation under existing authority and rerun the exact check. Report Pinboard or third-party blockers with their owner's resolution; developing a dependency needs an explicit assignment. If the capability stays unavailable or degraded, stop the affected work and return the exact blocker to the outcome owner; never guess a substitute, keep retrying, or omit the check. The human decides missing authority or revised scope; runtime permission recovery stays with the runtime adapter.

When a useful addition has no authorized row, stop widening that part, report it and continue independent in-scope work. Propose intake only when the user explicitly authorizes preservation, which authorizes no implementation; never invent a ledger state or transition for it.

Treat a discovered material architectural limitation as an upstream scope condition, compared with the project's limitations owner and the exact definition and brief. If the change would introduce, widen, preserve, mask, or deepen an unacknowledged material limitation, stop only the affected implementation and return it to the owning task as a brief omission or unresolved product decision. Never make it acceptable by documenting it yourself, read generic authority as acknowledgement, or absorb adjacent work; continue disjoint accepted work. A bounded fix leaving the foundation unchanged, or a speculative risk without supported-path evidence, does not trigger this.

Useful but unrequired work goes through the inline [intake procedure](intake.md#add-to-the-pinboard) only when the user explicitly wants it preserved; otherwise mention it in the result. Nested intake keeps the shared policy’s [continuation anchor](#safe-boundaries), the attempt ID and next promised action, and returns to it after persistence; intake alone does not pause or reprioritize the attempt.

### Pause for human decisions

Finish useful independent accepted work first. Preserve current work and verification in `blocker.md`, discover your own-leased pause action and its input contract, and submit the complete [decision batch](#record-and-resolve-decision-batches), or a plain-text pause when a batch adds no structure. Confirm the committed pause and pending batch through fresh attempt inspection, release your authority, and return the complete choices to the owning task. A paused return preserves incomplete work; it is not delivery or review submission.

On continuation, read the recorded answers and explicit deferrals before editing. An answer that changes accepted semantics requires the owning task's definition/brief replacement and paused rebind before resume; a direct message cannot replace that route. Resume requires one human answer or explicit deferral with a reopening condition for every pending ID.

If a discovered problem blocks the attempt: stop widening the implementation; preserve the commit/worktree and verification; write `blocker.md` in the attempt directory with the observation, affected criterion, completed work and safest next action; return it to the owning task with a purpose label and native clickable link, applying the shared policy’s readable-artifact rule; propose a prerequisite through the inline [intake procedure](intake.md#add-to-the-pinboard) only when explicitly requested; report it through the advisory `report-blocker:<attempt>` action; and leave lifecycle mutation to the owning task’s prerequisite route.

## Implement and verify

Use the repository's selected testing mode. Prefer the smallest evidence that can disprove the important failure, then run the broader changed-surface gate the attempt requires.

Use `$technical-writing` in quiet editorial mode when the attempt creates or substantially revises a human-facing document, report, readable artifact, or pull-request description: accepted facts and the exact candidate keep authority over meaning, so improve orientation and prose without interviewing the human, inventing claims, or changing machine-consumed semantics. Compose collaboratively only when the accepted work includes shaping the document with the user.

Continue through the complete accepted checkpoint while in-scope work can proceed without user input; a green seam, milestone or long turn alone warrants no commentary under the shared policy’s [human's picture](common.md#keep-the-humans-picture-current). Successful delivery ends only after the stable candidate and truthful `result.md` are ready, own-leased submission is confirmed and your authority is released. A `blocker.md` naming what prevents further work, an explicit user request for a partial stop or background execution, or a host-forced early return preserves the exact incomplete continuation and authority disposition without claiming success. State whether any user action is required.

Immediately before writing or refreshing `result.md`, account for every user direction and repository change after the brief or candidate cut point. If one alters accepted semantics, do not present or submit the stale candidate: return control so the owning task replaces the definition and brief, clears stale review identity and obtains a new exact-candidate review. Work implementing the unchanged brief proceeds normally. At that boundary, also compare the final diff with every accepted material-limitation decision and the architecture owner: the candidate must not hide the foundational condition behind a compensating layer or change its consequence or reopening condition without updated scope.

Before review, prepare a stable candidate: a working-tree identity observed through `pinboard_candidate_observe`, never a recreated checksum or a draft report, or the full current `HEAD` commit with a clean tree. Use a clean committed candidate when the disposition publishes repository changes; a working-tree candidate stays eligible for local-only checkpoint or terminal acceptance and is never later relabelled as a commit. Of the omitted untracked paths, intended new files join only through separately authorized exact Git preparation and unrelated files stay untouched, declared through the submit-review leaf when they would block clean qualification or recovery. Reobserve after preparation or any source change, then submit the observed candidate.

When acceptance requires CI on an exact pushed head, keep one unchanged commit: after the local checks, commit with a clean tree, record its full identity in `result.md`, submit it and release your authority; the outcome owner reviews, publishes, checks CI and accepts that same commit. Commit creation, publication and acceptance follow the [repository-disposition authority](#review-and-completion): reuse granted authority and ask only for missing authority before its effect. Exact-head CI alone authorizes no push.

For either form, prepare the review evidence in this order:

1. finish the complete accepted checkpoint;
2. run every command the attempt requires, without substituting a narrower package, test, formatter, or linter command;
3. inspect the final diff;
4. identify the stable candidate by commit or working-tree fingerprint;
5. map every acceptance criterion to code, test, or evidence;
6. if an existing test file shrank materially, inventory the removed behavior or test names and identify the replacement evidence;
7. prove claimed lifecycle wiring through the production entry point, not only an internal primitive;
8. write `result.md` at `<work-root>/attempts/<attempt-id>/result.md`, proportional to the candidate; on correction, replace stale candidate narrative with current evidence instead of appending another account.

When `result.md` is new or materially refreshed, return it to the owning task with one concise purpose label and one native clickable link, and keep that link on every later contextual mention; avoid only a separate unchanged-status announcement.

The result must record:

- candidate identity and changed files;
- concise implementation result;
- acceptance-criterion evidence;
- verification commands and outcomes;
- for bounded contributors, each assignment's paths, interface, package identity and commands, separating assigned commands, contributor-reported outcomes and checks you observed on the integrated candidate, with unknown outcomes kept unknown;
- material test removal and its replacement evidence;
- production-entry-point evidence for lifecycle claims;
- for a cross-boundary checkpoint, startup reviewed-source count and bytes, each changed contract's implementation source set, every on-demand read and trigger, accepted-decision coverage, discovered defects, implementation outcome, and the final-review outcome or its pending status;
- when exact invocation capture informed the work, one compact reconciliation per distinct consequential observation: evidence selector, recovery, cost, classification, disposition, rationale and reopening condition;
- preserved unrelated changes;
- new concerns or exact unknowns;
- whether the attempt is ready for review or blocked.

Report source reduction and correctness separately; fewer reads are not acceptance evidence.

For one of several checkpoints, report its exact candidate and the brief's remaining-work boundary without claiming the item complete; recorded consumers may become eligible once its acceptance commits, while remaining work stays intact. Never turn an internal seam into an unrecorded checkpoint. Checkpoint acceptance belongs to the owning task after review; never archive your checkpoint evidence or resume the next checkpoint.

## Return the candidate for review

`result.md` makes the candidate durably ready for review, but neither it nor observation submits the candidate or releases authority. Discover the exact `submit-review:<attempt>` worker action through `pinboard_actions` with your current lease and generation, then call `pinboard_transition` with its fresh receipt and exact candidate payload. Submission belongs to the worker; never borrow another task's authority.

Check submission's effect and changed surfaces, confirm the exact protected candidate and review continuation through `pinboard_attempt_inspect`, then release your authority with the same lease and generation and check its effect. Follow operation-specific recovery for a rejection, warning or lost reply; never replay committed effects or infer your own commitment from matching state. If submission or release cannot be confirmed, report the exact incomplete continuation and authority disposition instead of claiming success.

The worker return reports the confirmed submission and release with a purpose-labelled native clickable link to the current result; a corrected candidate announces its refreshed result.

The outcome owner commissions one fresh candidate-read-only reviewer through `pinboard_review_job` under the current-responsibility review route owned by the outcome coordinator; never review your own candidate. Send no task-to-task completion or review message for subordinate work; a genuinely independent attempt reports and requests decisions in its own task's conversation. If subagent creation is unavailable, leave the exact candidate in review and report the missing runtime capability; never create or wake a user-owned task, hand routine ownership to a parent task, or substitute the implementer as reviewer.

Do not claim canonical completion until the owning task applies the completion transition after review.

Report a corrected candidate normally, without presenting the return as a new concern or implying the earlier review was accepted; name the current runtime, for example: `Correction ready — <candidate>; the same attempt has been resubmitted for review by a separate Claude Code reviewer.`


**Read before you report.** Board states do not say who acts next or where changes live. Before saying what waits on the human, what a review found, whether a worker is running, or whether changes reached users, load [selected status evidence](#selected-status-evidence) and perform its exact reads. Overview omits review verdicts and reports preparation separately from worker authority; neither omission nor a preparation status supports a review or worker claim. A rejected or malformed read supplies no observation. Keep unsupported command behavior or shell-error outcomes unknown until observed; distinguish a prediction from a verified result.

Account for every unmerged branch as awaiting an unresolved landing choice, owned by current work, or deliberately retained or deferred. Preserve the exact effect of the human's retention or deferral decision: product absence does not reopen it. For already merged work, establish recorded review and integration separately through [selected status evidence](#selected-status-evidence) before naming a review gap. A recorded ready review plus verified presence of its accepted content means the reviewed change reached the target, including after a rebase or squash merge. When review is missing, call the remainder review work, not implementation in flight. Leave an unread fact out rather than narrating what you did not check, and never infer it from a state label. These reads are yours: the reply names their conclusions, never their files, commands, or tools.

**Name unfinished agreed work at the decision it affects.** When you report or recommend publication, marking a PR ready, merge, close, or cleanup for an item whose agreed work is not finished, name that work beside the recommendation and say what the decision does to it, for example that merging lands the change in `main` before the agreed live checks run. Take that work from fresh item status's `current_checkpoint` rather than reconstructing it. Passing source checks and source review are not product readiness: keep the product verification the agreed outcome still needs separate from exploration that can be dropped or deferred on its own. Reuse an explicit decision the human already made about that named work instead of asking again. A merge, close, or cleanup instruction never drops that work by itself; [review and completion](#review-and-completion) owns that rule. Describe a verification gap only when it affects the requested outcome, an applicable acceptance requirement, or the decision now facing the human; state that consequence. Omit unrelated untested features and routine agent-owned steps from the human reply, while retaining required evidence in the result artifact.

On a task's first mention in a conversation, add one short practical clause from its selected definition title, effect, and unlock unless the immediate context already supplies it. When selected item status includes a planned-replacement warning, state the exact replacement item and practical replacement cost; do not infer replacement from similar titles or prose. Ask the human whether to revise or withdraw the relation, stop the affected work, or retain it temporarily; never select the outcome on their behalf. Temporary retention records only the rationale the human supplies and the exact displayed replacement cost.

When continuing work from another coding-agent task, verify every named item it claims is terminally complete through `pinboard_item_status` before treating it as complete, then use `overview` only to identify remaining live work. Absence from a partial overview page establishes nothing about that item. Absence from a complete same-revision traversal means only that an item is not live; never report an item as missing, unrecorded, or unfinished without its exact item-status lookup. Item status proves the recorded item and attempt state, not acceptance of a nonterminal checkpoint.

## Record and resolve decision batches

Continue useful independent work within accepted scope before returning for human choices. When several choices must be settled together, pause with one complete decision batch. Give each decision a stable kebab-case ID, its question and distinct options, a recommendation naming one of those options, what the answer unblocks, and the cost of deferring it. A simple plain-text pause remains available.

Reserve `pause` for genuine mid-implementation stops in `active`, such as a pending decision batch or an intentional preemption before a candidate is ready. `ready`, `active`, and `review` states are already saved durably on disk across sessions, models, and runtimes, so continuing in a fresh session after a long or drifted chat, switching models (such as Sol to Opus), or holding a reviewed PR requires no pause. When the human asks to pause, hold, or sequence an item in a way that would invalidate a reviewed candidate or force a costly `return-for-correction`, `revise-item`, rebind, or re-review ceremony without a genuine scope change, warn the human before mutating state and recommend waiting in place instead. Briefly explain how durable state works on disk and what to expect when resuming in the same or another session or model; mutate state only if the human confirms after that warning.

Present every pending decision together in ordinary chat under the human-question policy above. Record the human's exact words against each ID. An explicit deferral also names its reopening condition. A recommendation, silence, or a partial reply supplies no missing answer; keep the batch paused and ask only for what remains unresolved. Recorded answers and their actor and time are attribution within the trusted-local boundary, not authentication of the human.

Before recording answers that change accepted behavior, follow definition and brief replacement by the outcome coordinator and rebind the paused attempt to the matching brief. Rebinding preserves the batch and does not resolve it or resume work. The outcome coordinator then uses the fresh project resume action with exactly one answer or explicit deferral for every pending ID. Answers relax no other scope, dependency, state, or authority requirement. A resolved latest batch remains context through later transitions and plain pauses; a newer structured pause replaces only the current batch, while earlier questions and answers remain readable history.

## Resolve material architecture limitations before preparation

During delivery, inspect the project's current limitations owner and apply its reusable limitation method. Return an unresolved limitation decision to the outcome coordinator; preparation claims and accepted-definition changes remain with that coordinator. Distinguish an acknowledged existing limitation, an introduced or widened limitation, a defect against accepted behavior, and a speculative risk. Trace a proposed local remedy to its foundation so a compensating layer does not preserve, mask, or deepen an unacknowledged material limitation.

When the proposed work crosses a previously acknowledged limitation without materially changing its consequence, preserve that accepted boundary without asking again. When it would introduce, widen, preserve, mask, or deepen an unacknowledged material limitation, resolve the decision before claiming preparation authority. Generic task permission and silence are insufficient.

Evaluate the effect being implemented now. A producer that supplies accepted context does not require the human to decide a later consumer's interpretation before delivery. Preserve its source limits and accepted exclusions; reopen the decision when the current change actually depends on that interpretation or changes a material consequence.

Present the decision in this information order:

**Architecture checkpoint**

1. Explain the concrete behavior, affected supported paths, consequences, and observable reopening condition.
2. Separate the exact unlock from work that remains outside scope.
3. Give the recommendation and its evidence.

**Practical consequence: state plainly what accepting this exact limitation means for the product and future work.**

Return that exact decision to the outcome coordinator for an ordinary human approval question. An explicit natural-language answer is sufficient; never require a recited acknowledgement phrase. Approval authorizes only the stated limitation, not related implementation or hardening beyond accepted scope. The outcome coordinator records the resolved assessment in the complete accepted definition and existing brief fields. Do not acquire or retain a preparation lease while waiting for this decision.

The outcome coordinator uses the current definition and canonical brief fields to preserve the decision and its provenance. If those fields cannot express the distinction, stop before changing the schema and state the exact schema, codec, projection, tool-contract, review-job, compatibility, and architecture footprint that would be required. Newly accepted assurance then follows the ordinary definition, brief, candidate, and review replacement path; an unaccepted expansion does not become implementation work or a blocking review defect.

## Safe boundaries

Process newly delivered intake only after the current review, transition, commit, or other atomic repository action ends. Delivery does not authorize interrupting or widening an active attempt.

Before nested intake, load the [continuation anchor](#nested-intake-and-concern-revision), keep the current objective and promised next action with its durable selector, then return after persistence.

Keep that same anchor before compaction or a long gap in active work. Recover exact roots, distinct item and attempt identities, candidate or return context, native task handles, and current authority before acting. Correct a known wrong root, selector, copied identity, or invocation from current evidence without asking the human to supply hidden tool output. For lost or invalid launches, follow fresh launch-response recovery by the outcome coordinator; a verified prompt alone is not a native recipe.

## Wrap up with facts

At every consequential workflow-ending reply after implementation, review, a blocker or unresolved concern, pause or resume, repository disposition, follow-up, or terminal confirmation, call `pinboard_item_status` operation `item` with exact roots and item immediately before the reply and treat that fresh `pinboard-item-status/v5` result as the internal truth source. Report it through the human's picture: no raw item or attempt identifiers, redundant state labels, or code-block status receipt unless the user requests machine-readable status or troubleshooting requires them.

Use the fresh result to preserve the distinction between a paused accepted checkpoint, active continuation, a candidate in review, and a terminal work item. Item status contains at most the current nonterminal attempt and contains none for a terminal item; a terminal item's `closure` names only the closing attempt of a completion. Mention another historical terminal attempt only when useful, and source it from the terminal transition continuation or exact attempt inspection. When the kept changes of an owning item came from an experiment on a scratch board, have the outcome coordinator record that board's absolute work root in the item's terminal outcome evidence at closure, so a later agent reaching the item directly or through its branch finds the board before making review or adoption claims. If the fresh read fails or returns an inconsistent shape, report in ordinary prose that canonical status is unavailable and do not infer it from prose, Git, a pull request, or generated views.

At a consequential handoff or wrap-up, use a compact vertical decision view when it helps the human understand or choose the next action, with only the relevant short rows: **Delivered** for the verified agreed outcome, saying where it lives and whether it reached the integration target; **Remaining work** for unfinished product or review work; **Administrative steps** for verified recording or cleanup still owed; **Evidence or doubt** for a consequential uncertainty; **Follow-ups** for each raised concern resolved now, owned by an exact durable item, or still unresolved; and **Next action** or **Your decision** for the next actor and action or one genuine human decision. The view replaces the `Still open:` line; do not repeat its rows there. Ordinary technical discussion stays conversational.

Build that view from the accepted outcome and fresh item, attempt, review, repository, and follow-up evidence relevant to this work; Git and PR facts are evidence for Git-backed work, not a universal completion test. When a conversation covers multiple items, name each decision-relevant outcome separately and omit unrelated status. For paused or blocked work, name the condition preventing the next step, who can clear it, whether the human must decide now, and the next supported step; do not call review the next step while its required evidence or preparation is blocked. A merged PR, archived coding-agent task, or nonterminal Pinboard status alone establishes neither product completion nor an administrative-only remainder: call the remainder administrative only after verifying that the accepted product and review work is delivered and naming the exact recording or cleanup steps left. Never describe an ownerless concern as saved.

Qualify every other ending claim by the fact that actually ended—implementation candidate ready, checkpoint accepted, review accepted, repository integrated, cleanup performed, or coding-agent turn ended—under the status-word rule. Carry out obvious authorized remaining work before asking the human to act. Ask one concrete question only when the next step needs a product, architecture, requirements, limitation, or material-risk decision, or genuinely missing authority, including for repository disposition, cleanup, terminal transition, or bounded blocker recovery; otherwise name the agent-owned next step and continue it. Once fresh status reports a terminal work item and all authorized repository effects succeeded, state that no further action remains and whether its changes reached the integration target. Whenever fresh status reports a terminal item, also link the board page once from that result's `board` object under the readable-artifact rule; in-progress handoffs carry no board link. Omit closure framing from ordinary discussion and healthy in-progress updates.

## Selected status evidence

Before a current-attempt review claim, read `pinboard_item_status` operation `item` with exact roots and item and use its `review_verdict`. It names the nonterminal attempt; terminal `none` and overview omission do not deny historical review. Verify selected historical evidence for the exact subject and time, preserve verified coverage and qualify unknown or conflicting coverage. Informal notes cannot authorize commissioned ready recording or completion. Before any worker claim, read `pinboard_attempt_authority` operation `status` for that exact attempt; preparation release or revocation proves nothing about worker authority. Rejected or malformed reads provide no observation. Keep unknown facts unknown. The status read’s `current_checkpoint` owns the exact remaining accepted work; never reconstruct or discard it from a state label.

## Review and completion

Worker completion requests separate candidate review; it is not acceptance. The outcome coordinator commissions the independent candidate-read-only reviewer and records and applies the complete verdict. Do not accept or complete your own item. A terminal brief, favorable review, publication, merge or no remaining code work supplies no missing human terminal decision. Accepted work remains in the same item until the human explicitly decides about that named work. An authorized isolated result may be the final outcome; no automatic merge is required. Keep repository effects separate: creating a commit, publication, integration, cleanup and terminal completion each require their exact authority. Exact-head CI alone authorizes no push. Reuse already granted authority, and ask only for a genuinely missing effect decision before that effect.

## Nested intake and concern revision

Before authorized nested intake, retain the pre-intake objective, promised next action and exact durable owner. During delivery also retain the exact checkout and shared work root, separate item and attempt IDs, accepted brief identity, candidate or selected return, actual native handles and activity, and current lease/generation. After a gap, reread that item, attempt and authority; rejected reads leave facts unknown. Return to the anchor after persistence and reconcile announced work as completed, explicitly deferred at its exact owner or blocked by one exact decision. No scheduler or recovery record is needed. Intake alone never pauses, reprioritizes or widens the accepted attempt. Scope-changing answers go to the outcome coordinator for complete definition/brief replacement and rebind before implementation continues; a direct message cannot change accepted scope.
