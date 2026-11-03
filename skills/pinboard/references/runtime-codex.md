<!-- roles: all -->

# Codex runtime adapter

<!-- roles: all -->

These are the codex instructions for the role named at the top of this document. Shared rules are inline above.

<!-- roles: all -->

## Connection

<!-- roles: all -->

Codex's explicit root `mcp-codex.json` declares `sh ./scripts/pinboard --mcp` with `cwd: "."`, resolved under the plugin root. It starts the shared `pinboard-mcp` entry through the same runtime owner as the other runtimes. Codex loads no Pinboard hook: Codex discovers default plugin hooks only at `hooks/hooks.json`, which Pinboard does not supply.

<!-- roles: all -->

## Operations

<!-- roles: all -->

| Operation | Codex (primary, stress-tested) |
| --- | --- |
| Run Pinboard | Discover the connected Pinboard MCP tools and pass exact roots. For CLI-only operations, run `<launcher-root>/scripts/pinboard` using the plugin root resolved from this startup document’s actual location. |
| Verified record inputs | For oversized completed briefs or reviews, use only the connected declared file inputs with the exact selected path and raw SHA-256; retain inline controls and all independent checks. Missing capability still stops its operation. |

<!-- roles: coordinator -->

| Operation | Codex (primary, stress-tested) |
| --- | --- |
| Task and host identity | Use trusted task and host values exposed by the runtime. A forked or resumed thread that inherits another thread's conversation uses its own current identity, never the inherited thread's. A launched worker reads its own `CODEX_THREAD_ID`, never `CODEX_SESSION_ID`, and uses the verified dispatch host. |
| Checkout isolation | Use the assigned checkout or Codex worktree selected by the owning task. When a selected Codex-managed worktree is detached, create a bounded local branch in that same checkout before brief construction; do not create a second worktree merely to obtain a branch name. Continue to use the exact shared work root. |
| Permission declarations | Map already authorized access into the dispatch record and the runtime's actual sandbox or approval controls. Pinboard records declarations but neither grants nor enforces them. |
| Workers and reviewers | Check the advertised native collaboration surface, then call the returned `native_launch.tool` with exactly `native_launch.arguments`. The recipe selects `spawn_agent`, supplies the complete `message`, and requires `fork_turns: "none"` for a genuinely fresh context rather than inherited conversation or a user-owned task. Results return to the owning context. If the required callable is absent, stop native execution. |
| Startup loading | A launched worker or reviewer reads the complete generated role/runtime document named by its verified native envelope. The complete native-read and startup-text fallback procedure is inline here. A mention, catalog entry or verified brief is not complete loading; missing complete content stops startup. |
| Worker continuation | Continue the same launched worker through its actual returned native task handle and supported follow-up operation only for direction already covered by its accepted brief. |
| Waiting | Wait on the launched subagent until it completes or needs attention. |
| Optional messaging | For explicitly requested intake delivery, use the Codex-only [task transport](../../pinboard-intake/references/codex-transport.md). |

<!-- roles: launched -->

| Operation | Launched Codex role |
| --- | --- |
| Own identity | Read your own `CODEX_THREAD_ID`, never `CODEX_SESSION_ID`, an inherited identity or the parent’s identity; use the verified dispatch host. |
| Checkout | Use the exact assigned checkout and shared work root. Pinboard’s declarations grant no runtime access. |
| Runtime access | Use only the declared access and this runtime’s actual approval controls. Missing access stops the affected operation and is reported to the outcome coordinator; declarations grant and enforce nothing. Never edit saved permission policy or substitute another client. Reviewers remain candidate-read-only. |
| Complete startup loading | Read this actual resolved startup file completely through native loading or native `read_file`; when native complete reading is unavailable or refused, use `pinboard_startup_text` `file-start` with exact roots and `max_batch_bytes: "host-profile"`, following each continuation verbatim until null, one page per outer response. |
| Return | Return through this launched agent’s native final response. |

<!-- roles: coordinator -->

