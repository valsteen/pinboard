# Pinboard

## Keep your coding agent building the product you meant

<img align="right" width="430" src="assets/pinboard-investigation-office.png" alt="A 1970s office worker explaining a wall-sized investigation board covered with a map, notes, portraits, diagrams, colored markers, and connecting thread">

Long-running agent work can drift into a coherent, well-tested product that no one actually decided to build. Pinboard is a repository-local record of what you proposed, accepted, built, and reviewed, shared across tasks and interruptions.

Your coding agent still does the work. Pinboard keeps that work tied to the decision.

<br clear="right">
<br>

[![CI](https://github.com/valsteen/pinboard/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/pinboard/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Nobody asked for all this

Each addition can make sense on its own. The agent proposes an improvement, review makes it look settled, and you keep moving. Soon every part has a reason, but the whole has no decision behind it.

## The code passed review. Nobody checked the request.

A change can be clean, tested, and carefully reviewed while building something no one asked for. Pinboard gives the reviewer the accepted request alongside the exact change, so review asks both: does it work, and is it what we decided to build?

## What changes when you use Pinboard

- **An idea can stay an idea.** A useful discovery is preserved without quietly joining the current feature.
- **“Yes” has an exact meaning.** Work starts from what you accepted, not from whichever suggestion appeared most recently.
- **An interruption does not rewrite the task.** Another session can recover the decision, current work, and evidence without reconstructing them from chat.

## Pinboard makes the first delivery slower

Pinboard adds work before implementation: preserving discoveries, agreeing on scope, and carrying evidence into review. For a small or disposable task, using the coding agent directly is often the better choice.

Pinboard is meant for work that outlives the current conversation: another revision, interruption, reviewer, or task.

Pinboard's request check came from its own review loop improving work that had never been accepted.

[How Pinboard works](HOW_IT_WORKS.md) follows the complete workflow and the decisions behind it.

## What you decide

You still decide what belongs in the product. You accept or reject proposed work, settle choices that change scope or behavior, and choose what happens to a reviewed change in the repository.

## What Pinboard coordinates

Pinboard keeps those decisions attached to the work. It preserves discoveries without starting them, turns accepted outcomes into stable briefs, carries the same target through implementation and separate review, and restores the relevant context after an interruption. The coding agent operates that workflow and brings material choices back to you in ordinary language.

## Skills

Use `$pinboard` in Codex or `/pinboard:pinboard` in Claude Code. This is the main entry point when you want to preserve work, decide what should happen next, or start an existing item.

You can ask naturally:

> Save this database concern for later without changing the current task.

> What should we work on next?

> Start the accepted API cleanup.

For substantial documents, Pinboard automatically brings in its bundled Technical Writing guidance. Repository Readiness, Slop Cleanup, and Maintaining Agent Guidance are optional repository-care skills; they are useful on their own and are not alternate ways to start Pinboard.

## Install from GitHub

The plugin currently supports macOS and Linux. It uses [uv](https://docs.astral.sh/uv/) to provide its Python 3.14 runtime and installed command.

### Codex

Codex is the primary, stress-tested integration. Add this repository as a marketplace, then install Pinboard:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository and ask:

> Set up Pinboard here and explain how I can use it from one task or several tasks.

### Claude Code

Claude Code support is experimental. Clone this repository, then install it as a local marketplace plugin:

```sh
git clone https://github.com/valsteen/pinboard.git
claude plugin marketplace add /path/to/pinboard
claude plugin install pinboard@pinboard
```

Ask Claude Code to set up Pinboard in the opened project.

<details>
<summary>Try Pinboard for one Claude Code session without installing it</summary>

```sh
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard
```

</details>

The [installation guide](INSTALL.md) covers first setup, Codex permissions, linked worktrees, the experimental Claude Code routes, and troubleshooting.

## Local data

By default, project decisions and evidence stay in ignored repository-local files; an explicit work root can place them elsewhere on your machine. The installed plugin cache contains packaged code and skills, not your project data.

## Learn more

- [How Pinboard works](HOW_IT_WORKS.md) explains the complete workflow and deeper command behavior.
- [Install Pinboard](INSTALL.md) covers advanced setup and troubleshooting.
- [Contributing](CONTRIBUTING.md) covers the development environment, checks, tests, and packaging.
- [Architecture](ARCHITECTURE.md) describes system ownership, boundaries, limitations, and failure semantics.
