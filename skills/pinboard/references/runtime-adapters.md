# Coding-agent runtime adapters

Pinboard has one CLI, SQLite authority, action vocabulary, brief schema, and skill tree. Use this reference only to translate shared workflow operations into the current coding agent's native outer capabilities. The surrounding skill remains the semantic owner.

| Operation | Codex (primary, stress-tested) | Claude Code (experimental) |
| --- | --- | --- |
| Run Pinboard | Resolve `../../scripts/pinboard` from the active skill. | Run `PINBOARD_RUNTIME=claude "${CLAUDE_PLUGIN_ROOT}/scripts/pinboard" ...`; Claude substitutes the absolute local plugin root in skill content. |
| Task and host identity | Use trusted task and host values exposed by the runtime. | Use trusted session, agent, and host values when exposed. If an exact required identity is unavailable, ask the human for it; never invent one. |
| Checkout isolation | Use the assigned checkout or Codex worktree selected by the owning task. | Use the assigned checkout, `EnterWorktree`, or a worktree-isolated subagent selected by the owning session. Pinboard validates the declared checkout but creates or switches none. |
| Workers and reviewers | Check the native collaboration surface directly, then launch a bounded subagent whose result returns to the owning task. | Check the native subagent surface directly, then launch a subagent whose result returns to the owning session. Use a fresh, candidate-read-only subagent for review in either runtime. |
| Permission declarations | Map already authorized access into the dispatch record and the runtime's actual sandbox or approval controls. | Map already authorized access into the dispatch record and Claude permission controls. In both runtimes, Pinboard records declarations but neither grants nor enforces them. |
| Waiting | Wait on the launched subagent until it completes or needs attention. | Wait on the launched subagent or background task until it completes or needs attention. A prepared dispatch prompt is not a launched worker. |
| Optional messaging | For explicitly requested intake delivery, use the Codex-only [task transport](../../pinboard-intake/references/codex-transport.md). | Only when delivery was explicitly requested and an agent team is already active, resolve the exact named teammate and use `SendMessage` with the proposal ID, source identity, shared work root, and confirmed item-view link when available. Apply the main Pinboard skill's readable-artifact rule to every item or evidence reference in that message. If that teammate changes or the send fails, re-resolve the same target once; never redirect. If the exact target or capability remains unavailable, keep the saved ledger result, report delivery as unavailable, and do not ask the human to relay it. |

Do not enable Claude agent teams, open another session, create another worktree, or widen permissions merely to mimic a Codex transport. Messaging reduces latency; it never gates persistence, ownership, review, or completion.

Do not infer that subagents are unavailable because an unrelated, nested, shell, MCP, or dynamically listed tool surface omits their controls. Report a missing subagent capability only after the runtime's native launch surface is actually absent or unsupported, or an actual required native launch returns an unavailable or unsupported result.
