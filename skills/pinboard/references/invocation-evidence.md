# Invocation evidence

Load this phase when exact capture or a consequential Pinboard invocation anomaly needs investigation. The [main conversation core](../SKILL.md) owns concern disposition and the human picture. Capture is private evidence, never ledger authority.

### Capture and reconcile consequential invocation evidence

For an initialized project, `<work-root>/contributor-traces.config` decides whether normal Pinboard invocations are captured. The selected work root is the shared ignored `.pinboard` by default; an explicit work root keeps its own setting and traces. Its project mode is created explicitly `off`; an item override of `inherit`, `on`, or `off` changes the effective mode for that item, and the runtime rereads it before each supported CLI or MCP invocation. Effective on declares that exact arguments and results are safe to persist locally without redaction. Automatic traces are private ignored files under `<work-root>/invocation-traces/` with newest-100 retention. A ledger or lifecycle read can still initialize that local setting or publish a trace; read-only ledger behavior does not promise a non-writing filesystem invocation.

Both `pinboard_brief_sources` and `pinboard_brief_source_plan_output` bypass automatic and manual MCP capture. The former reads plans and verified batches without writing; the latter writes only its explicitly selected plan destination. Neither initializes trace settings or publishes invocation traces. Other manual capture remains available through the CLI `--capture-evidence <file> --safe-to-persist-exactly --` wrapper or a dedicated MCP `--capture-evidence-dir <existing-directory> --safe-to-persist-exactly` process, with caller-owned destination and retention and no environment snapshot. [Contributor guidance](../../../CONTRIBUTING.md#diagnose-pinboard-invocations-during-contributor-work) owns setup, toggling, risk and deletion details.

Treat captured files as private evidence, not accepted ledger truth. CLI records preserve the complete launcher argv, original stdout and stderr bytes, and exit status; MCP records preserve the exact SDK-decoded arguments and strictly validated result, without transport bytes or pre-callback client events. A strict result-validation failure preserves an unavailable record with the callback classification and commit reference before the error is re-raised. Distinguish pre-publication `capture-unavailable` from `capture-committed-with-warning`, whose filename retains the published digest and size after a post-publication failure. Never replay a mutation to replace missing, interrupted or externally truncated evidence.

During any work on an enabled Pinboard project or item, investigate consequential Pinboard anomalies when they arise, regardless of the current work item or Codex task. Routine success stays quiet. Inspect the relevant trace, but report selectors and conclusions rather than raw values that may contain secrets. Reconcile evidence at a natural task or review boundary, consolidate recurrence, and keep one compact record for each distinct consequential observation with:

- attempted outcome;
- observed behavior, exact evidence selector when available, and confidence in the conclusion;
- recovery outcome;
- measured command, time or token cost when available, without inventing absent measurements;
- classification as Pinboard product behavior or discovery, evidence handling, caller or harness error, runtime or environment constraint, external client behavior, or model limitation;
- disposition and rationale;
- observable reopening condition for deferred or accepted friction.

Use one explicit disposition: fixed and verified in authorized scope; incorporated into an exact admitted item whose accepted definition covers it; admitted separately with saved priority; deferred because cost, uncertainty or an external constraint prevents a useful remedy; accepted friction with proportionate demonstrated recovery; or unresolved because evidence is insufficient. An actionable Pinboard-owned or project-owned observation is not disposed until it is fixed and verified or an exact admitted item owns it with a saved priority decision. When that ownership needs human authority, ask one admission or priority question at the existing result or review boundary instead of leaving a TODO or creating another workflow stage.
