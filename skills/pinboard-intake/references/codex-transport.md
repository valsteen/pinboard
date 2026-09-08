# Codex task transport

Use this Codex-only adapter when the user explicitly requested delivery to another Codex task. Task existence, project match, or lease ownership never substitutes for that request. Claude Code delivery is owned entirely by the shared coding-agent runtime adapters and must not route through this leaf.

1. Confirm that the explicit request identifies the intended task.
2. Resolve the exact task the user requested through native task discovery and verify that it belongs to the same project root or project identity. If the target is ambiguous, absent, or belongs elsewhere, stop instead of selecting an alternative.
3. Send one compact notification containing the proposal ID, source task ID, shared work root, the confirmed item-view link when available, and the instruction to inspect it at the next safe boundary. Make the proposal's human-facing label the clickable link under the main Pinboard skill's shared readable-artifact rule. Do not send commands or make the notified task responsible for unrelated outcomes.
4. If the requested task's identifier or availability changes between discovery and send, re-resolve that same requested target once. Never redirect delivery to another plausible task. This retry does not need human approval.
5. If the target remains unavailable, the retry fails, or messaging is unavailable, stop delivery and report the requested delivery as unavailable while confirming that the proposal remains saved in the ledger. Do not ask the human to relay it, revoke a lease, or select a replacement merely to reduce notification latency.
6. Treat a successful send as transport delivery only.

Messaging is optional latency reduction only. It does not replace repository persistence or make the notified task the outcome owner. A delivery problem must never be phrased as though the saved proposal itself was lost.

Never use this intake adapter to return subordinate implementation, completion, or review results. Subagent results return automatically to their owning task, while an independent task reports in its own conversation.

If task messaging is unavailable, do not reconstruct it with shell commands, issue comments, commits, or public files. Leave the proposal visible in the ledger and report the requested delivery as unavailable.
