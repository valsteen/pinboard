# Install Pinboard

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration; Claude Code can load the same local plugin experimentally.

## Launcher and environment paths

Pinboard uses these terms consistently:

- `<launcher-root>` is the directory that contains `scripts/pinboard`. Direct CLI commands invoke this launcher; the plugin's MCP declaration invokes the same launcher with `--mcp`.
- `<launcher-root>/.pinboard-runtime/environment` is the installed private runtime. One explicit `--prepare-runtime` command uses uv to create it for an installed plugin version; ordinary installed commands use neither uv nor its cache.
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

An installed version needs deliberate runtime preparation before its first connection. If startup reports a preparation requirement, discover `<launcher-root>` relative to the active Pinboard skill and run `<launcher-root>/scripts/pinboard --prepare-runtime` once with uv available. Request write access only to that version's `.pinboard-runtime` when needed. Then reconnect through the client's supported MCP reconnect mechanism or start a new task/session that reloads the plugin. Startup itself never prepares or changes the plugin. Later connections and CLI commands use the prepared runtime without uv or its cache.

### Allow routine project-data writes

For normal commands from a primary checkout, create a named [Codex permission profile](https://learn.chatgpt.com/docs/permissions) that grants write access only to Pinboard's repository-local data:

```toml
default_permissions = "pinboard"

[permissions.pinboard]
extends = ":workspace"

[permissions.pinboard.filesystem.":workspace_roots"]
".codex/pinboard" = "write"
```

Remove legacy `sandbox_mode` and `sandbox_workspace_write` settings before relying on this profile because they override permission profiles.

### First setup

The first default initialization adds only `/.codex/pinboard/` to the repository's local `.git/info/exclude`, then creates the Pinboard data directory. Approve that exact initialization once; it needs the narrow Git-metadata write only for this setup. Pinboard does not edit `.gitignore`, and sibling `.codex` paths remain visible to Git.

Linked worktrees share the same repository-local exclusion, so repeating setup remains idempotent. If setup stops after making a durable change, Pinboard reports which surface changed so the coding agent can inspect it before retrying.

### Linked worktrees and custom data locations

A linked worktree uses the shared repository's Pinboard data, which is outside the linked checkout. Run `<launcher-root>/scripts/pinboard root` to obtain the exact resolved location, then add a direct write rule for only that absolute `.codex/pinboard` directory under `[permissions.pinboard.filesystem]`.

Do not add the shared repository as a workspace root. Do not grant access to its `.git` directory, sibling `.codex` paths, or the installed plugin cache.

If you deliberately use `--work-root`, add a direct write rule for only that exact selected directory.

## Claude Code

Claude Code support is experimental. Clone this repository, then choose a persistent local installation or a one-session load.

### Persistent local installation

```sh
git clone https://github.com/valsteen/pinboard.git
claude plugin marketplace add /path/to/pinboard
claude plugin install pinboard@pinboard
```

Open the target project and ask Claude Code to set up Pinboard there.

The Claude manifest selects the separate root `mcp-claude.json`. Claude expands `${CLAUDE_PLUGIN_ROOT}` in its command and invokes that root's `scripts/pinboard --mcp`, independent of the target project's current directory. Prepare an installed version deliberately as described above, then use Claude Code's supported reconnect or reload mechanism. A one-session source load uses the prepared source `.venv` when present.

Claude's manual permission mode asks before each MCP tool call by default. To approve Pinboard once for autonomous workflows, merge this server-scoped rule into your user-level `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "mcp__plugin_pinboard_pinboard__*"
    ]
  }
}
```

This rule covers only tools from the installed Pinboard plugin server. It does not approve shell commands, repository writes outside Claude's existing file permissions, or another MCP server, and it does not bypass Pinboard's receipts and leases. Omit it if you prefer to approve every Pinboard call separately.

This route uses Claude Code's marketplace mechanism with your local checkout. Pinboard is not published in or installed from Anthropic's official marketplace, and it does not claim live sharing between Codex and Claude Code.

### One session without installation

```sh
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard
```

The free Claude chat plan and Claude Code access are separate product surfaces. Check [Anthropic's current authentication options](https://code.claude.com/docs/en/authentication) before an authenticated smoke test because access can change.

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

For a normal primary checkout, grant only relative `.codex/pinboard`. For a linked worktree or explicit data location, grant only the exact absolute location resolved by Pinboard. If an immutable artifact was already published, preserve it and inspect current state and artifact identity before selecting supported recovery; do not blindly replay the operation.

### The launcher says runtime preparation is required

An unprepared installed plugin exits before Pinboard starts and returns `pinboard-launcher-result/v1` with the exact same-launcher `--prepare-runtime` action. CLI recovery is on stdout; MCP prestart recovery is on stderr so protocol stdout stays empty. Run that action once with uv available and grant write access only to `<launcher-root>/.pinboard-runtime`. If preparation fails, keep the reported upstream diagnostics and follow its stated retry requirement; do not substitute `<managed-project>/.venv`, an import-path change, or an ad hoc uv command.

Preparation verifies the CLI entry and availability of the MCP executable before writing the ready marker. After it succeeds, reconnect the client; `scripts/pinboard --mcp` selects `pinboard-mcp`, while ordinary CLI arguments select `pinboard`. MCP startup accepts no additional arguments: roots belong to tool requests. Ordinary installed use does not invoke uv, read its cache, or mutate the prepared plugin tree. A source checkout prepared by `<pinboard-source>/scripts/prepare-worktree` continues to use `<pinboard-source>/.venv`.

For workflow and recovery behavior beyond installation, see [How Pinboard works](HOW_IT_WORKS.md). For system boundaries and current operating assumptions, see [Architecture](ARCHITECTURE.md).
