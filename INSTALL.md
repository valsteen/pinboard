# Install Pinboard

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration; Claude Code installs the same plugin through its own marketplace mechanism, and Antigravity CLI (`agy`) installs the same repository as a native Antigravity plugin from a local clone.

## Launcher and environment paths

Pinboard uses these terms consistently:

- `<launcher-root>` is the directory that contains `scripts/pinboard`. Direct CLI commands invoke this launcher; the plugin's MCP declaration invokes the same launcher with `--mcp`.
- `<launcher-root>/.pinboard-runtime/environment` is the private Python environment for an installed plugin.
- `<pinboard-source>/.venv` is the development environment for a prepared Pinboard source checkout. Repository-owned `uv sync` and `uv run` commands create or use this environment only for Pinboard development; `uv build` may use uv's isolated build environment.
- `<managed-project>` is the repository whose work Pinboard coordinates. CLI callers select it with `--project-root <managed-project>`; native tool requests carry explicit `project_root` and `work_root` fields. Pinboard never uses the managed project's Python environment, `.venv`, or dependency files.

The launcher selects the environment based on its own directory. Use [source setup](#direct-cli-use-from-a-source-checkout) for development or [installed runtime preparation](#the-launcher-says-runtime-preparation-is-required) for a plugin.

## Codex

Add the GitHub repository as a Codex marketplace, then install Pinboard:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository where you want to use Pinboard and ask:

> Set up Pinboard here and explain how I can use it from one task or several tasks.

The plugin manifest selects `mcp-codex.json` at the plugin root. Codex resolves its `cwd` to that root and starts `sh ./scripts/pinboard --mcp`. The connected Pinboard tools take explicit project and work roots for each request; they do not use the client's current directory as a board selection.

Codex starts [runtime preparation](#the-launcher-says-runtime-preparation-is-required) through that MCP connection. If tools stay unavailable, see [reconnecting](#reconnect-when-pinboard-stays-unavailable).

### Use Investigation Focus

In a Codex task, invoke `$investigation-focus` for a substantial investigation. For a clear bounded request, name the result, sources, audience and output format; the skill begins without requiring an inquiry home or a different outcome. For a diffuse question, it offers a few evidence-grounded results so you can choose the direction.

For an inquiry that needs later sessions, choose a local home outside the evidence-source repositories. The skill maintains a human-readable note there, and a later task reads it before revising direction or producing another audience output. The note is optional for a bounded request. Keep original sources and private locators under your control.

### Optionally pre-approve Pinboard tools

To avoid repeated Pinboard MCP tool prompts, you can pre-approve the installed server in your Codex configuration. This is independent of `approval_policy`: it also works with interactive policies such as `"on-request"`. With `"never"`, calls that require approval need pre-approval because Codex cannot ask:

```toml
[plugins."pinboard@pinboard".mcp_servers.pinboard]
default_tools_approval_mode = "approve"
```

`pinboard@pinboard` is the plugin identity installed by the marketplace commands above. Use the actual installed marketplace identity if yours differs. The server default covers its tools unless an explicit per-tool `approval_mode` overrides it. To require a [close confirmation](#close-confirmation) under an interactive approval policy, use:

```toml
[plugins."pinboard@pinboard".mcp_servers.pinboard.tools.pinboard_close]
approval_mode = "prompt"
```

Approving a selected list of tools covers only those names; another or newly added tool still follows the server default. Review existing per-tool settings when prompts continue. See [Codex plugin MCP policy](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks).

MCP approval controls whether Codex may call the tool. It does not grant filesystem or network access, approve setup commands or Git actions, or authorize an effect you have not requested. Keep the narrow profile below; use the separate [Git approval](#git-actions-you-request) and [setup recovery](#setup-commands-ask-for-cache-or-network-access) when those boundaries need approval.

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

A failed Git command can still have changed a commit, ref, worktree, or push destination. See [Codex Git effect verification](skills/pinboard/sources/native.detail.codex.git-effect-verification.md) for how Pinboard handles failed and denied actions.

### Linked worktrees and custom data locations

A linked worktree uses the shared repository's Pinboard data, which is outside the linked checkout. Run `<launcher-root>/scripts/pinboard root` to obtain the exact resolved location, then add a direct write rule for only that absolute `.pinboard` directory under `[permissions.pinboard.filesystem]`.

Do not add the shared repository as a workspace root. Do not grant persistent access to its `.git` directory, sibling `.codex` paths, or the installed plugin cache.

If you deliberately use `--work-root`, add a direct write rule for only that exact selected directory.

## Claude Code

**Context setting:** `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` disables MCP tool search, loading all connected MCP tool schemas up front and potentially using a large part of the context window. Leave it unset if your gateway supports tool search; if your gateway requires this setting, connect fewer MCP servers. See [Claude Code environment variables](https://code.claude.com/docs/en/env-vars).

Choose a persistent installation or a one-session load.

### Persistent installation

Add the GitHub repository as a Claude Code marketplace, then install Pinboard:

```sh
claude plugin marketplace add valsteen/pinboard
claude plugin install pinboard@pinboard
```

Start Claude Code in the target project and ask it to set up Pinboard there. See [runtime preparation](#the-launcher-says-runtime-preparation-is-required) for first-session setup and [reconnecting](#reconnect-when-pinboard-stays-unavailable) if tools stay unavailable.

Claude Code copies each installed version to `~/.claude/plugins/cache/pinboard/pinboard/<version>/`, where `<version>` is the plugin version with `+` written as `-`. `claude plugin list --json` reports the exact `installPath`. To use a local checkout as the marketplace source, run `claude plugin marketplace add /path/to/pinboard`.

The Claude manifest selects `mcp-claude.json`. Claude expands `${CLAUDE_PLUGIN_ROOT}` in its command and invokes that root's `scripts/pinboard --mcp`, independent of the target project's current directory.

### Permissions

Claude Code cannot install permission rules from a plugin, so in default permission mode it would ask before each of Pinboard's MCP tools. The plugin therefore approves its own MCP tool calls automatically: a PreToolUse hook answers `allow` for every `mcp__plugin_pinboard_pinboard__*` call from a prepared version. The hook covers only tools from the installed Pinboard plugin server. It does not approve shell commands, repository writes outside Claude's existing file permissions, or another MCP server, and it does not bypass Pinboard's receipts and leases.

See [close confirmation](#close-confirmation) for the exception to automatic approval.

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

For an autonomous repository-writing run, use Claude's `acceptEdits` mode to accept ordinary file edits in its working directories. The MCP hook and the Bash allow rule above do not select an edit mode or approve arbitrary Bash commands. `dontAsk` may deny an uncovered write instead of asking. See [Claude permission modes and rules](https://code.claude.com/docs/en/permissions).

A linked worktree under `.claude/worktrees/` inside the session's launch directory needed no extra directory grant in an observed auto-mode worker run in this repository; treat that as an observation rather than a guarantee. A selected linked worktree or work root outside the launch directory must be in the session's allowed directories before the run. Add only those selected locations with `/add-dir` or `--add-dir`; their editing permissions still follow the current mode. Pinboard records the intended access but cannot grant it.

Claude's [Bash sandbox](https://code.claude.com/docs/en/sandboxing) separately limits filesystem and network access for shell commands and their child processes. File tools, MCP servers and hooks run outside that sandbox; the Pinboard hook approves only its own MCP calls. If a setup command is sandboxed, its cache writes and downloads may still need specific path or host access even when Pinboard MCP tools are approved.

This route uses Claude Code's marketplace mechanism with this repository as the marketplace. Pinboard is not published in or installed from Anthropic's official marketplace, and it does not claim live sharing between Codex and Claude Code.

### One session without installation

```sh
git clone https://github.com/valsteen/pinboard.git
/path/to/pinboard/scripts/pinboard --prepare-runtime
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard
```

The explicit preparation makes the runtime available before the session starts. You can let [first-use preparation](#the-launcher-says-runtime-preparation-is-required) handle this step or use the existing `.venv` in a checkout already prepared with `scripts/prepare-worktree`. See [reconnecting](#reconnect-when-pinboard-stays-unavailable) if tools stay unavailable.

The free Claude chat plan and Claude Code access are separate product surfaces. Check [Anthropic's current authentication options](https://code.claude.com/docs/en/authentication) before an authenticated smoke test because access can change.

## Antigravity

Antigravity CLI (`agy`) loads the same repository as a native Antigravity plugin. Install it from a local clone:

```sh
git clone https://github.com/valsteen/pinboard.git
agy plugin install /path/to/pinboard
agy plugin list --json
```

`agy plugin install` copies the whole directory to `~/.gemini/config/plugins/pinboard/` and reads `.antigravity-plugin/plugin.json`, the root `mcp_config.json` and the root `hooks.json`. `agy plugin list --json` should report the plugin with `"source": "antigravity"`, the `pinboard` MCP server and the `pinboard` hooks. Install from a clean clone: the copy takes everything in the directory, and the launcher prefers a development `.venv` when one is present. `agy plugin validate /path/to/pinboard` checks the plugin; its warnings that two skill bodies exceed 24000 bytes and that `AGENTS.md` is never loaded are expected.

### Allow Pinboard tools

Antigravity runs a plugin's MCP tools only under its own permission rules, and a plugin cannot install them. Add Pinboard's server to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`, keeping any rules already there:

```json
{
  "permissions": {
    "allow": [
      "mcp(pinboard_pinboard/*)"
    ]
  }
}
```

Pinboard never writes this file. Without the rule, an interactive session asks before each Pinboard call and a headless `agy -p` run denies it. A rule in a project-level settings file did not grant the call in the observed version.

Antigravity routes every server's MCP calls through the PreToolUse hook. The prepared Pinboard hook allows its own tools, subject to [close confirmation](#close-confirmation), and leaves other servers' tools to your Antigravity approval rules.

### Unattended headless runs

A Pinboard reviewer computes the SHA-256 digests of the files it checks itself, and in Antigravity that takes a shell command. Before you leave a headless `agy -p` run to delegate review, add an allow rule for the hash command next to the Pinboard rule, for example `command(shasum)`:

```json
{
  "permissions": {
    "allow": [
      "mcp(pinboard_pinboard/*)",
      "command(shasum)"
    ]
  }
}
```

Workers and reviewers run as Antigravity subagents, and in the observed version a headless run does not deliver a refusal to a subagent. When a worker or reviewer needs a command that has no allow rule, the subagent stalls and the run waits indefinitely with no message instead of failing. Allow the commands your workers and reviewers need, such as the hash command and the project's build and test commands, or stop a run that stops making progress. An interactive session is expected to ask you about such a command instead, but this has not been confirmed.

### Linked worktrees, command approvals, and macOS Seatbelt Python

Antigravity matches command approvals against the leading binary. A binary-level rule in `permissions.allow`, such as `command(uv)`, `command(git)`, or `command(ruff)`, permits any invocation of that binary in the session. Leave approval interactive if you want to review an individual invocation or subcommand prefix.

On macOS, Antigravity's Seatbelt sandbox can report `Operation not permitted (os error 1)` for a worktree's uv-managed Python under `~/.local/share/uv/python`. An unsandboxed retry for the affected checkout still requires your command approval. See the [Antigravity checkout instructions](skills/pinboard/sources/shared.runtime.antigravity.checkout-isolation.md) for command forms and recovery.

### First Antigravity session

Start agy in the project where you want to use Pinboard and ask:

> Set up Pinboard here and explain how I can use it from one session or several.

See [runtime preparation](#the-launcher-says-runtime-preparation-is-required) and [reconnecting](#reconnect-when-pinboard-stays-unavailable) if tools stay unavailable.

For headless `agy -p`, allow `read_file` for plugin files outside the workspace. See [Antigravity startup and permissions](skills/pinboard/sources/native.antigravity.setup.md) for native reads and [checkout instructions](skills/pinboard/sources/shared.runtime.antigravity.checkout-isolation.md) for workers and reviewers sharing the parent's workspace.

### Upgrade or remove

Reinstall from the updated clone:

```sh
agy plugin uninstall pinboard
agy plugin install /path/to/pinboard
```

Each install is a fresh copy that needs [runtime preparation](#the-launcher-says-runtime-preparation-is-required). Uninstalling removes the copy and leaves `~/.gemini/config/import_manifest.json`, Antigravity's cached Pinboard tool schemas under `~/.gemini/antigravity-cli/mcp/pinboard_pinboard/`, and your `permissions.allow` rule, which you can remove yourself.

## Local data

By default, Pinboard keeps project decisions and evidence in the managed repository's ignored `.pinboard` directory, created by the [first setup](#first-setup). Primary and linked worktrees share that location; [Linked worktrees and custom data locations](#linked-worktrees-and-custom-data-locations) explains how to find and authorize it. Explicit data locations remain at the selected path under the ordinary root rules.

Contributor traces live under the selected work root. See [invocation diagnostics](CONTRIBUTING.md#diagnose-pinboard-invocations-during-contributor-work) for the settings, retention, and exact capture commands.

See [MCP architecture](ARCHITECTURE.md#mcp) for the tool and schema boundary.

## Startup page size per host

The `startup-page-bytes` setting controls how much oversized evidence Pinboard returns per page. See [startup text and fixture review](ARCHITECTURE.md#mcp) for how agents read instructions and evidence.

The default page sizes for Claude Code and Codex fit within each host's default tool output limit. Antigravity saves text blocks above about 4 KB to a file and shows its path. Each spilled page fits in one native `read_file` call at the default size. Pinboard recognizes the host by the client name it sends when it connects and keeps one page size per recognized host across versions in `~/.config/pinboard/hosts.config`.

```ini
[host "claude-code"]
	startup-page-bytes = 32000
[host "codex"]
	startup-page-bytes = 20000
[host "antigravity"]
	startup-page-bytes = 92000
```

The first page read from a recognized host creates `~/.config/pinboard` (mode `0700`) and the file (mode `0600`) when they are missing, writes that host's measured default as one key, and reads it back, the same way Pinboard seeds contributor-trace settings. If `~/.config/pinboard` or `hosts.config` is a symbolic link, for example from a dotfile manager, Pinboard reads and writes the file it points to and leaves the link in place. That per-user write is the only one `pinboard_startup_text` makes; it never writes your project or board. After that the value is yours; Pinboard never rewrites it. Any other MCP client gets 8000-byte pages and no setting.

| Host | Client name | Seeded default | Measured on | Host limit the default assumes |
| --- | --- | --- | --- | --- |
| Claude Code | `claude-code` | 32000 | Claude Code 2.1.293 | `MAX_MCP_OUTPUT_TOKENS` at its default of 25000 tokens; larger results are saved to a file |
| Codex | `codex-mcp-client` | 20000 | Codex 0.164.0 | `tool_output_token_limit` at its default of 10000 tokens; larger results lose their middle |
| Antigravity | `antigravity-client` | 92000 | agy 1.4.8 | Inline text results up to about 4 KB, which agy offers no setting for; larger results are saved to a file, and `read_file` reads 46,080 bytes per call, so each page spills into one file read in one call |

If you lowered a host's output limit, lower its page size to match; if you raised the limit, you can raise the page size. Values are whole numbers of bytes from 4000 to 200000, and a change applies to the next page. For example:

```sh
git config --file ~/.config/pinboard/hosts.config host.codex.startup-page-bytes 12000
```

Remove a key to have Pinboard write the measured default again. A malformed or out-of-range value, an unreadable file, or a failed first write stops page reads with a diagnostic that names the file and key and gives one next step; Pinboard does not fall back to another size. Antigravity sends the same client version for every agy release, so if a new agy release changes how much it shows inline or how much `read_file` returns per call, adjust the `antigravity` value yourself. A stored `antigravity` value between about 8400 and 92000, such as 16000, still works but makes every page spill into a smaller file, so reading the evidence takes more calls; remove the key to have Pinboard write the measured default.

## Upgrade an installed plugin

Refresh the installed code through the client's supported plugin update or reload mechanism. Codex loads a cached copy; marketplace refresh can replace files for configured plugins, including disabled ones. See [Codex plugin refresh](https://developers.openai.com/plugins/build/plugins#enable-or-disable-a-plugin-for-a-repo). In Claude Code, use `claude plugin update pinboard@pinboard`. In Antigravity, [reinstall from the updated clone](#upgrade-or-remove).

After updating, follow [runtime preparation](#the-launcher-says-runtime-preparation-is-required) and check whether you need to [reconnect](#reconnect-when-pinboard-stays-unavailable). A missing working directory by itself gives no cause for its removal.

Updating plugin code does not migrate project data. Update every client sharing an upgraded board before accessing it. For a separately authorized project migration, stop every Pinboard process using that ledger before preview or apply. Use the same exact selected project and work roots for both commands:

```sh
<launcher-root>/scripts/pinboard --project-root <managed-project> --work-root <selected-work-root> migrate-schema --json
<launcher-root>/scripts/pinboard --project-root <managed-project> --work-root <selected-work-root> migrate-schema --apply <plan-id> --json
```

Preview returns a read-only plan and ID. Apply retains the exact predecessor backup before the atomic schema change. Request a fresh preview after each step. Preserve the plan and backup and inspect reported effects before resuming an interrupted apply with the same ID. If the schema change commits with a view warning, run view repair. An apply refusal names the supported schema-recovery operation and exact selected plan. Preserve those inputs for predecessor advancement and unchanged-state reversal.

## Direct CLI use from a source checkout

Humans who want to run Pinboard directly use a known `<pinboard-source>` checkout as `<launcher-root>` rather than locating an application's installed plugin cache or creating a global alias. Prepare `<pinboard-source>/.venv` once, then invoke the same launcher boundary and name `<managed-project>` explicitly:

```sh
cd /path/to/pinboard
scripts/prepare-worktree
/path/to/pinboard/scripts/pinboard --project-root /path/to/managed-project status --json
```

Direct CLI use covers root discovery, initialization, schema migration, summary status, validation, generated-view repair, portable human export, direct human closure, and `tool-contract` diagnostics. Agents use the connected MCP tools for their workflows. See [reconnecting](#reconnect-when-pinboard-stays-unavailable) if those tools are unavailable.

## After setup

After the first successful setup, Pinboard may point to the optional Repository Readiness, Slop Cleanup, and Maintaining Agent Guidance skills. It does not run them, create work, or change configuration.

After a successful first setup, Pinboard may suggest `model_auto_compact_token_limit_scope` for long Codex tasks if the setting is absent from your user config. You can set it in `~/.codex/config.toml` or its equivalent under `CODEX_HOME`. A trusted project's `.codex/config.toml` can override the user default. Pinboard leaves both files untouched. See the [setup command](src/pinboard/cli/work_state_commands.py) for when it offers the suggestion.

## Troubleshooting

### Codex still asks before a Pinboard tool

Check the installed plugin/server identity and its effective `default_tools_approval_mode`, then any explicit per-tool `approval_mode`. A list of pre-approved tools may omit the requested name. See [MCP tool approvals](#optionally-pre-approve-pinboard-tools) and [close confirmation](#close-confirmation). For a shell command, Git action, or denied filesystem location, use the corresponding controls below.

### Setup commands ask for cache or network access

Source development runs `scripts/prepare-worktree`, which installs locked dependencies and may access uv/npm caches and download packages. For installed plugins, see [runtime preparation](#the-launcher-says-runtime-preparation-is-required).

Inspect the failed command and the exact path or host before approving access. For a diagnosed Codex access restriction during authorized source setup, see the one exact-command retry in [contributor setup](CONTRIBUTING.md#prepare-the-pinboard-source-development-environment). In Claude, distinguish Bash approval from a path or network denial and grant only the needed access under [its sandbox controls](https://code.claude.com/docs/en/sandboxing). For a Git prompt, follow [Git actions you request](#git-actions-you-request).

### Pinboard cannot write its project data

For a denied CLI database write, `SQLITE_READONLY` names the affected path, operation, any changes already made, and the access needed. MCP results give their own retry instructions. See [primary-checkout permissions](#allow-routine-project-data-writes) or [linked worktrees and custom locations](#linked-worktrees-and-custom-data-locations) before granting access. If the result reports a published artifact or migration change, preserve it and check the current state before retrying. See [invocation diagnostics](CONTRIBUTING.md#diagnose-pinboard-invocations-during-contributor-work) for recovery codes.

### Claude Code reports the pinboard MCP server as failed

`Connection closed` reports a failed connection. Ask Claude Code why Pinboard is unavailable and inspect the launcher diagnostics. Follow [runtime preparation](#the-launcher-says-runtime-preparation-is-required) and [reconnecting](#reconnect-when-pinboard-stays-unavailable).

### Claude Code asks for Bash or file access

Identify the requested tool first. Use [Claude permissions](#permissions) to check `acceptEdits`, the exact checkout and work-root directories, and any explicit ask or deny rule. `/permissions` shows the active rules. For a shell sandbox denial, use `/sandbox` and its diagnostics to identify the path or host before granting access.

### Claude Code still asks before each Pinboard tool

Check that the plugin's [runtime is ready](#the-launcher-says-runtime-preparation-is-required), the plugin is enabled, and `disableAllHooks` is unset. `/hooks` should list the Pinboard PreToolUse hook for `mcp__plugin_pinboard_pinboard__.*`. See [close confirmation](#close-confirmation) for that prompt. For other prompts, check the settings sources for an ask or deny rule and keep any rule you intended.

If Claude Code reports a hook error, use the [launcher for your Claude Code setup](#the-launcher-says-runtime-preparation-is-required) with `--claude-pre-tool-use` on one PreToolUse event to see its stderr diagnostic. For approval when the hook is unavailable, see [Claude permissions](#permissions).

### Antigravity asks before or denies each Pinboard tool

Check the [Pinboard allow rule](#allow-pinboard-tools). `agy plugin list --json` should show the plugin enabled with its hooks. See [close confirmation](#close-confirmation) for that prompt.

### Antigravity reports the pinboard MCP server as unavailable

Check the session message for the launcher diagnostic, then follow [runtime preparation](#the-launcher-says-runtime-preparation-is-required) and [reconnecting](#reconnect-when-pinboard-stays-unavailable).

### Pinboard reports a host page-size setting as unavailable

If you receive `HOST_PAGE_SETTING_UNAVAILABLE`, follow the next step in the result for the named file or resource and key. Pinboard checks every entry in the shared `hosts.config`, so the key may belong to a different host. Retry the page read once the reported issue is resolved. See [page-size settings](#startup-page-size-per-host) for allowed values and defaults.

### The launcher says runtime preparation is required

Each installed version or one-session checkout needs its own runtime. A [prepared source checkout](#direct-cli-use-from-a-source-checkout) can use its `.venv`. Otherwise MCP startup tries to prepare the private runtime automatically before starting the server. The Claude Code and Antigravity SessionStart hooks can also prepare it. Both routes use the same launcher-local lock and verify the CLI, MCP, and hook executables before marking the runtime ready. Preparation also repairs a runtime with a missing entry point. Ordinary commands using a ready private runtime leave the plugin tree untouched and need neither uv nor access to its cache.

To prepare it yourself before the first session, when uv is missing from the host's PATH, or when the launcher asks, run the launcher for the plugin you are loading with uv available:

```sh
<launcher-root>/scripts/pinboard --prepare-runtime
```

For a [persistent Claude Code installation](#persistent-installation), use the exact `installPath` from `claude plugin list --json`. For a [one-session load](#one-session-without-installation), use the checkout you selected with `--plugin-dir`. In Antigravity, use `~/.gemini/config/plugins/pinboard` as `<launcher-root>`. Grant write access only to that launcher root's `.pinboard-runtime`.

An unprepared ordinary CLI call reports the recovery action on stdout before running Pinboard. An MCP startup failure reports it on stderr and keeps protocol stdout empty. If the Claude Code or Antigravity SessionStart hook fails to prepare, its session message gives the cause and manual command. The launcher's `pinboard-launcher-result/v1` reports whether anything may have changed. Keep the diagnostic and follow the stated next step, such as waiting for a running preparation or fixing the named uv or write-access issue. See the [launcher contract](ARCHITECTURE.md#launcher) for the entry points and startup diagnostics.

### Reconnect when Pinboard stays unavailable

After preparation, check that Pinboard's tools are available, even if SessionStart reports that the runtime is ready. If tools are still unavailable, make sure the plugin directory still exists and reconnect.

- **Codex** uses the client's supported MCP reconnect or restart mechanism.
- **Claude Code** uses `/mcp` to reconnect, or you can restart Claude Code. If you restart a [one-session load](#one-session-without-installation), launch it from the project directory with the same `--plugin-dir` argument.
- **Antigravity** reconnects when you start a new `agy` session.

### Close confirmation

Closing a saved item through `pinboard_close` requires your explicit decision in your own words. Give those words to the agent before it calls the tool. Whether the host also prompts depends on its approval controls:

- **Codex** uses your [MCP approval settings](#optionally-pre-approve-pinboard-tools).
- **Claude Code v2.1.199 or later** asks for each close even with an allow rule or Pinboard's approval hook. In `dontAsk` mode and non-interactive runs such as `claude -p`, it denies the close. Earlier Claude Code versions have no extra close prompt.
- **Antigravity** asks through Pinboard's PreToolUse hook even with the [server allow rule](#allow-pinboard-tools). Use an interactive session to close a saved item because headless `agy -p` denies the close. When the hook does not run, your Antigravity approval settings decide for every tool.

For workflow and recovery behavior beyond installation, see [How Pinboard works](HOW_IT_WORKS.md). For system boundaries and current operating assumptions, see [Architecture](ARCHITECTURE.md).
