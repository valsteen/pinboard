---
name: pinboard-deliver
description: Deliver exactly one active pinboard attempt from its accepted brief, current definition identity, and renewable lease. Use when the item, checkout, definition, acceptance criteria, and verification are already recorded. Do not use for intake, portfolio selection, broad audits, design exploration, or acceptance review.
---

# Deliver from the pinboard

Deliver the accepted checkpoint of one active attempt: implement its complete scope, verify it, leave a durable result, and return it accurately for review.

Load this complete skill before acting on it. A native loader or `read_file` (following `ByteOffset=46080` above 46,080 bytes) suffices when it loads the whole body through the final section; otherwise read the resolved `SKILL.md` via `pinboard_startup_text` `file-start` with exact roots and `max_batch_bytes: "host-profile"` until null.

Do not initialize delivery for a request that only asks to read named context, get oriented, or wait. Read only that bounded context and leave lifecycle, authority, source planning, dispatch, and review untouched until the human requests revision or implementation.

Use [the shared runtime adapters](../pinboard/references/runtime-adapters.md) with your own runtime's reference, [Codex](../pinboard/references/runtime-codex.md), [Claude Code](../pinboard/references/runtime-claude-code.md) or [Antigravity](../pinboard/references/runtime-antigravity.md), for identity, isolation, launch, permissions, waiting and evidence-write recovery; this skill owns delivery semantics. Under the [public-contract boundary](../pinboard/references/runtime-adapters.md#public-contracts-and-source-scope), a missing input stops its operation and is reported precisely; installed Pinboard internals are no protocol fallback for another project.

A human invoking this skill to start a named item without a prepared active attempt follows the main skill's [human task-start route](../pinboard/SKILL.md#route-human-task-starts-through-pinboard); that is not an establishment failure.

## Establish the attempt

