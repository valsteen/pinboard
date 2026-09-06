# Coding-agent runtime adapters

Pinboard has one CLI, SQLite authority, action vocabulary, brief schema, and skill tree. Use this reference only to translate shared workflow operations into the current coding agent's native outer capabilities. The surrounding skill remains the semantic owner.

| Operation | Codex (primary, stress-tested) | Claude Code (experimental) |
| --- | --- | --- |
| Run Pinboard | Resolve `../../scripts/pinboard` from the active skill. | Run `PINBOARD_RUNTIME=claude "${CLAUDE_PLUGIN_ROOT}/scripts/pinboard" ...`; Claude substitutes the absolute local plugin root in skill content. |
| Task and host identity | Use trusted task and host values exposed by the runtime. | Use trusted session, agent, and host values when exposed. If an exact required identity is unavailable, ask the human for it; never invent one. |
| Checkout isolation | Use the assigned checkout or Codex worktree selected by the owning task. | Use the assigned checkout, `EnterWorktree`, or a worktree-isolated subagent selected by the owning session. Pinboard validates the declared checkout but creates or switches none. |
| Workers and reviewers | Launch a bounded subagent whose result returns to the owning task. | Launch a native subagent whose result returns to the owning session. Use a fresh, candidate-read-only subagent for review in either runtime. |
| Permission declarations | Map already authorized access into the dispatch record and the runtime's actual sandbox or approval controls. | Map already authorized access into the dispatch record and Claude permission controls. In both runtimes, Pinboard records declarations but neither grants nor enforces them. |
| Waiting | Wait on the launched subagent until it completes or needs attention. | Wait on the launched subagent or background task until it completes or needs attention. A prepared dispatch prompt is not a launched worker. |
| Optional messaging | Use task discovery and task-to-task messaging only when the user explicitly requested it. | Use `SendMessage` only inside an already active agent team when the user explicitly requested it. If the capability or exact target is unavailable, keep the durable ledger result and report only the requested delivery outcome. |

Do not enable Claude agent teams, open another session, create another worktree, or widen permissions merely to mimic a Codex transport. Messaging reduces latency; it never gates persistence, ownership, review, or completion.
