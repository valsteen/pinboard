---
name: pinboard-deliver
description: Deliver exactly one active pinboard attempt from its accepted brief, current definition identity, and renewable lease. Use when the item, checkout, definition, acceptance criteria, and verification are already recorded. Do not use for intake, portfolio selection, broad audits, design exploration, or acceptance review.
---

# Deliver from the pinboard

Deliver the accepted checkpoint of one active attempt: implement its complete scope, verify it, leave a durable result, and return it accurately for review.

Do not initialize delivery for a request that only asks to read named context, get oriented, or wait. Read only that bounded context and leave lifecycle, authority, source planning, dispatch, and review untouched until the human requests revision or implementation.

Use [the shared runtime adapters](../pinboard/references/runtime-adapters.md) for identity, checkout isolation, worker and reviewer launch, permission declarations, and waiting. This skill continues to own delivery semantics.

Direct human invocation to start a named Pinboard item is not an attempt-establishment failure. When no already prepared active attempt was supplied, follow the main skill's [human task-start route](../pinboard/SKILL.md#route-human-task-starts-through-pinboard): give its one gentle clarification, suggest ordinary Pinboard wording, and continue through Pinboard when the item and outcome are clear. Do not enter the worker checks merely to surface missing internal preparation, and do not stop after explaining the route when Pinboard can continue.

## Establish the attempt

