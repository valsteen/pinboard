# State and recovery

## Contents

- [Authoritative state](#authoritative-state)
- [Leases and revocation](#leases-and-revocation)
- [Invalid state](#invalid-state)
- [Interrupted tasks](#interrupted-tasks)

## Authoritative state

The project-local work root contains one SQLite authority and generated or immutable supporting artifacts:

```text
.codex/pinboard/
  state.sqlite3
  artifacts/
  views/
```

Resolve the project through Git's shared common directory. A linked worktree therefore uses the primary checkout's `.codex/pinboard`, not a competing ignored root. Default initialization adds only the anchored `/.codex/pinboard/` entry to that shared repository's local Git exclude file; it does not edit committed ignore files or hide unrelated `.codex` content. An explicit `--work-root` remains at the exact selected path.

Require `authority: sqlite-v4` from the executable. `state.sqlite3` owns lifecycle, dependencies, attempts, preparation and attempt leases, proposals, history, and accepted artifact references. `views/` is replaceable output and never a fallback authority. Do not reconstruct state from other files.

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

Project transitions are direct atomic SQLite changes authorized by the invoking task and host identity. They do not acquire or retain a project-wide lease. During contention, one exact transition commits completely or the prior revision remains; stale actions must be refreshed rather than replayed.

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
2. Run one `pinboard status --json` or `pinboard overview --json` read. If the intended item still has its prior state, no transition committed. If it has the intended next state, the SQLite transition committed completely even if the task stopped before reporting it.
3. For an interrupted project transition, select the exact action again from current state and continue only if its semantics and payload are unchanged.
4. For interrupted preparation or attempt work, acquire or transfer the exact lease through its supported command. A higher generation fences actions retained by the interrupted task.
5. Use forced lease revocation only with explicit user authority when the recorded holder cannot release it or has demonstrably abandoned it.
6. Resume from the authoritative item and attempt state. Never reconstruct ownership from the stopped task's prose, generated views, archived files, or temporary payloads.

The observable transaction contract remains binary: the previous valid revision or one complete new revision. Report any counterexample as a current SQLite defect rather than routing around it.
