# Parallel Work

Use this workflow for requests such as:

- “What can run independently right now?”
- “Let me choose a batch from the safe work.”
- “Launch all safe work in parallel.”

Do not use it merely because more than one item exists. Ordinary next-work selection stays in the main skill.

Use [the coding-agent runtime adapters](runtime-adapters.md) for native task, subagent, worktree, permission, and waiting operations. This reference owns only the shared selection and launch decisions.

## Build the preview

1. Require authority `sqlite-v4`.
2. Run `pinboard parallel preview --json`.
3. Present the result in two compact groups:
   - **Ready together:** the unambiguous all-safe set.
   - **Not ready:** excluded items with the command's exact reason translated into ordinary language.
4. Add one execution-form recommendation to every launchable item:
   - Use a **Subagent** for bounded work that belongs to the current outcome and should return to its owning task. Keep design, refinement, new-authority, live-application, external-write, and likely user-input decisions in the owning task until the work has a complete accepted brief, an independently buildable checkpoint, and already-authorized permissions.
   - Use a **Separate task** only for a genuinely independent outcome that the human intends to follow in that task's own conversation. It is not a subordinate worker and does not return its result to the current task.
5. Explain the recommendation in one short phrase and identify the task the human should follow. Do not recommend a separate task merely because work is incomplete, interactive, approval-sensitive, or structurally independent.

If the request was only to list or preview, stop after presenting the groups. A read-only preview is not launch permission.

## Resolve batch authority

Treat either of these as explicit launch authority:

- the user names an exact subset from the preview;
- the user says to launch all safe work.

“All safe” means the preview's **Ready together** group.

Before creating anything, rerun one selected preview containing every authorized item:

```text
pinboard parallel preview --item <first> --item <second> --json
```

Proceed only when `safe` is true. Preserve its revision as the batch observation. If it is false, show the changed reason and ask only for the decision that the new state requires.

## Launch each outcome

Work through the authorized items in the presented order. Before each external creation, rerun the selected preview for that item and every not-yet-created item. Stop the remaining batch if structural safety changed; tasks already created remain real and must be reported.

For a **Separate task**:

1. Use the environment's native task creation capability. This is authorized by the user's exact batch request.
2. Give it the repository root, item identity, and fresh preview revision, with an instruction to use Pinboard to inspect the item and apply only its own legal transitions. When its checkout or worktree must be selected, use the main skill's project-specific baseline inference and confirmation rule; do not assume that a local branch named `main` is current or that any fixed remote name is authoritative.
3. Tell the human to follow that independent outcome in the new task. It reports and requests decisions there; do not use task-to-task messaging to return its result to the current task.
4. Do not replace it with a subagent when separate-task creation is unavailable, because the human selected a separate outcome and conversation.

For a **Subagent**:

1. Keep the current task as the outcome owner and resolve incomplete, ambiguous, or interactive decisions there.
2. Follow the main skill's delegated-attempt procedure, including the canonical attempt brief and exact dispatch prompt.
3. Launch it through the environment's subagent capability only after the dispatch check succeeds; its result returns automatically to the owning task.

Task creation is an external effect, not a ledger transaction. Do not claim atomic launch or try to roll back a successfully created task because a later creation failed.

## Report the batch

Keep the report compact and exact:

| Item | Form | Result |
| --- | --- | --- |
| `<item>` | separate task or subagent | created with task identifier, or not created with exact cause |

Say `batch launched` only when every authorized item was created. Otherwise say `partial launch`, identify what exists, name the first changed-state or transport failure, and state whether retry needs user action. Never count a prepared prompt, retained proposal, or attempted message as a created task.