Follow the [current MCP request contract](../pinboard/references/runtime-adapters.md#current-mcp-request-shapes) when executing native launch acquisition and continuation inputs. Do not reuse historical flat inputs as a current call shape.

Before a first default initialization or after `SQLITE_READONLY`, follow the shared runtime adapter's [Codex protected project writes](../pinboard/references/runtime-adapters.md#codex-protected-project-writes) rule. A normal checkout uses relative `.pinboard`; a linked worktree or explicit root uses only the exact absolute effective work root reported by recovery. Do not substitute the whole shared repository. A denied attempt-authority write does not establish or renew a lease.

Use the connected Pinboard MCP tools for item status, complete accepted definition and bounded history, brief construction and source preparation, action discovery, attempt inspection, artifact verification, candidate observation and restoration, attempt authority, and lifecycle transition. Preparation authority, overview, proposal creation, brief publication, dispatch, and review-job serve the owning coordination task through the same twenty-tool surface. Every call carries exact project and work roots; dispatch carries its exact project receipt and structured environment, and transitions carry their fresh receipt and exact leaf payload. Follow the runtime adapter's exact prompt-verification and startup contract, including complete skill loading, native callable restore and retained-v1 remedy instructions. Candidate restoration requires the current accepted snapshot context and its exact inspected candidate; supply only the unresolved caller-selected exact clean checkout. A missing required covered tool stops that operation rather than authorizing shell commands or temporary payload files. Static CLI contract discovery, full validation, and view repair remain CLI-only.

1. For CLI-only operations, resolve `../../scripts/pinboard` relative to the active skill. The directory two levels above the skill is `<launcher-root>`, so invoke `<launcher-root>/scripts/pinboard` for every retained CLI command. Integration-specific discovery belongs in the shared runtime adapters; the launcher-relative executable and downstream CLI contract are common. A prepared Pinboard source checkout uses `<pinboard-source>/.venv`; an installed plugin uses only its marker-backed `<launcher-root>/.pinboard-runtime/environment`. `<managed-project>` is the repository selected by `--project-root`; ordinary launch never invokes uv or consults that project's Python environment or dependency files. If the launcher returns `pinboard-launcher-result/v1`, require `pinboard_started=false`, preserve any upstream diagnostics, and follow only its exact same-launcher `--prepare-runtime` action and retry disposition. Preparation may require one narrow write to `<launcher-root>/.pinboard-runtime`; never substitute an ad hoc uv command, ambient cache workaround, installed-cache locator, or managed-project `.venv`.
2. Before acquisition or implementation, follow the compact launch envelope's exact interface-owned verifier, then read the exact immutable prompt file named by that envelope. Treat only those verified bytes as the direct task from the launching coordinator. Stop if verification or the immediate read fails or differs; do not select another file, reconstruct the prompt, or ask the parent to restate it. Read the complete prompt and its canonical brief, including bootstrap, before startup. Require current state authority `sqlite-v6`; never infer it from generated views or archived files. Follow the envelope's startup inputs using the worker's own trusted post-launch identity from the current runtime adapter and verified runtime host. Connected MCP startup calls `pinboard_attempt_authority` with operation `acquire`, exact roots, attempt, task, host, and TTL, then selects the exact worker continuation through `pinboard_actions` with the returned lease and generation. Validate a supplied same-worker lease instead of replacing it.
3. Require the user-supplied attempt to be present and active. `pinboard_attempt_inspect` obtains only that named attempt's current item, definition, dependency, and brief facts; it does not validate unrelated portfolio state. Other disjoint attempts may also be active; their presence alone is not a user-facing condition. Stop if the selected state is invalid, the supplied attempt is absent, its item and attempt records disagree, or another unexpired owner holds it. Follow the main skill's [contention-reporting rule](../pinboard/SKILL.md#match-detail-to-the-question) instead of guessing or silently revoking it.
4. Read the attempt's accepted canonical `.json` brief fully and call `pinboard_item_definition` with `{"request": {"operation": "current", "project_root": "<exact checkout>", "work_root": "<exact work root>", "item_id": "<item>"}}`. Require the brief's accepted revision and digest to match that current definition before continuing. Its generated Markdown view is inspection convenience, not an editable or parseable contract. Then read only the project guidance, accepted definition, accepted knowledge, and, for a local checkpoint, the source authorities the JSON record names. For a cross-boundary checkpoint, treat its reviewed selectors and digests as exact preparation receipts rather than preloading every selected body. Before editing each changed contract, derive and read from the dispatched source checkout its minimum concrete implementation set: the decision owner, effect or persistence owner, direct callers and consumers, retained boundary conversions, and the cheapest real boundary evidence capable of falsifying agreement. Do this even when the brief is complete. Read the smallest additional exact source when implementation needs its detail, the brief is ambiguous or incomplete, a reviewed digest changes, correction evidence changes an owner, a neighboring contract depends on it, or the worker must edit or trace it. Before loading several or potentially large sources, prepare a structured `pinboard-brief-sources/v1` manifest and call `pinboard_brief_sources` operation `plan-to-file` with exact roots, that manifest, a positive batch ceiling and one explicit plan destination. Require its compact receipt, then read each emitted batch once in order through `pinboard_brief_sources` operation `emit-file` with that plan path and batch index. Use operation `plan` and inline `emit` when the complete small plan itself is useful in context. Never overwrite a differing plan destination or replay a committed selected-output failure; inspect the exact path and select a new destination explicitly when needed. Preserve exact selectors and selected digests; reuse unchanged receipts and reread only changed owners and dependent neighbors. If output truncates, continue at the first unread batch or line without replaying returned content. Preparation and independent brief review still freeze semantics, validate selected authorities, and record uncertainty; dispatch digest validation and separate final candidate review remain unchanged.
5. Inspect the checkout, branch/worktree, base revision, and unrelated user changes before editing.

When the accepted brief records repository worktree bootstrap, require that preparation to precede implementation-source changes. If the attempt changes the preparation command, dependency locks, package-manager contract, launcher, validator, or another tool needed to assess itself, preserve the base setup receipt, use stable installed tooling only for coordination, and verify the changed candidate directly through its source or built artifact.

When a native request or retry rule is unclear, use the connected tool's negotiated schema and the selected action's inline input contract. For retained CLI syntax only, use `<launcher-root>/scripts/pinboard tool-contract --json` and one returned operation selector. Do not inspect implementation source or guess fields. Follow the result's declared retry disposition and any fresh same-subject receipt. Never replay after committed effects, including native `failed-after-publication` or `committed-effect`; inspect the named changed surfaces and current attempt continuation first.

Confirm the attempt identity, current definition revision and digest, stable checkpoint ID, and execution environment without rewriting its semantics. Treat the dispatched starting revision and permissions as brief declarations that may narrow the work; they do not grant authority beyond the user and execution environment, and Pinboard does not enforce them. The canonical JSON brief remains the sole source for execution ordering, deferrals, and verification, while the matching immutable definition owns accepted item semantics. Ask only when missing information would change product behavior, architecture, scope, compatibility, or verification expectations.

When reacquiring an attempt returned from review, require the correction-dispatch prompt produced from the exact selected return receipt and current independently reviewed source bytes. Follow its interface-owned acquisition and continuation inputs, then read the prompt's selected return history and canonical reason and the current `review.md` before editing. Keep the same accepted brief, branch, evidence, and attempt identity. Treat the earlier `result.md` as preserved history, not a current readiness claim; refresh it only after the corrected candidate is stable and all required checks pass.

If a replacement brief superseded an outstanding correction, follow the coordination skill's [replacement-brief review recovery](../pinboard/SKILL.md#coordinate-review-responsibility-and-checkout-use). A worker launched by its completed correction dispatch does not replay the coordinator's pre-edit recovery.

## Stay inside the attempt

- Edit only what the attempt requires.
- Keep one writer per checkout. Disjoint attempts may proceed concurrently in separate checkouts.
- Preserve unrelated user changes.
- Follow the repository's own testing, formatting, lint, documentation, and safety guidance.
- Treat a stale instruction as an instruction defect before reshaping working code around it.
- Do not edit generated views, SQLite authority, preparation or attempt leases, or another item's lifecycle state outside the executable workflow.
- Do not accept or complete your own item.
- Prepare the stable candidate for independent review, then follow the current ownership and review-return rules below. `pinboard_dispatch` prepares the exact launch prompt but does not create the worker task.

Worker diff inspection, requirement mapping, and fresh verification are pre-review evidence. They do not replace independent review.

As the candidate takes shape, classify every changed file and contract against the accepted scope and reviewed authorities. Reuse unchanged preparation receipts, revalidate changed relationships, and read the smallest neighboring owner when an unclassified change, explicit uncertainty, repository check, or changed dependency exposes it. Widen beyond that concrete surface only for architecture, persistence, wire, lifecycle, dynamic-consumer, or demonstrated-blind-spot evidence. Record each source set, on-demand read, trigger, and outcome in `result.md`; a stale file prediction is never authority to omit or force a read.

For a cross-boundary attempt, map every observable behavior and behavior-defining test to an authorized Contract row before adding it. The row's `Authorization basis` must resolve to the current accepted scope or to an exact reviewed authority family. Ordinary internal choices that preserve supported behavior—such as helper names, local refactors, and equivalent algorithms—need no separate provenance record.

Apply the main Pinboard skill's [assurance boundary](../pinboard/SKILL.md#keep-assurance-inside-accepted-product-scope) before treating a discovered concern as required work. Continue fixing defects and satisfying assurance already supported by accepted scope, an exact brief contract or criterion, applicable repository policy, an exact reviewed product-authority family, a named production consumer, or an observed supported-path failure. A broad correctness claim, exploratory failing test, public narrative, or newly written guidance is not enough. Keep speculative risks non-blocking; when a finding would materially add an unsupported concurrency, durability, security, adversarial, platform, or compatibility guarantee, stop only that expansion and return the concrete human decision described by the main skill without weakening accepted work.

When cross-boundary implementation extends behavior governed by interacting conditions or operations, read and apply [Preserve independent decision gates](../repository-readiness/references/developer-navigation.md#preserve-independent-decision-gates) through the accepted contracts and evidence. Do not load the method for an ordinary change with no conditional interaction.

When the accepted checkpoint introduces or changes a command or closed-variant family across several production owners, or places dynamic dispatch on that path, read the shared [developer-navigation lens](../repository-readiness/references/developer-navigation.md) and implement the accepted trace and sibling-change shape. If implementation exposes another same-meaning routing site, hidden fallback, or dynamic wiring owner absent from the accepted brief, treat it as a brief omission rather than silently spreading or removing the distinction.

When the canonical brief explicitly selects the optional engineering-health baseline, read the shared [engineering-health baseline](../pinboard/references/engineering-health-baseline.md) and apply it only through the brief's authorized contracts and verification. Do not infer the selection from perceived complexity, architecture impact, or the number of changed files.

Run every basis-bearing entry in the accepted `Verification` section as a mandatory check. Do not add a tool, threshold, platform promise, compatibility obligation, or hardening check to that mandatory list unless the brief gives it an accepted basis. Proportionate exploratory checks remain available when they help implementation, but they do not become acceptance obligations merely because they were run or suggested during review.

If a mandatory verification tool is unavailable despite preparation, make one cheapest observation that distinguishes a missing prerequisite, locked-install failure, permission or network boundary, or tool-under-change condition, then fail fast and return that exact blocker to the outcome owner. Do not guess a substitute, keep retrying, or omit the check. The human decides whether to abort, authorize one investigation or retry, or accept a revised scope and evidence contract that explicitly replaces or removes the obligation.

When a useful addition has no authorized row, stop widening that part of the implementation and report the unsupported addition. Continue independent in-scope work when possible. Create an intake proposal only when the user explicitly authorizes preservation; proposal creation does not authorize implementation. Do not invent a ledger state or transition to represent the discovery.

Treat an implementation-discovered material architectural limitation as an upstream scope condition, not an ordinary internal choice. Compare the finding with the project's current limitations owner and the exact accepted definition and brief. If the change would introduce, widen, preserve, mask, or deepen an unacknowledged material limitation, stop only the affected implementation and return it to the owning task as a brief omission or unresolved product decision. Do not make the limitation acceptable by documenting it yourself, reinterpret generic implementation authority as acknowledgement, or absorb adjacent work. Continue disjoint accepted work when it remains valid. A bounded defect fix that leaves the foundation unchanged and a speculative risk without evidence on a supported path do not trigger this stop rule.

If additional work is useful but not required, invoke `$pinboard-intake` only when the user explicitly wants it preserved. Otherwise mention it in the result without creating shared state.

When authorized intake is nested inside delivery, retain the attempt ID, current accepted objective, and next promised implementation or verification action as a continuation anchor. After the proposal is persisted and optional delivery handling ends, return to that action. Intake alone does not pause or reprioritize the attempt. Before the final result, account for every announced pending action as completed, durably deferred at an exact owner, or blocked by one exact decision.

If a discovered problem blocks the attempt:

1. stop widening the implementation;
2. preserve the current commit/worktree and verification;
3. write `blocker.md` in the active attempt directory with the observation, affected criterion, completed work, and safest next action;
4. return the blocker to the owning task with a concise purpose label and a native clickable link to that Markdown, and apply the main Pinboard skill's readable-artifact rule to every mentioned item, attempt, or related evidence whose Markdown is confirmed available;
5. use `$pinboard-intake` to propose a prerequisite when explicitly requested;
6. use the worker-visible `report-blocker:<attempt>` affordance to report that preserved evidence; it is advisory and has no mutation payload;
7. leave shared lifecycle mutation to the owning task, which must select the exact project action `block:<attempt>` only for dependencies already accepted in the current definition or `pause:<attempt>` when no accepted dependency condition applies; a newly accepted dependency requires a complete item revision and revised-brief recovery, and `block-item:<item>` is only for unstarted intake work.

## Implement and verify

Use the repository's selected testing mode. Prefer the smallest evidence that can disprove the important failure, then run the broader changed-surface gate required by the attempt.

Use `$technical-writing` in quiet editorial mode when this attempt creates or substantially revises a human-facing document, report, readable artifact, or pull-request description. Accepted definitions, briefs, implementation and review evidence, repository facts, and the exact candidate retain authority over meaning. Improve reader orientation and prose without interviewing the human when those facts are sufficient, inventing missing claims, or changing canonical machine-consumed semantics. Use collaborative composition only when the accepted work explicitly includes shaping the document with the user.

Continue through the complete accepted checkpoint while in-scope work can proceed without user input. A green internal seam, implementation milestone, or long-running turn does not by itself warrant commentary; follow the main Pinboard skill's user-facing detail threshold. Successful delivery ends only after the stable candidate and truthful `result.md` are ready, own-leased submission is confirmed, and own authority is released through the return procedure below. A `blocker.md` naming the condition that prevents further work, an explicit user request for a partial stop or background execution, or a host-forced early return preserves the exact incomplete continuation and current authority disposition without claiming successful delivery. State whether any user action is required.

Immediately before writing or refreshing `result.md`, account for every user direction and repository change after the current accepted brief or exact candidate cut point. If any change alters accepted semantics, do not present or submit the stale candidate. Return control to the owning task so it can replace the complete definition, publish the matching brief, clear stale review identity through the state-appropriate project transition, and obtain a new exact-candidate review. Work that implements the unchanged brief and evidence that supports it may proceed normally.

At that same boundary, compare the final diff with every accepted material-limitation decision and the named architecture owner. Confirm that the candidate neither hides the foundational condition behind a compensating layer nor changes its consequence or reopening condition without updated accepted scope.

Before review:

A stable candidate has one of two accepted forms. A working-tree candidate is the `working-tree-state-sha256:<digest>` identity of the actual full `HEAD` revision and exact binary diff from that revision. Its immutable snapshot preserves both inputs. A committed candidate is the full current `HEAD` revision, accepted only while the working tree is clean; its immutable snapshot is the binary diff from the accepted brief base to that revision. Retained `working-tree-sha256:<digest>` evidence identifies only historical patch bytes and cannot authorize a new correction start. Prepare the candidate before observing and submitting its identity. Candidate observation itself is read-only and must truthfully describe the resulting Git state.

Use a clean committed candidate when the intended disposition publishes repository changes. A matching working-tree candidate remains eligible for local-only checkpoint or terminal acceptance; publication does not retroactively turn that snapshot into a commit candidate.

Obtain a working-tree identity through `pinboard_candidate_observe` with exact roots and attempt. Inspect its omitted untracked paths: intended new files must participate through separately authorized exact Git preparation, while unrelated files remain untouched. Reobserve after preparation or any source change, then submit the observed candidate through your own current lease and fresh action. Do not recreate the checksum algorithm or treat a draft report as a frozen candidate. Follow the runtime adapter's [candidate observation contract](../pinboard/references/runtime-adapters.md#current-mcp-request-shapes).

When acceptance requires CI on an exact pushed head, use one unchanged commit throughout:

1. After implementation and required local checks, establish the local commit candidate and confirm that the working tree is clean.
2. Record that full commit identity in `result.md`, then submit it through your own current lease and release your authority through the return procedure below. The outcome owner commissions independent review of that exact commit.
3. After a favorable review, the outcome owner publishes that commit unchanged under the authorized repository disposition.
4. The outcome owner waits for the required CI results and verifies that they belong to that exact commit.
5. Have the owning task accept the same protected candidate when the review and required CI pass, without rebinding its identity.

Apply the [repository-disposition authority](../pinboard/SKILL.md#review-and-completion) to local commit creation, remote publication, and acceptance. Reuse authority already granted; ask the owning task to resolve only a missing authorization before its effect. An exact-head CI requirement does not itself authorize a push. Keep the working-tree candidate route available when acceptance does not require a pushed commit; do not first submit a working-tree identity and later relabel it as a commit to satisfy exact-head CI.

For either candidate form, prepare the review evidence in this order:

1. finish the complete accepted checkpoint;
2. run every command required by the attempt, without replacing it with a narrower package, test, formatter, or linter command;
3. inspect the final diff;
4. identify the stable candidate by commit or working-tree fingerprint;
5. map every acceptance criterion to code, test, or evidence;
6. if an existing test file shrank materially, inventory the removed behavior or test names and identify the replacement evidence;
7. when acceptance claims lifecycle wiring, prove it through the production entry point rather than only through an internal primitive;
8. write `result.md` at the exact attempt path `<work-root>/attempts/<attempt-id>/result.md`. Keep it proportional to the candidate. On correction, replace stale candidate narrative with the current evidence instead of appending another account; retained review and history already preserve the earlier state.

When `result.md` is new or materially refreshed, return it to the owning task with one concise purpose label and one native clickable link. Keep that confirmed link on every later contextual mention of the result; avoid only a separate unchanged-status announcement.

The result must record:

- candidate identity and changed files;
- concise implementation result;
- acceptance-criterion evidence;
- verification commands and outcomes;
- any material test removal and its replacement evidence;
- production-entry-point evidence for lifecycle claims;
- for a cross-boundary checkpoint, startup reviewed-source count and bytes, each changed contract's concrete implementation source set, every on-demand read and trigger, accepted-decision coverage, discovered defects, implementation outcome, and the separate final-review outcome or its pending status;
- when exact invocation capture informed the work, one compact reconciliation per distinct consequential observation with its evidence selector, recovery, available cost, classification, disposition, rationale and reopening condition; consolidate recurrence and omit routine successful calls;
- preserved unrelated changes;
- new concerns or exact unknowns;
- whether the attempt is ready for review or blocked.

Report source reduction and correctness separately; fewer reads are not acceptance evidence.

When the accepted checkpoint is one of several recorded for the item, report its exact candidate and the brief's remaining-work boundary without claiming that the whole item is complete. Do not turn an internal implementation seam into an unrecorded checkpoint. Checkpoint acceptance belongs to the owning task after review; the worker does not archive its own checkpoint evidence or resume the next checkpoint.

## Return the candidate for review

For a caller-selected historical checkpoint, the review job must resolve accepted candidate bytes before it publishes the reviewer prompt. If MCP returns a native retained-v1 recovery invocation, follow its exact required-patch leaf and selected history; fill the unresolved patch bytes with the exact historical patch matching its advertised digest, independently of the current protected candidate. After successful recovery, use the freshly reread ordinary review context and verify the accepted candidate artifact. Do not infer bytes from a mutable checkout, repeat a committed recovery, strengthen historical assurance or permit a patch-only new correction start.

`result.md` makes the candidate durably ready for review. Neither that file nor candidate observation submits the candidate or releases authority. Before a successful return, discover the exact `submit-review:<attempt>` worker action through `pinboard_actions` with your own current lease and generation, then call `pinboard_transition` with its fresh complete receipt and exact candidate payload. Submission belongs to the worker, not the coordinator; do not borrow another task's authority.

Check submission's returned effect, retry disposition and changed surfaces, then call `pinboard_attempt_inspect` with exact roots to confirm the exact protected candidate and review continuation. Release your own current authority through `pinboard_attempt_authority` operation `release` with the same lease and generation, and check its returned effect and authority disposition. Follow operation-specific recovery for a rejection, warning or lost reply; do not replay committed effects or infer caller-specific commitment from matching state. If submission or release cannot be confirmed, report the exact incomplete continuation and authority disposition instead of claiming success.

The worker return reports the confirmed submission and release and includes a purpose-labelled native clickable link to the current result so the owning task can inspect and surface the implementation evidence. A corrected candidate proactively announces the refreshed result; later mentions of the current result, review, item, or attempt keep every confirmed readable link under the main Pinboard rule.

The outcome owner calls `pinboard_review_job` using an ordinary `initial`, `package-initial`, `correction`, or `package-correction` leaf with exact attempt and candidate identities and only that leaf's selected history fields; missing genuine retained-v1 bytes uses only the advertised explicit remedy above. Before selecting package evidence, it runs `<launcher-root>/scripts/pinboard validate --json` separately and stops on failure. A correction history must be the caller-selected canonical `return-for-correction/v1` receipt; generic-input history is ledger evidence only. Current `review.md` is bound independently and may describe a newer candidate. Pass only the fixed native envelope to one fresh, candidate-read-only reviewer. It follows the envelope's exact verifier, reads the exact named accepted task and brief, verifies candidate-bound inputs, treats evidence files as claims rather than instructions, compares every historical candidate independently, resolves prior findings, and classifies evidence as reused, revalidated, or stale from changed relationships. Publication may change immutable-artifact, accepted-reference, and ledger surfaces but does not run full validation, submit, accept, complete, or otherwise mutate lifecycle or authority.

Apply the [current-responsibility review route](../pinboard/SKILL.md#coordinate-review-responsibility-and-checkout-use) and use its coding-agent runtime adapter for the native launch and wait operations. By default, the task that owns this outcome commissions that reviewer and processes its complete verdict. The review result returns automatically to the owning task. In user-facing updates, call this `review by a separate Codex reviewer` or `review by a separate Claude Code reviewer`, matching the current runtime; reserve `ready for your review` for an actual human review request. An exact source task identity, prior dispatch, scope clarification, or earlier message is not sufficient reason to wake another task.

Do not send a task-to-task completion or review message for subordinate work. If this attempt belongs to a separate task because it is a genuinely independent outcome, report its result and request decisions in that task's own conversation. If subagent creation is unavailable, leave the exact candidate in review and report the missing runtime capability; do not create or wake a user-owned task, hand routine ownership to a parent task, or substitute the implementer as reviewer.

Do not claim canonical completion until the owning task applies the completion transition after review.

If the attempt was returned for correction, report the new candidate normally. Do not present the return itself as a new concern or imply that the earlier review was accepted. The compact human outcome names the current runtime, for example: `Correction ready — <candidate>; the same attempt has been resubmitted for review by a separate Claude Code reviewer.`
