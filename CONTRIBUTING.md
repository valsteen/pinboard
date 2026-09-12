# Contributing to Pinboard

This guide covers the supported development environment, repository checks, tests, and packaging. [Architecture](ARCHITECTURE.md) owns system boundaries and module responsibilities; [AGENTS.md](AGENTS.md) owns imperative guidance for coding agents working in this repository.

## Prepare the development environment

The repository pins Python 3.14.7 and uv 0.12.10. uv manages Python installation, the project environment, Python dependencies, the checked-in lockfile, builds, and Python command execution. msgspec provides immutable records and strict JSON decoding at repository boundaries.

After cloning the repository or creating a new implementation worktree, prepare both locked development environments:

```sh
scripts/prepare-worktree
```

The command runs `uv sync --locked` and `npm ci --prefer-offline --no-audit --no-fund` from the selected checkout. Each checkout keeps its own ignored `.venv/` and `node_modules/` directories while uv and npm may reuse their package caches. The checked-in locks make setup repeatable. jscpd 5.1.2 is the sole non-Python development dependency; it requires Node.js 18 or newer and npm, but no global installation.

## Run the checks

Use the installed package from the locked uv environment for every Python check:

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

The repository uses `unittest`. The metadata validator is the supported check for plugin and skill discovery consistency. `HOW_IT_WORKS.md` and its diagrams are generated from `docs/how_it_works/`; update their sources and rerun the renderer instead of editing the generated guide directly.

Run the project-local duplication checks after changing production Python:

```sh
npm run duplication
npm run duplication:ci
```

The local scan uses four-line and 40-token limits. It is intentionally sensitive: matches are prompts for judgment, not defects to eliminate mechanically. The CI profile uses eight-line and 60-token limits, rejects clones that are new relative to `origin/main`, and enforces a 0.3% ceiling; the current accepted scan reports 0.2%. Lower the ceiling after accepted cleanup reduces the baseline; do not raise it merely to admit new duplication.

Every pull request and main-branch update runs the supported checks on macOS and Linux, including plugin and skill validation.

## Build and inspect the package

Build without workspace sources, then exercise the repository launcher:

```sh
uv build --no-sources
scripts/pinboard --help
```

The build produces the installed application and plugin command. Pinboard is not a supported Python library, so internal modules and package exports are not public extension APIs.

## Match verification to the change

Run the checks that own the surfaces you changed. In particular, run the metadata validator after plugin or skill changes, and run the duplication scan after production Python changes. Do not add a second Python dependency manager, test runner, formatter, linter, type checker, or global npm dependency without an accepted repository decision.
