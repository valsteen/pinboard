# Install Pinboard

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration; Claude Code installs the same plugin through its own marketplace mechanism.

## Launcher and environment paths

Pinboard uses these terms consistently:

- `<launcher-root>` is the directory that contains `scripts/pinboard`. Direct CLI commands invoke this launcher; the plugin's MCP declaration invokes the same launcher with `--mcp`.
- `<launcher-root>/.pinboard-runtime/environment` is the installed private runtime. The first installed MCP connection lazily invokes the same `--prepare-runtime` path; later installed commands use the prepared runtime without uv or its cache.
- `<pinboard-source>/.venv` is the development environment for a prepared Pinboard source checkout. Repository-owned `uv sync` and `uv run` commands create or use this environment only for Pinboard development; `uv build` may use uv's isolated build environment.
- `<managed-project>` is the repository whose work Pinboard coordinates. CLI callers select it with `--project-root <managed-project>`; native tool requests carry explicit `project_root` and `work_root` fields. Pinboard never uses the managed project's Python environment, `.venv`, or dependency files.

The launcher root may be a prepared Pinboard source checkout or an installed plugin version. A prepared source launcher uses `<pinboard-source>/.venv`; an installed launcher uses its private runtime. That runtime choice follows the resolved launcher root, not whether the caller is a human or an agent.

## Codex

Add the GitHub repository as a Codex marketplace, then install Pinboard:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository where you want to use Pinboard and ask:

> Set up Pinboard here and explain how I can use it from one task or several tasks.

The plugin manifest selects `mcp-codex.json` at the plugin root. Codex resolves its `cwd` to that root and starts `sh ./scripts/pinboard --mcp`. The connected Pinboard tools take explicit project and work roots for each request; they do not use the client's current directory as a board selection.

### Use Investigation Focus

In a Codex task, invoke `$investigation-focus` for a substantial investigation. For a clear bounded request, name the result, sources, audience and output format; the skill begins without requiring an inquiry home or a different outcome. For a diffuse question, it offers a few evidence-grounded results so you can choose the direction.

For an inquiry that needs later sessions, choose a local home outside the evidence-source repositories. The skill maintains a human-readable note there, and a later task reads it before revising direction or producing another audience output. The note is optional for a bounded request. Keep original sources and private locators under your control.

On the first connection for an unprepared installed version, when uv is available, the launcher runs `<launcher-root>/scripts/pinboard --prepare-runtime`, which owns the version-local preparation lock, and starts Pinboard only after the ready marker and entry point are valid. Request write access only to that version's `.pinboard-runtime` when needed. If uv is missing or another preparation is in progress, the launcher writes an unchanged `pinboard-launcher-result/v1` recovery result to stderr; if preparation starts and fails, the result reports the runtime as potentially changed. In both cases MCP stdout stays empty and the result names the same `--prepare-runtime` retry. After preparation succeeds, reconnect through the client's supported MCP reconnect mechanism if the connection remains unavailable. Codex does not use plugin data or a SessionStart hook for this boundary; those alternatives remain outside this contract.

Closing a saved item runs through `pinboard_close`, which requires your close decision in your own words; an agent without them should ask you instead of closing. Codex ignores the Claude Code metadata that makes Claude Code v2.1.199 or later confirm each close, so Codex adds no extra close prompt: your Codex approval settings decide whether that call runs without asking.

### Pre-approve Pinboard tools with approval policy never

If you run Codex with `approval_policy = "never"`, pre-approve only the installed Pinboard server's tools in your Codex configuration:

```toml
[plugins."pinboard@pinboard".mcp_servers.pinboard]
default_tools_approval_mode = "approve"
```

`pinboard@pinboard` is the plugin identity installed by the marketplace commands above. Use the actual installed marketplace identity if yours differs. This setting approves that plugin's Pinboard MCP tools; filesystem permissions remain governed by the profile below.

### Allow routine project-data writes

