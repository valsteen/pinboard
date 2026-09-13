# Install Pinboard

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration; Claude Code can load the same local plugin experimentally. Each installed plugin version uses uv once, during explicit preparation, to create its own Python 3.14 runtime at `.pinboard-runtime`. Pinboard never uses the managed project's Python environment or dependency files.

## Codex

Add the GitHub repository as a Codex marketplace, then install Pinboard:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository where you want to use Pinboard and ask:

> Set up Pinboard here and explain how I can use it from one task or several tasks.

The coding agent resolves the installed `scripts/pinboard` launcher relative to the active Pinboard skill. On first use, that launcher returns one machine-readable preparation action. The agent runs the same launcher with `--prepare-runtime`, requesting write access only to the installed plugin version when needed. Later commands use the prepared private runtime without uv or its cache.

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

## Direct CLI use from a source checkout

Humans who want to run Pinboard directly use a known source checkout rather than locating an application's installed plugin cache or creating a global alias. Prepare that source checkout's development environment once, then use its launcher and name the managed project explicitly:

```sh
cd /path/to/pinboard
scripts/prepare-worktree
/path/to/pinboard/scripts/pinboard --project-root /path/to/managed-project status --json
```

The source checkout's `.venv` contains Pinboard development dependencies. It is distinct from an installed plugin's `.pinboard-runtime` and from any `.venv` belonging to the managed project.

## After setup

After the first successful setup, Pinboard may point to the optional Repository Readiness, Slop Cleanup, and Maintaining Agent Guidance skills. It does not run them, create work, or change configuration.

Pinboard may also recommend the `model_auto_compact_token_limit_scope` setting for long Codex tasks when that user setting is absent. It reads `~/.codex/config.toml`, or the equivalent under `CODEX_HOME`, but never edits user or project Codex configuration. A trusted project's `.codex/config.toml` can override the user default. Returning to an existing Pinboard, a failed setup, or an unreadable or malformed configuration produces no recommendation.

## Troubleshooting

### Pinboard cannot write its project data

When a routine Codex operation lacks the required permission, Pinboard reports `SQLITE_READONLY`, the affected location and operation, whether anything changed, and the exact recovery path.

For a normal primary checkout, grant only relative `.codex/pinboard`. For a linked worktree or explicit data location, grant only the exact absolute location reported by Pinboard. If the failure says an immutable artifact was already published, inspect current state before retrying rather than replaying the operation.

### The launcher says runtime preparation is required

An unprepared installed plugin exits before Pinboard starts and returns `pinboard-launcher-result/v1` with the exact same-launcher `--prepare-runtime` action. Run that action once with uv available and grant write access only to the launcher root's `.pinboard-runtime`. If preparation fails, keep the reported upstream diagnostics and follow its stated retry requirement; do not substitute the managed project's `.venv`, an import-path change, or an ad hoc uv command.

After preparation succeeds, the same launcher uses the verified private entry point. Ordinary installed use does not invoke uv, read its cache, or mutate the prepared plugin tree. A source checkout prepared by `scripts/prepare-worktree` continues to use its separate locked development `.venv`.

For workflow and recovery behavior beyond installation, see [How Pinboard works](HOW_IT_WORKS.md). For system boundaries and current operating assumptions, see [Architecture](ARCHITECTURE.md).