Call every Pinboard operation through its connected MCP tool with exact roots under its negotiated schema and the [current request shapes](../pinboard/references/runtime-adapters.md#current-mcp-request-shapes). Stop if a tool is missing. No shell fallback or undeclared [record files](../pinboard/references/runtime-adapters.md#verified-record-file-inputs). Only static contract discovery, full validation and view repair use the CLI, `<launcher-root>/scripts/pinboard`, two levels above this skill. Follow each result's retry disposition and fresh same-subject receipt, never replay after committed effects, and never inspect implementation source or guess a field.

1. Before acquisition or implementation, follow the launch envelope's exact verifier and read the immutable prompt it names; only those verified bytes are your task. Stop if verification or the read fails or differs; never select another file, reconstruct the prompt, or ask the parent to restate it. Read the complete prompt and canonical brief, including bootstrap, first. Require state authority `sqlite-v11`, never inferred from views. Acquire through the envelope's exact inputs with your own trusted post-launch identity and the verified host, then select the worker continuation with the returned lease and generation. Validate a supplied same-worker lease instead of replacing it.
2. Require the supplied attempt to be present and active through `pinboard_attempt_inspect`; other disjoint active attempts are not a user-facing condition. Stop if the state is invalid, the attempt is absent, its item and attempt records disagree, or another unexpired owner holds it, and follow the main skill's [contention-reporting rule](../pinboard/SKILL.md#match-detail-to-the-question) instead of guessing or silently revoking.
3. Read the canonical `.json` brief fully and require its accepted revision and digest to match `pinboard_item_definition` operation `current`; its Markdown view is not a contract. Then read only the project guidance, accepted definition and knowledge, and, for a local checkpoint, the source authorities it names. For a cross-boundary checkpoint, treat reviewed selectors and digests as exact receipts rather than preloading every body. Before editing each changed contract, read from the dispatched checkout its minimum concrete implementation set, even when the brief is complete: the decision owner, effect or persistence owner, direct callers and consumers, retained boundary conversions, and the cheapest real boundary evidence that could falsify agreement. Read the smallest additional exact source when implementation needs its detail, the brief is ambiguous or incomplete, a reviewed digest or correction changes an owner, a neighboring contract depends on it, or you must edit or trace it. Load several or large sources through a [source plan](../pinboard/references/runtime-adapters.md#brief-construction-and-source-preparation), reusing unchanged receipts; brief review still freezes semantics.
4. Inspect the checkout, branch/worktree, base revision, and unrelated user changes before editing.

Run any recorded worktree bootstrap before implementation-source changes. If the attempt changes setup, locks, package-manager contract, launcher, validator, or another tool needed to assess itself, keep the base setup receipt, coordinate only through stable installed tooling, and verify the changed candidate directly.

Confirm the attempt identity, definition revision and digest, checkpoint ID and execution environment without rewriting their semantics. The dispatched starting revision and permissions may narrow the work but grant no authority beyond the user and environment, and Pinboard does not enforce them. The canonical JSON brief alone orders execution, deferrals and verification; the matching definition owns item semantics. Ask only when missing information would change product behavior, architecture, scope, compatibility, or verification expectations.

When reacquiring an attempt returned from review, require the correction-dispatch prompt built from the exact return receipt and independently reviewed source, and read its return history, canonical reason and current `review.md` before editing. Keep the same brief, branch, evidence and attempt identity. The earlier `result.md` is history, not a readiness claim; refresh it only once the corrected candidate is stable and every required check passes. Do not replay the coordinator's [replacement-brief recovery](../pinboard/references/review-and-disposition.md#coordinate-review-responsibility-and-checkout-use).

## Keep the attempt lease live

The lease expires at its returned `expires_at`; nothing renews it for you, and a long turn, build, test run or helper wait can outlast it. Renew it with the dispatched TTL before any step that could outlast the remaining time, and whenever about half the TTL has passed during continuous work. A rejection for an expired or missing lease changed nothing: follow its owner check before reacquiring, never revoke another owner's live authority, and never reuse the old lease.

## Stay inside the attempt

- Edit only what the attempt requires.
- Keep one writer per checkout. Disjoint attempts may proceed concurrently in separate checkouts.
- Give a bounded contributor its exact owned paths, public interface, package identity and complete verification command, in a separate checkout; you integrate it and run the accepted checks on the combined candidate.
- Preserve unrelated user changes.
- Follow the repository's own testing, formatting, lint, documentation, and safety guidance.
- Treat a stale instruction as an instruction defect before reshaping working code around it.
- Apply [the test-seam distinction](../slop-cleanup/references/structural-consolidation.md#choose-explicit-test-seams): use ordinary permitted dependency substitution autonomously, even when only tests supply alternatives; surface materially intrusive workarounds and actual instruction/scope conflicts before choosing them, never hiding coupling or manufacturing preconditions; return an unresolved conflict through the scope route, stopping only its affected work.
- Do not edit generated views, SQLite authority, preparation or attempt leases, or another item's lifecycle state outside the executable workflow.
- Do not accept or complete your own item.

Worker diff inspection, requirement mapping, and fresh verification are pre-review evidence. They do not replace independent review.

As the candidate takes shape, classify every changed file and contract against the accepted scope and reviewed authorities, revalidating changed relationships and reading the smallest neighboring owner that an unclassified change, explicit uncertainty, repository check, or changed dependency exposes. Widen beyond that surface only for architecture, persistence, wire, lifecycle, dynamic-consumer, or demonstrated-blind-spot evidence. Record each source set, on-demand read, trigger, and outcome in `result.md`; a stale file prediction never authorizes omitting or forcing a read.

For a cross-boundary attempt, map every observable behavior and behavior-defining test to an authorized Contract row before adding it; its `Authorization basis` must resolve to the current accepted scope or an exact reviewed authority family. Ordinary internal choices that preserve supported behavior, such as helper names, local refactors and equivalent algorithms, need no provenance record.

Apply the main skill's [assurance boundary](../pinboard/SKILL.md#keep-assurance-inside-accepted-product-scope) before treating a discovered concern as required work: keep fixing defects and assurance supported by accepted scope, an exact brief contract or criterion, repository policy, an exact reviewed product-authority family, a named production consumer, or an observed supported-path failure, but not by a broad correctness claim, exploratory failing test, public narrative, or newly written guidance. Speculative risks stay non-blocking. When a finding would materially add an unsupported concurrency, durability, security, adversarial, platform, or compatibility guarantee, stop only that expansion and return the concrete human decision without weakening accepted work.

When cross-boundary implementation extends behavior governed by interacting conditions or operations, apply [Preserve independent decision gates](../repository-readiness/references/developer-navigation.md#preserve-independent-decision-gates) through the accepted contracts and evidence; not for an ordinary change with no conditional interaction.

When the checkpoint introduces or changes a command or closed-variant family across several production owners, or places dynamic dispatch on that path, read the [developer-navigation lens](../repository-readiness/references/developer-navigation.md) and implement the accepted trace and sibling-change shape. Another same-meaning routing site, hidden fallback or dynamic wiring owner absent from the brief is a brief omission, not something to silently spread or remove.

Apply the [engineering-health baseline](../pinboard/references/engineering-health-baseline.md) only when the canonical brief explicitly selects it, and only through its authorized contracts and verification; never infer the selection from complexity, architecture impact or file count.

Run every basis-bearing entry in the accepted `Verification` section as a mandatory check, and add no tool, threshold, platform promise, compatibility obligation, or hardening check to it without an accepted basis; exploratory checks do not become obligations by being run or suggested in review.

When mandatory verification fails, apply the main skill's [repair responsibility](../pinboard/SKILL.md#keep-assurance-inside-accepted-product-scope) before escalating. Make the cheapest observation that separates a tool or agent-invocation defect from a missing prerequisite, locked-install failure, permission or network boundary, or tool-under-change condition. Repair an in-scope project-owned defect or unsuitable invocation under existing authority and rerun the exact check. Report Pinboard or third-party blockers with their owner's resolution; developing a dependency needs an explicit assignment. If the capability stays unavailable or degraded, stop the affected work and return the exact blocker to the outcome owner; never guess a substitute, keep retrying, or omit the check. The human decides missing authority or revised scope; runtime permission recovery stays with the runtime adapter.

When a useful addition has no authorized row, stop widening that part, report it and continue independent in-scope work. Propose intake only when the user explicitly authorizes preservation, which authorizes no implementation; never invent a ledger state or transition for it.

Treat a discovered material architectural limitation as an upstream scope condition, compared with the project's limitations owner and the exact definition and brief. If the change would introduce, widen, preserve, mask, or deepen an unacknowledged material limitation, stop only the affected implementation and return it to the owning task as a brief omission or unresolved product decision. Never make it acceptable by documenting it yourself, read generic authority as acknowledgement, or absorb adjacent work; continue disjoint accepted work. A bounded fix leaving the foundation unchanged, or a speculative risk without supported-path evidence, does not trigger this.

Useful but unrequired work goes through `$pinboard-intake` only when the user explicitly wants it preserved; otherwise mention it in the result. Nested intake keeps the main skill's [continuation anchor](../pinboard/SKILL.md#safe-boundaries), the attempt ID and next promised action, and returns to it after persistence; intake alone does not pause or reprioritize the attempt.

### Pause for human decisions

Finish useful independent accepted work first. Preserve current work and verification in `blocker.md`, discover your own-leased pause action and its input contract, and submit the complete [decision batch](../pinboard/SKILL.md#record-and-resolve-decision-batches), or a plain-text pause when a batch adds no structure. Confirm the committed pause and pending batch through fresh attempt inspection, release your authority, and return the complete choices to the owning task. A paused return preserves incomplete work; it is not delivery or review submission.

On continuation, read the recorded answers and explicit deferrals before editing. An answer that changes accepted semantics requires the owning task's definition/brief replacement and paused rebind before resume; a direct message cannot replace that route. Resume requires one human answer or explicit deferral with a reopening condition for every pending ID.

If a discovered problem blocks the attempt: stop widening the implementation; preserve the commit/worktree and verification; write `blocker.md` in the attempt directory with the observation, affected criterion, completed work and safest next action; return it to the owning task with a purpose label and native clickable link, applying the main skill's readable-artifact rule; propose a prerequisite through `$pinboard-intake` only when explicitly requested; report it through the advisory `report-blocker:<attempt>` action; and leave lifecycle mutation to the owning task's [prerequisite route](../pinboard/references/setup-and-transitions.md).

## Implement and verify

Use the repository's selected testing mode. Prefer the smallest evidence that can disprove the important failure, then run the broader changed-surface gate the attempt requires.

Use `$technical-writing` in quiet editorial mode when the attempt creates or substantially revises a human-facing document, report, readable artifact, or pull-request description: accepted facts and the exact candidate keep authority over meaning, so improve orientation and prose without interviewing the human, inventing claims, or changing machine-consumed semantics. Compose collaboratively only when the accepted work includes shaping the document with the user.

Continue through the complete accepted checkpoint while in-scope work can proceed without user input; a green seam, milestone or long turn alone warrants no commentary under the main skill's [human's picture](../pinboard/SKILL.md#keep-the-humans-picture-current). Successful delivery ends only after the stable candidate and truthful `result.md` are ready, own-leased submission is confirmed and your authority is released. A `blocker.md` naming what prevents further work, an explicit user request for a partial stop or background execution, or a host-forced early return preserves the exact incomplete continuation and authority disposition without claiming success. State whether any user action is required.

Immediately before writing or refreshing `result.md`, account for every user direction and repository change after the brief or candidate cut point. If one alters accepted semantics, do not present or submit the stale candidate: return control so the owning task replaces the definition and brief, clears stale review identity and obtains a new exact-candidate review. Work implementing the unchanged brief proceeds normally. At that boundary, also compare the final diff with every accepted material-limitation decision and the architecture owner: the candidate must not hide the foundational condition behind a compensating layer or change its consequence or reopening condition without updated scope.

Before review, prepare a stable candidate: a working-tree identity observed through `pinboard_candidate_observe`, never a recreated checksum or a draft report, or the full current `HEAD` commit with a clean tree. Use a clean committed candidate when the disposition publishes repository changes; a working-tree candidate stays eligible for local-only checkpoint or terminal acceptance and is never later relabelled as a commit. Of the omitted untracked paths, intended new files join only through separately authorized exact Git preparation and unrelated files stay untouched, declared through the submit-review leaf when they would block clean qualification or recovery. Reobserve after preparation or any source change, then submit the observed candidate.

When acceptance requires CI on an exact pushed head, keep one unchanged commit: after the local checks, commit with a clean tree, record its full identity in `result.md`, submit it and release your authority; the outcome owner reviews, publishes, checks CI and accepts that same commit. Commit creation, publication and acceptance follow the [repository-disposition authority](../pinboard/references/review-and-disposition.md#review-and-completion): reuse granted authority and ask only for missing authority before its effect. Exact-head CI alone authorizes no push.

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

The outcome owner commissions one fresh candidate-read-only reviewer through `pinboard_review_job` under the [current-responsibility review route](../pinboard/references/review-and-disposition.md#coordinate-review-responsibility-and-checkout-use); never review your own candidate. Send no task-to-task completion or review message for subordinate work; a genuinely independent attempt reports and requests decisions in its own task's conversation. If subagent creation is unavailable, leave the exact candidate in review and report the missing runtime capability; never create or wake a user-owned task, hand routine ownership to a parent task, or substitute the implementer as reviewer.

Do not claim canonical completion until the owning task applies the completion transition after review.

Report a corrected candidate normally, without presenting the return as a new concern or implying the earlier review was accepted; name the current runtime, for example: `Correction ready — <candidate>; the same attempt has been resubmitted for review by a separate Claude Code reviewer.`
