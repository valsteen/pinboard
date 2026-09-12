# Install Pinboard

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration; Claude Code can load the same local plugin experimentally. Both routes use uv to provide the plugin's Python 3.14 runtime.

## Codex

Add the GitHub repository as a Codex marketplace, then install Pinboard:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository where you want to use Pinboard and ask:

> Set up Pinboard here and explain how I can use it from one task or several tasks.

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

A linked worktree uses the shared repository's Pinboard data, which is outside the linked checkout. Run `pinboard root` to obtain the exact resolved location, then add a direct write rule for only that absolute `.codex/pinboard` directory under `[permissions.pinboard.filesystem]`.

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

This route uses Claude Code's marketplace mechanism with your local checkout. Pinboard is not published in or installed from Anthropic's official marketplace, and it does not claim live sharing between Codex and Claude Code.

### One session without installation

```sh
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard
```

The free Claude chat plan and Claude Code access are separate product surfaces. Check [Anthropic's current authentication options](https://code.claude.com/docs/en/authentication) before an authenticated smoke test because access can change.

## After setup

After the first successful setup, Pinboard may point to the optional Repository Readiness, Slop Cleanup, and Maintaining Agent Guidance skills. It does not run them, create work, or change configuration.

Pinboard may also recommend the `model_auto_compact_token_limit_scope` setting for long Codex tasks when that user setting is absent. It reads `~/.codex/config.toml`, or the equivalent under `CODEX_HOME`, but never edits user or project Codex configuration. A trusted project's `.codex/config.toml` can override the user default. Returning to an existing Pinboard, a failed setup, or an unreadable or malformed configuration produces no recommendation.

## Troubleshooting

### Pinboard cannot write its project data

When a routine Codex operation lacks the required permission, Pinboard reports `SQLITE_READONLY`, the affected location and operation, whether anything changed, and the exact recovery path.

For a normal primary checkout, grant only relative `.codex/pinboard`. For a linked worktree or explicit data location, grant only the exact absolute location reported by Pinboard. If the failure says an immutable artifact was already published, inspect current state before retrying rather than replaying the operation.

### The installed command should not need environment workarounds

The installed plugin launcher uses its prepared environment. A source checkout uses the repository's locked development environment. Ordinary installed use should not require a custom uv cache directory, an ambient Python interpreter, or import-path changes.

For workflow and recovery behavior beyond installation, see [How Pinboard works](HOW_IT_WORKS.md). For system boundaries and current operating assumptions, see [Architecture](ARCHITECTURE.md).
