# Install Pinboard

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration; Claude Code can load the same local plugin experimentally.

## Launcher and environment paths

Pinboard uses these terms consistently:

- `<launcher-root>` is the directory that contains `scripts/pinboard`. Every human and agent Pinboard command invokes `<launcher-root>/scripts/pinboard`.
- `<launcher-root>/.pinboard-runtime/environment` is the installed private runtime. One explicit `--prepare-runtime` command uses uv to create it for an installed plugin version; ordinary installed commands use neither uv nor its cache.
- `<pinboard-source>/.venv` is the development environment for a prepared Pinboard source checkout. Repository-owned `uv sync` and `uv run` commands create or use this environment only for Pinboard development; `uv build` may use uv's isolated build environment.
- `<managed-project>` is the repository whose work Pinboard coordinates. Callers select it with `--project-root <managed-project>`; Pinboard never uses its Python environment, `.venv`, or dependency files.

The launcher root may be a prepared Pinboard source checkout or an installed plugin version. A prepared source launcher uses `<pinboard-source>/.venv`; an installed launcher uses its private runtime. That runtime choice follows the resolved launcher root, not whether the caller is a human or an agent.

## Codex

Add the GitHub repository as a Codex marketplace, then install Pinboard:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository where you want to use Pinboard and ask:

> Set up Pinboard here and explain how I can use it from one task or several tasks.

The coding agent discovers `<launcher-root>` relative to the active Pinboard skill and invokes `<launcher-root>/scripts/pinboard`. On first use, an unprepared installed launcher returns one machine-readable preparation action. The agent runs that same launcher with `--prepare-runtime`, requesting write access only to the installed plugin version when needed. Later commands use `<launcher-root>/.pinboard-runtime/environment` without uv or its cache.

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

This route uses Claude Code's marketplace mechanism with your local checkout. Pinboard is not published in or installed from Anthropic's official marketplace, and it does not claim live sharing between Codex and Claude Code.

### One session without installation

```sh
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard
```

The free Claude chat plan and Claude Code access are separate product surfaces. Check [Anthropic's current authentication options](https://code.claude.com/docs/en/authentication) before an authenticated smoke test because access can change.

### Permission prompts

The plugin ships a `PreToolUse` hook (`hooks/hooks.json`, `scripts/permission-hook.sh`) that auto-allows Bash invocations of the installed `scripts/pinboard` launcher when the subcommand is confirmed read-only against the CLI parser: `overview`, `status`, `root`, `validate`, `actions`, `input-contract`, `tool-contract`, `attempt status`, `attempt inspect`, `preparation status`, `item status`, `item definition`, `item definition-history`, `parallel preview`, `brief review-status`, `artifact verify`, and `brief-sources` without `--output-plan`. The hook only recognizes a single, unadorned invocation of that exact launcher path — any shell chaining, substitution, or redirection in the command falls through to the normal permission flow, as does every command that mutates state (`transition`, `proposal`, `dispatch`, `close`, `order`, `item revise`, lease acquisition, and so on). Those still prompt for approval by design.

## Direct CLI use from a source checkout

Humans who want to run Pinboard directly use a known `<pinboard-source>` checkout as `<launcher-root>` rather than locating an application's installed plugin cache or creating a global alias. Prepare `<pinboard-source>/.venv` once, then invoke the same launcher boundary and name `<managed-project>` explicitly:

```sh
cd /path/to/pinboard
scripts/prepare-worktree
/path/to/pinboard/scripts/pinboard --project-root /path/to/managed-project status --json
```

The source-development environment at `<pinboard-source>/.venv` contains Pinboard development dependencies. It is distinct from an installed plugin's `<launcher-root>/.pinboard-runtime/environment` and from any environment or dependency files under `<managed-project>`.

## After setup

After the first successful setup, Pinboard may point to the optional Repository Readiness, Slop Cleanup, and Maintaining Agent Guidance skills. It does not run them, create work, or change configuration.

Pinboard may also recommend the `model_auto_compact_token_limit_scope` setting for long Codex tasks when that user setting is absent. It reads `~/.codex/config.toml`, or the equivalent under `CODEX_HOME`, but never edits user or project Codex configuration. A trusted project's `.codex/config.toml` can override the user default. Returning to an existing Pinboard, a failed setup, or an unreadable or malformed configuration produces no recommendation.

## Troubleshooting

### Pinboard cannot write its project data

When a routine Codex operation lacks the required permission, Pinboard reports `SQLITE_READONLY`, the affected location and operation, whether anything changed, and the exact recovery path.

For a normal primary checkout, grant only relative `.codex/pinboard`. For a linked worktree or explicit data location, grant only the exact absolute location reported by Pinboard. If the failure says an immutable artifact was already published, inspect current state before retrying rather than replaying the operation.

### The launcher says runtime preparation is required

An unprepared installed plugin exits before Pinboard starts and returns `pinboard-launcher-result/v1` with the exact same-launcher `--prepare-runtime` action. Run that action once with uv available and grant write access only to `<launcher-root>/.pinboard-runtime`. If preparation fails, keep the reported upstream diagnostics and follow its stated retry requirement; do not substitute `<managed-project>/.venv`, an import-path change, or an ad hoc uv command.

After preparation succeeds, `<launcher-root>/scripts/pinboard` uses the verified private entry point in `<launcher-root>/.pinboard-runtime/environment`. Ordinary installed use does not invoke uv, read its cache, or mutate the prepared plugin tree. A source checkout prepared by `<pinboard-source>/scripts/prepare-worktree` continues to use `<pinboard-source>/.venv`.

For workflow and recovery behavior beyond installation, see [How Pinboard works](HOW_IT_WORKS.md). For system boundaries and current operating assumptions, see [Architecture](ARCHITECTURE.md).
