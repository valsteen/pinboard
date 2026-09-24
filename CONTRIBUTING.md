# Contributing to Pinboard

This guide covers the supported development environment, repository checks, tests, and packaging. [Architecture](ARCHITECTURE.md) owns system boundaries and module responsibilities; [AGENTS.md](AGENTS.md) owns imperative guidance for coding agents working in this repository.

## Prepare the Pinboard source-development environment

The repository pins Python 3.14.7 and uv 0.12.10. Every uv command in this guide is a Pinboard source-development operation run from `<pinboard-source>`. `uv sync` and `uv run` create or use `<pinboard-source>/.venv`; `uv build` may use uv's isolated build environment. None of these commands prepares Pinboard's installed private runtime at `<launcher-root>/.pinboard-runtime/environment` or uses an environment or dependency file from a `<managed-project>` repository. msgspec provides immutable records and strict JSON decoding at repository boundaries.

After cloning the repository or creating a new implementation worktree, prepare both locked development environments:

```sh
scripts/prepare-worktree
```

The command runs `uv sync --locked` and `npm ci --prefer-offline --no-audit --no-fund` from the selected `<pinboard-source>` checkout. Each checkout keeps its own ignored `.venv/` and `node_modules/` directories while uv and npm may reuse their package caches. The checked-in locks make setup repeatable. jscpd 5.1.2 is the sole non-Python development dependency; it requires Node.js 18 or newer and npm, but no global installation.

## Run the checks

Use the Pinboard package installed in `<pinboard-source>/.venv` for every Python check:

```sh
uv run --locked python -m docs.how_it_works.render --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pyrefly check
uv run --locked pyrefly coverage check src --strict --fail-under 100
uv run --locked coverage run -m unittest discover -v
uv run --locked coverage report
uv run --locked python scripts/validate-metadata.py
```

The pyrefly configuration deliberately ignores Git ignore files and pyrefly's default exclusion heuristics so that linked worktrees under `.claude/worktrees/` check the same files as the main checkout; a `No Python files matched patterns` result from `pyrefly check` is a misconfiguration, not a pass.

The repository uses `unittest`. The metadata validator is the supported check for plugin and skill discovery consistency. `HOW_IT_WORKS.md` and its diagrams are generated from `docs/how_it_works/`; update their sources and rerun the renderer instead of editing the generated guide directly.

## Diagnose Pinboard invocations during contributor work

An initialized project gets an ignored `<managed-project>/.pinboard/contributor-traces.config` file on its first normal Pinboard CLI or MCP invocation, including a CLI call that needs runtime preparation. Its explicit default is:

```gitconfig
[pinboard "unsafe_persist_exact_pinboard_traces"]
    mode = off
```

Change the project `mode` to `on` to capture normal supported Pinboard CLI and MCP calls without adding capture arguments to each call. Add `[item "my-item"]` with `mode = on` or `mode = off` to override that project value; `mode = inherit` follows it. A call without an identifiable item uses the project value. The file belongs to the primary repository's ignored `.pinboard` directory, so linked worktrees and Codex tasks share it. Edit either value while work continues; the next supported invocation reads it again. Restore project `mode = off` or remove an item override to stop future automatic capture. Invalid settings or a trace destination that cannot be used privately reject before the target call.

The name is deliberately blunt: **on means exact values may be persisted with secrets.** Pinboard does not detect or redact them. Set it only when the expected arguments and output are safe to keep on this computer. Automatic files live in `.pinboard/invocation-traces/` with directory mode `0700` and file mode `0600`; Pinboard retains the newest 100 automatic files and removes older automatic files. Check and delete sensitive traces yourself when they are no longer needed. The files are local diagnostic evidence, not accepted ledger evidence, and Pinboard records no environment snapshot. CLI files preserve the original invocation argv, stdout, stderr, and exit status; MCP files preserve strict decoded request and result values, while transport bytes and events before the callback remain unavailable. A failure after the target runs never authorizes replay.

The existing one-off `--capture-evidence ... --safe-to-persist-exactly --` CLI form and dedicated MCP capture startup still work. Those manual captures use the caller's selected destination and retention. During ordinary work on an enabled project or item, investigate consequential Pinboard anomalies using the relevant trace selector. Report the attempted outcome, observed behavior, recovery, confidence, available cost, and precise disposition without pasting raw secret-bearing values. Keep routine successful calls quiet. Fix and verify an actionable issue within authorized scope, or confirm an exact admitted item covers it and has saved priority. If neither authority exists, ask one admission or priority question at the task result boundary.

Run the project-local duplication checks after changing production Python:

```sh
npm run duplication
npm run duplication:ci
```

The local scan uses four-line and 40-token limits. It is intentionally sensitive: matches are prompts for judgment, not defects to eliminate mechanically. The CI profile uses eight-line and 60-token limits, rejects clones that are new relative to `origin/main`, and enforces a 0.3% ceiling; the current accepted scan reports 0.2%. Lower the ceiling after accepted cleanup reduces the baseline; do not raise it merely to admit new duplication.

Every pull request and main-branch update runs the supported checks on macOS and Linux, including plugin and skill validation.

## Build and inspect the package

Build without workspace sources, then exercise `<pinboard-source>/scripts/pinboard`, the same launcher boundary used from an installed plugin root:

```sh
uv build --no-sources
scripts/pinboard --help
```

The build produces the installed application and plugin command. Pinboard is not a supported Python library, so internal modules and package exports are not public extension APIs.

## Match verification to the change

Run the checks that own the surfaces you changed. In particular, run the metadata validator after plugin or skill changes, and run the duplication scan after production Python changes. Do not add a second Python dependency manager, test runner, formatter, linter, type checker, or global npm dependency without an accepted repository decision.