If the exact recipe cannot launch because its Codex task name was consumed by an invalid pre-verification launch, stop that context and follow [fresh launch-response recovery](preparation-and-dispatch.md#recover-a-worker-or-reviewer-launch-response). Obtain a new validated dispatch or candidate review-job response and use its new arguments exactly. Do not edit a task name or digest or follow up an invalid reviewer to repair its task. The same recovery applies when the successful response was lost; it neither substitutes another client nor bypasses worker submission, independent review or runtime permission.

<!-- roles: coordinator -->

## Close confirmation

<!-- roles: coordinator -->

Codex ignores the `anthropic/requiresUserInteraction` metadata on `pinboard_close` and applies its own approval settings. The human's own close decision in `human_decision` remains required.

<!-- roles: all -->

## Lost tool results

<!-- roles: all -->

Codex may trim any tool result before compaction, including a recovery read such as `pinboard_brief_lookup`. Pinboard cannot prevent that client behavior. After compaction, repeat the same read if its result was also lost; retain the original roots, attempt and revision so recovery remains possible.

<!-- roles: all -->

## Native human questions

<!-- roles: all -->

Apply the shared [native human questions](runtime-adapters.md#native-human-questions) rule. In Codex, ask in ordinary chat whenever the main policy selects it, even if a native question tool is available. For a short, optional, self-contained clarification, prefer `request_user_input_async` when available and permitted; only this route permits independent authorized work while its answer is pending. Use `request_user_input` only when its current mode and purpose restrictions allow the choice; a Plan-only tool is unavailable in Default mode, and a tool that excludes permission requests cannot collect them. Follow the selected tool's question and choice limits. If neither tool is exposed or permitted for a bounded choice, use the shared policy’s ordinary-text fallback.

<!-- roles: coordinator worker -->

## Source-worktree setup permission retry

<!-- roles: coordinator worker -->

For the one retry that the shared [source-worktree setup recovery](runtime-adapters.md#source-worktree-setup-recovery) permits, use the execution tool's `sandbox_permissions: "require_escalated"` with a justification naming the observed restriction and the authorized setup command. Request only that invocation; do not add a persistent approval rule.

<!-- roles: coordinator worker -->

## Codex protected project writes

<!-- roles: coordinator -->

Fresh default initialization is special: approve the exact `<launcher-root>/scripts/pinboard init` command once because it first idempotently adds only `/.pinboard/` to the shared repository's `.git/info/exclude`, then creates `.pinboard/`. Do not grant persistent `.git` write access. Pinboard never edits `.gitignore`, and sibling `.codex` paths remain visible to Git. If a later initialization step fails, inspect the exact effect before issuing a fresh command: `committed-effect` names `repository-git-exclude` when this invocation added it and also names `ledger` when it published the database before failing. A repeat that commits neither surface is an unchanged rejection.

<!-- roles: coordinator worker -->

For routine Pinboard commands from a normal checkout at the default work root, select a Codex permission profile that extends `:workspace` and whose only protected workspace override writes relative `.pinboard`. From a linked worktree, use only a direct write rule for the exact resolved absolute shared-repository `.pinboard` directory; do not add the shared repository as a workspace root. For an explicit `--work-root`, grant write access to that exact selected directory instead. Automatic trace settings and destinations follow the selected work root. Remove legacy `sandbox_mode` and `sandbox_workspace_write` settings because they override permission profiles. The narrow profile must not write sibling `.codex` content, `.git`, the rest of the shared repository, or the installed plugin cache; packaged plugin code and skills are immutable runtime inputs, while the selected work-root tree is project data.

<!-- roles: coordinator worker -->

For a Git action the human explicitly authorized, keep that profile and use the runtime's exact-command approval mechanism. Codex needs `approval_policy = "on-request"`; `approvals_reviewer = "user"` requests manual approval, and optional `"auto_review"` requests a separate [Auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review). With `"never"`, or when the approval capability is unavailable, stop the affected Git action and name what the human must perform or enable. Human authority is not runtime approval, and neither proves the effect.

<!-- roles: coordinator worker -->

Request the exact Git invocation through the current execution tool's `sandbox_permissions: "require_escalated"`, with a justification naming the authorized action, checkout and target. Do not add a persistent approval rule, `.git` write grant, shared repository workspace root or policy override. Approval for worktree creation covers its selected destination and Git metadata only; it does not authorize a sibling Git action or replace the linked checkout's exact shared-work-root rule.

<!-- roles: coordinator worker -->

After execution, inspect the resulting Git state before claiming success or retrying: commit or refs, created worktree and any pushed destination's observed revision. Preserve an initial sandbox failure as evidence when an approved command later recovers; a failed command can still commit an effect. Distinguish that failure from an explicit approval rejection. For a reviewer rejection, report the exact action and rationale, pursue only a materially safer authorized alternative, or stop for the human. Do not retry the same outcome through indirect execution, wider permissions or another client.

<!-- roles: coordinator worker -->

On `SQLITE_READONLY`, use the structured database path, operation, retry disposition, effect disposition, changed surfaces, and recovery naming the exact rule that needs write access: relative `.pinboard` only for a normal default checkout, otherwise the exact absolute effective work root. Correct the narrow profile before issuing a fresh command. Never replay an operation whose result reports a committed immutable artifact; inspect current state and continue through the operation-specific recovery instead.