For normal commands from a primary checkout, create a named [Codex permission profile](https://learn.chatgpt.com/docs/permissions) that grants write access only to Pinboard's repository-local data:

```toml
default_permissions = "pinboard"

[permissions.pinboard]
extends = ":workspace"

[permissions.pinboard.filesystem.":workspace_roots"]
".pinboard" = "write"
```

Remove legacy `sandbox_mode` and `sandbox_workspace_write` settings before relying on this profile because they override permission profiles.

### First setup

The first default initialization adds only `/.pinboard/` to the repository's local `.git/info/exclude`, then creates the Pinboard data directory. Approve that exact initialization once; it needs the narrow Git-metadata write only for this setup. Pinboard does not edit `.gitignore`, and `.codex` remains available for unrelated project configuration.

Linked worktrees share the same repository-local exclusion, so repeating setup remains idempotent. If setup stops after making a durable change, Pinboard reports which surface changed so the coding agent can inspect it before retrying.

### Git actions you request

Keep the narrow profile above and enable approval requests in your Codex configuration:

```toml
approval_policy = "on-request"
approvals_reviewer = "user"
```

When you request a commit, merge, push, fetch, branch change or worktree creation, the agent can request runtime approval for that exact command in the selected checkout. Your request authorizes the intended Git effect; Codex approval separately decides whether the command may cross the sandbox boundary. Approve the exact invocation, including its destination and target, without granting persistent `.git` access or a reusable broad command rule. Worktree creation needs approval for its Git metadata and selected destination; the linked checkout still uses the narrow shared-data rule below.

[Auto-review](https://learn.chatgpt.com/docs/sandboxing/auto-review) is optional: set `approvals_reviewer = "auto_review"` to send eligible requests to a separate reviewer while keeping `approval_policy = "on-request"` and the same profile. It changes who reviews the request, not your authority or the default filesystem and network boundary. With `approval_policy = "never"`, the agent cannot request this approval.

After execution, the agent checks the actual effects before reporting success or retrying: the resulting commit or refs, a created worktree, and, for a push, the destination's observed revision. A failed command can still have changed state. If approval is unavailable or rejected, the agent reports the exact action and reason, continues only with a materially safer authorized alternative, or stops for you. It must not retry a reviewer rejection through an indirect command or wider permissions.

### Linked worktrees and custom data locations

A linked worktree uses the shared repository's Pinboard data, which is outside the linked checkout. Run `<launcher-root>/scripts/pinboard root` to obtain the exact resolved location, then add a direct write rule for only that absolute `.pinboard` directory under `[permissions.pinboard.filesystem]`.

Do not add the shared repository as a workspace root. Do not grant persistent access to its `.git` directory, sibling `.codex` paths, or the installed plugin cache.

If you deliberately use `--work-root`, add a direct write rule for only that exact selected directory.

### Move an existing project from `.codex/pinboard`

Default commands do not create a second board when they find only the legacy `.codex/pinboard` location. They return the exact recovery command instead:

```sh
<launcher-root>/scripts/pinboard --project-root /path/to/managed-project migrate-work-root --json
```

Run the migration only while no other Pinboard command is accessing that project. It verifies the current SQLite schema, adds the neutral Git exclusion, moves the directory without rewriting SQLite or artifact bytes, and creates the relative `.codex/pinboard -> ../.pinboard` compatibility alias. It keeps unrelated `.codex` content and any existing legacy exclusion. On a partial failure, inspect the reported changed surfaces before running the command again.

## Claude Code

**Context setting:** `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` disables MCP tool search, loading all connected MCP tool schemas up front and potentially using a large part of the context window. Leave it unset if your gateway supports tool search; if your gateway requires this setting, connect fewer MCP servers. See [Claude Code environment variables](https://code.claude.com/docs/en/env-vars).

Choose a persistent installation or a one-session load. Both need one runtime preparation per installed version before Pinboard's MCP server can start. With uv available, MCP startup and the plugin's SessionStart hook can perform that preparation through the same version-local lock.

### Persistent installation

Add the GitHub repository as a Claude Code marketplace, then install Pinboard:

```sh
claude plugin marketplace add valsteen/pinboard
claude plugin install pinboard@pinboard
```

Start Claude Code in the target project. On the first session of an unprepared version, MCP startup can prepare the runtime and start the server automatically. The SessionStart hook can also run the same `scripts/pinboard --prepare-runtime` path with uv; both use the preparation lock and write only that version's `.pinboard-runtime`. Hook preparation alone does not establish whether the MCP connection succeeded. If Pinboard remains unavailable after preparation, reconnect with `/mcp` or restart Claude Code, then ask Claude Code to set up Pinboard there.

To prepare before the first connection, or when uv is not on Claude Code's PATH, prepare the version yourself before the first session:

```sh
~/.claude/plugins/cache/pinboard/pinboard/*/scripts/pinboard --prepare-runtime
```

Claude Code copies each installed version to `~/.claude/plugins/cache/pinboard/pinboard/<version>/`, where `<version>` is the plugin version with `+` written as `-`. `claude plugin list --json` reports the exact `installPath`. Each version has its own directory, so a version installed by `claude plugin update pinboard@pinboard` is prepared again on its first session. A local checkout works the same way: `claude plugin marketplace add /path/to/pinboard` registers it as the marketplace source.

The Claude manifest selects the separate root `mcp-claude.json`. Claude expands `${CLAUDE_PLUGIN_ROOT}` in its command and invokes that root's `scripts/pinboard --mcp`, independent of the target project's current directory. When the hook cannot prepare the runtime, because uv is missing, another session holds the preparation lock, or preparation fails, it places the observed cause and exact manual command in the agent's context. Asking Claude Code why Pinboard is unavailable surfaces the next step; a failed preparation may have changed the version's `.pinboard-runtime`, as reported by the launcher.

### Permissions

Claude Code cannot install permission rules from a plugin, so in default permission mode it would ask before each of Pinboard's MCP tools. The plugin therefore approves its own MCP tool calls automatically: a PreToolUse hook answers `allow` for every `mcp__plugin_pinboard_pinboard__*` call from a prepared version. The hook covers only tools from the installed Pinboard plugin server. It does not approve shell commands, repository writes outside Claude's existing file permissions, or another MCP server, and it does not bypass Pinboard's receipts and leases.

Closing a saved item is the exception you will still see. `pinboard_close` carries the `anthropic/requiresUserInteraction` tool metadata, and Claude Code v2.1.199 or later documents that it asks you to confirm every such call in every permission mode, even when the hook or an allow rule approves it; in `dontAsk` mode and non-interactive runs such as `claude -p` the close is denied instead. Earlier Claude Code versions do not ask. The close also records your decision in your own words, so an agent without them should ask you rather than close. All other Pinboard tools stay automatically approved.

Claude Code still enforces your own `permissions.deny` and `permissions.ask` rules from every settings source (user, project, local, managed and `--settings` files) regardless of what the hook returns, so a saved deny or ask rule for a Pinboard tool keeps denying or asking. To be asked before each Pinboard call, save an ask rule such as:

```json
{
  "permissions": {
    "ask": [
      "mcp__plugin_pinboard_pinboard__*"
    ]
  }
}
```

To stop the automatic approval entirely, disable the Pinboard plugin or set `disableAllHooks` in your settings; Claude Code offers no way to switch off one plugin hook on its own.

If the hook is unavailable, for example on a version whose runtime is not prepared yet, Claude Code asks as usual. The explicit fallback is the server-scoped allow rule in your user-level `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__plugin_pinboard_pinboard__*"
    ]
  }
}
```

Direct launcher commands, which agents rarely need, run through Claude's Bash tool and are not covered by the hook. To approve them once for every installed version rather than saving a version-pinned rule, use a version-independent Bash rule:

```json
{
  "permissions": {
    "allow": [
      "Bash(*/plugins/cache/pinboard/pinboard/*/scripts/pinboard *)"
    ]
  }
}
```

For an autonomous repository-writing run, also use Claude's normal edit-accepting mode. A linked worktree under `.claude/worktrees/` inside the session's launch directory needed no extra directory grant in an observed auto-mode worker run in this repository; treat that as an observation rather than a guarantee. A selected linked worktree or work root outside the launch directory must be in the session's allowed directories. Pinboard records the intended access but cannot grant it; `dontAsk` may deny an uncovered write instead of asking.

This route uses Claude Code's marketplace mechanism with this repository as the marketplace. Pinboard is not published in or installed from Anthropic's official marketplace, and it does not claim live sharing between Codex and Claude Code.

### One session without installation

```sh
git clone https://github.com/valsteen/pinboard.git
/path/to/pinboard/scripts/pinboard --prepare-runtime
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard
```

The explicit preparation makes the runtime available before the single session starts. Without it, MCP startup or the SessionStart hook can prepare the checkout automatically; use `/mcp` to reconnect only if the connection remains unavailable afterward. A checkout already prepared for Pinboard development by `scripts/prepare-worktree` uses its `.venv` instead, so neither step is needed there.

The free Claude chat plan and Claude Code access are separate product surfaces. Check [Anthropic's current authentication options](https://code.claude.com/docs/en/authentication) before an authenticated smoke test because access can change.

## Local data

By default, Pinboard keeps project decisions and evidence in the managed repository's ignored `.pinboard` directory, created by the [first setup](#first-setup). Primary and linked worktrees share that location; [Linked worktrees and custom data locations](#linked-worktrees-and-custom-data-locations) explains how to find and authorize it. Existing projects that still use `.codex/pinboard` run the one-time migration described in [Move an existing project from `.codex/pinboard`](#move-an-existing-project-from-codexpinboard); it preserves the existing bytes and leaves a compatibility alias.

MCP startup advertises the same input and output schemas for every installation. Field patterns use JavaScript-compatible syntax, including constraints for identifiers, paths, text, and digests. Pinboard still validates requests and results against its canonical typed records. The former `[mcp] omitRegexLookarounds` setting is ignored; existing user configuration files are left as they are.

Contributor traces use `contributor-traces.config` in the selected work root. The default shared `.pinboard` location is ignored by Git; an explicit work root uses its own setting and trace directory, so authorize writes only to that exact root. On first eligible use, Pinboard creates the file privately at mode `0600`, writes `[pinboard "unsafe_persist_exact_pinboard_traces"] mode = off` as one key, and reads the stored mode. An existing file missing that key gets the same write without losing item overrides. The setting, secret risk, retention, and diagnosis procedure are in [Diagnose Pinboard invocations during contributor work](CONTRIBUTING.md#diagnose-pinboard-invocations-during-contributor-work).

Agent workflows use local stdio MCP tools for intake, briefs, inspection, authority, lifecycle changes, worker dispatch, and candidate-bound review publication. The CLI remains available through `<launcher-root>/scripts/pinboard` for root discovery, setup, storage migration, summary status, validation, view repair, portable human export, direct human closure, and its own diagnostics. It is not an agent-workflow fallback.

An installed plugin keeps its private Python environment at `<launcher-root>/.pinboard-runtime/environment`, while Pinboard source development uses `<pinboard-source>/.venv`. Neither environment is created in or borrowed from the managed project.

## Direct CLI use from a source checkout

Humans who want to run Pinboard directly use a known `<pinboard-source>` checkout as `<launcher-root>` rather than locating an application's installed plugin cache or creating a global alias. Prepare `<pinboard-source>/.venv` once, then invoke the same launcher boundary and name `<managed-project>` explicitly:

```sh
cd /path/to/pinboard
scripts/prepare-worktree
/path/to/pinboard/scripts/pinboard --project-root /path/to/managed-project status --json
```

The source-development environment at `<pinboard-source>/.venv` contains Pinboard development dependencies. It is distinct from an installed plugin's `<launcher-root>/.pinboard-runtime/environment` and from any environment or dependency files under `<managed-project>`.

Direct CLI use covers root discovery, initialization, summary status, validation, generated-view repair, portable human export, direct closure, and the CLI's own `tool-contract` diagnostics. Agent workflow tools are available through the connected MCP server, not through alternate CLI routes. A missing connection needs the supported reconnect or reload, not a CLI fallback.

## After setup

After the first successful setup, Pinboard may point to the optional Repository Readiness, Slop Cleanup, and Maintaining Agent Guidance skills. It does not run them, create work, or change configuration.

Pinboard may also recommend the `model_auto_compact_token_limit_scope` setting for long Codex tasks when that user setting is absent. It reads `~/.codex/config.toml`, or the equivalent under `CODEX_HOME`, but never edits user or project Codex configuration. A trusted project's `.codex/config.toml` can override the user default. Returning to an existing Pinboard, a failed setup, or an unreadable or malformed configuration produces no recommendation.

## Troubleshooting

### Pinboard cannot write its project data

For retained CLI mutations, a denied SQLite write reports `SQLITE_READONLY`, the affected location and operation, whether anything changed, and narrow permission recovery. Native tools report their own correlated failure, retry, and changed-surface facts rather than CLI-specific diagnostic prose. A native brief acceptance failure after immutable publication reports `ARTIFACT_ACCEPTANCE_FAILED` and its exact published selector; it does not claim the underlying failure is necessarily a permission error.

For a normal primary checkout, grant only relative `.pinboard`. For a linked worktree or explicit data location, grant only the exact absolute location resolved by Pinboard. The one-time migration additionally needs the exact legacy root, canonical root, compatibility alias, and repository-local Git exclusion named by the command. If an immutable artifact or migration surface was already published, preserve it and inspect current state before selecting supported recovery; do not blindly replay the operation.

### Claude Code reports the pinboard MCP server as failed

`Connection closed` reports a failed connection; it does not establish why startup failed. MCP startup and the SessionStart hook can prepare an unprepared version automatically. Ask Claude Code why Pinboard is unavailable and inspect the launcher diagnostics. When hook preparation fails, its context names the observed cause (uv missing, a held preparation lock, or failed preparation) and the exact manual command from the [Claude Code](#claude-code) section. Run that command against the exact `installPath` from `claude plugin list --json` if preparation is required. After preparation, check tool availability; use `/mcp` to reconnect or restart Claude Code if the connection remains unavailable.

### Claude Code still asks before each Pinboard tool

The PreToolUse hook only runs from a prepared version. Check that preparation finished and Pinboard tools are available; reconnect only if the connection remains unavailable, as described above. Check that the `pinboard` plugin is enabled and that `disableAllHooks` is not set; `/hooks` should list its `PreToolUse` hook for `mcp__plugin_pinboard_pinboard__.*`. A confirmation before `pinboard_close` is expected, not a misconfiguration: Claude Code v2.1.199 or later asks before every close of a saved item, and removing hooks or rules does not change that; Codex and earlier Claude Code versions may not ask. If a prompt still appears for another tool, a deny or ask rule naming it is saved in one of your settings sources; Claude Code enforces those rules regardless of the hook, so remove the rule to restore automatic approval or keep it if you want to be asked. If Claude Code reports a hook error, run the exact `installPath` launcher from `claude plugin list --json` with `--claude-pre-tool-use` on one PreToolUse event to see its stderr diagnostic. As a fallback, the `mcp__plugin_pinboard_pinboard__*` allow rule from the [Permissions](#permissions) section approves the server without the hook.

### The launcher says runtime preparation is required

An unprepared ordinary CLI call exits before Pinboard starts and returns `pinboard-launcher-result/v1` with the exact same-launcher `--prepare-runtime` action. MCP startup first tries automatic preparation; if that fails, its prestart recovery is on stderr so protocol stdout stays empty. CLI recovery is on stdout. Run the reported action with uv available and grant write access only to `<launcher-root>/.pinboard-runtime`. If preparation fails, keep the reported upstream diagnostics and follow its stated retry requirement; do not substitute `<managed-project>/.venv`, an import-path change, or an ad hoc uv command.

Preparation verifies the CLI entry and the availability of the MCP and Claude hook executables before writing the ready marker; a runtime prepared by an older version that lacks one of them is not treated as ready, and `--prepare-runtime` repairs it. After it succeeds, check tool availability and reconnect the client if Pinboard remains unavailable. `scripts/pinboard --mcp` selects `pinboard-mcp`, while ordinary CLI arguments select `pinboard`. MCP startup accepts no additional arguments: roots belong to tool requests. Ordinary installed use does not invoke uv, read its cache, or mutate the prepared plugin tree. A source checkout prepared by `<pinboard-source>/scripts/prepare-worktree` continues to use `<pinboard-source>/.venv`.

For workflow and recovery behavior beyond installation, see [How Pinboard works](HOW_IT_WORKS.md). For system boundaries and current operating assumptions, see [Architecture](ARCHITECTURE.md).
