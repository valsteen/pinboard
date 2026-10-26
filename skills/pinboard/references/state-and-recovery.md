# State and recovery

Use your own runtime's reference, [Codex](runtime-codex.md), [Claude Code](runtime-claude-code.md) or [Antigravity](runtime-antigravity.md), for trusted identity and runtime-native waiting or recovery operations; [the shared runtime adapters](runtime-adapters.md#handling-another-runtimes-attempt) govern an attempt another runtime started. SQLite remains the only lifecycle authority.

## Contents

- [Authoritative state](#authoritative-state)
- [Leases and revocation](#leases-and-revocation)
- [Invalid state](#invalid-state)
- [Interrupted tasks](#interrupted-tasks)
- [Inherited sessions](#inherited-sessions)

## Authoritative state

The project-local work root contains one SQLite authority and generated or immutable supporting artifacts:

```text
.pinboard/
  state.sqlite3
  artifacts/
  views/
```

Resolve the project through Git's shared common directory. A linked worktree therefore uses the primary checkout's `.pinboard`, not a competing ignored root. Default initialization adds only the anchored `/.pinboard/` entry to that shared repository's local Git exclude file; it does not edit committed ignore files or hide unrelated `.codex` content. Read only the selected initialized root; do not scan or substitute other locations. An explicit `--work-root` remains at the exact selected path.

Require `authority: sqlite-v11` from the executable. `state.sqlite3` owns lifecycle, dependencies, attempts, preparation and attempt leases, proposals, history, and accepted artifact references. `views/` is replaceable output and never a fallback authority. Do not reconstruct state from other files.

For a released `sqlite-v6`, `sqlite-v7`, `sqlite-v8`, `sqlite-v9`, or `sqlite-v10` ledger, run `pinboard migrate-schema` while quiescent to read its canonical plan and ID, then run `pinboard migrate-schema --apply <plan-id>` to retain exact predecessor bytes and apply the atomic upgrade. Current v5 plans advance exact v10 to v11, retain `.v10` bytes and add an empty indexed relation for latest decision-batch receipt references. Retained v4 plans advance v9 to v10, retain `.v9` bytes, and backfill indexed checkpoint acceptance facts from strictly decoded receipts and their exact producer attempts. Retained v3 plans advance v8 to v9 after verifying original artifact bytes and ownership relationships. Retained v2 plans produce v8 from v7; retained v1 plans produce v7 from v6. Preview and apply a separate plan after each predecessor step until the executable reports current authority. Preserve the original plan and backup bytes, and never reinterpret an old plan as a current advancement. A failed or interrupted apply resumes with that same ID after inspecting its reported effects.

To reverse unchanged schema-migrated data, preview `pinboard migrate-schema --reverse <forward-plan-id>` at the same selected real root and apply its returned reverse ID. A later ledger write refuses reversal without changing authority; preserve the ledger and exact backup for diagnosis. Existing root-relocation backups, plans, progress, sidecars, aliases and Git exclusions remain historical data until a separate human disposition. They supply no operational recovery route.

Use these nonterminal states:

- `intake`: admitted but not yet ready for selection;
- `ready`: eligible for activation;
- `active`: one current execution attempt for that item; several disjoint items may be active concurrently;
- `paused`: preserved attempt intentionally preempted;
- `blocked`: waiting for named work, evidence, or a decision;
- `deferred`: deliberately unscheduled with a concrete reopen condition;
- `review`: a frozen attempt awaiting independent acceptance.

Terminal state remains queryable as history and does not appear as live work.

## Leases and revocation

Project transitions are direct atomic SQLite changes made by a trusted local task. The supplied task and host strings are audit attribution, not authenticated credentials. Project transitions do not acquire or retain a project-wide lease. During contention, one exact transition commits completely or the prior revision remains; stale actions must be refreshed rather than replayed.

When `--json` returns `pinboard-rejected-operation/v1`, decide recovery from its effect facts before reading the message. `state_changed: false` means the failed invocation changed no Pinboard surface. `status: committed-effect` means the listed immutable artifact, accepted artifact reference, or ledger surface already changed; do not replay it. Use a returned action receipt only as one fresh same-subject alternative, copying its non-null lease and generation together. If no bounded alternative is returned, reacquire or reselect through the contract's named operation rather than inventing authority.

Attempt ownership is renewable and fenced. A worker presents its current attempt lease for item-local transitions. Replacing the attempt owner fences actions retained by the previous owner.

Use forced revocation only with explicit user authority when the recorded holder cannot release or has demonstrably abandoned the lease. Revocation increments the fencing generation. Never infer ownership from task titles, pinning, recency, or semantic similarity.

## Invalid state

When `pinboard validate --json` fails, report a compact recovery packet:

- the diagnostic codes and exact affected SQLite or artifact paths;
- whether the failure is authority, accepted-artifact, or generated-view state;
- the safest supported action that does not guess intent;
- one human question only when intent, scope, or product meaning is missing.

A `VIEW_REFRESH_REQUIRED` warning does not invalidate SQLite authority. Rebuild generated views only when the user authorizes that write or the active attempt requires it.

Do not edit SQLite directly or weaken validation. Recovery beyond initialization from absent `state.sqlite3` is not a general supported command. Preserve the observed failure and return it to the owning implementation when the executable cannot reopen the current database atomically.

## Interrupted tasks

A missing chat receipt is not evidence that a transition failed or succeeded. Never replay a retained action token.

For a task interrupted around a mutation:

1. Run `pinboard validate --json`.
2. Call `pinboard_item_status` operation `item` with exact project and work roots and the intended item. Compare its recorded state with the intended transition; absence from the live overview does not establish a terminal outcome. If the intended item still has its prior state, no transition committed. If it has the intended next state, the SQLite transition committed completely even if the task stopped before reporting it.
3. For an interrupted project transition, select the exact action again from current state and continue only if its semantics and payload are unchanged.
4. For interrupted preparation or attempt work, inspect the corresponding authority `status` and current item state. Continue a supplied same-worker lease only after validating its exact holder and generation. Use preparation `start` only while the item is ready; a paused item continues its existing attempt through the legal resume path, with replacement brief and rebind first if its accepted scope or Git lineage is stale. Use attempt `acquire` with the worker's own task and host when that resumed continuation calls for it. These ownership operations select initial ownership or a permissible inactive transfer under lock; neither tool exposes a `transfer` leaf. A live conflicting holder rejects unchanged. A higher generation fences actions retained by the interrupted task.
5. Use forced lease revocation only with explicit user authority when the recorded holder cannot release it or has demonstrably abandoned it.
6. Resume from the authoritative item and attempt state. Never reconstruct ownership from the stopped task's prose, generated views, archived files, or temporary payloads.

The observable transaction contract remains binary: the previous valid revision or one complete new revision. Report any counterexample as a current SQLite defect rather than routing around it.

## Inherited sessions

A session can inherit saved work from another session of the same runtime, for example a forked or resumed Codex thread that carries the original conversation. When the human asks the inheriting session to continue that work and the original session is unavailable, meaning it ended, was closed, or cannot be reached from the current runtime, the inheriting session becomes the outcome owner. A recorded `owner_task_id` that names the original session is the attempt's activation provenance, not a routing gate; continuation needs no matching owner and no reassignment.

1. Obtain this session's own trusted task and host identity through your own runtime's reference, [Codex](runtime-codex.md), [Claude Code](runtime-claude-code.md) or [Antigravity](runtime-antigravity.md). Never reuse the original session's task or host from inherited conversation, generated views, or earlier tool arguments, and never present its retained receipts, leases, or worker handles as your own.
2. Treat inherited conversation as context, not state. Follow the [interrupted-task](#interrupted-tasks) steps from current authority: validate, read item status, inspect the named attempt, and follow its returned continuation.
3. Read preparation or attempt authority `status` before claiming work. An expired or released claim transfers through the ordinary `start` or `acquire` with this session's identity. A live claim held by the original session or one of its workers stays fenced: wait for it to lapse, or ask the human for forced revocation under [leases and revocation](#leases-and-revocation) when its holder demonstrably cannot release it.
4. The original session's native worker and reviewer handles are unavailable. Do not attempt same-worker continuation through them. When the continuation selects worker or review work, launch a fresh worker or reviewer from this session through the returned dispatch or review job.

While the original session remains available, it keeps outcome ownership. Continue there, or ask the human which session owns the work when both remain in use; inheriting the conversation alone does not move ownership.

Tell the human the outcome in plain words: whether you are continuing the earlier session's work or why you cannot yet, where its partial work lives and whether it reached the integration target, and the one decision you need, such as stopping the other window or overriding an unfinished worker that cannot be confirmed gone. Keep task identities, claims, leases, worker names and native handles out of the reply.
