# Pinboard

## Keep your coding agent building the product you meant

<img align="right" width="430" src="assets/pinboard-investigation-office.png" alt="A 1970s office worker explaining a wall-sized investigation board covered with a map, notes, portraits, diagrams, colored markers, and connecting thread">

Working with a coding agent can stay fluid: follow an idea, ask for the change, and keep moving. You do not need to maintain a parallel version of the project in a ticket system. Pinboard keeps that rhythm while recording ideas, decisions, work, and reviews beside the repository.

Your coding agent still does the work. Pinboard adds guardrails that let the flow continue without losing the decisions behind it.

<br clear="right">
<br>

[![CI](https://github.com/valsteen/pinboard/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/pinboard/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## When the work outlives the conversation

That freedom becomes harder to steer when the work outlives the conversation. Ideas disappear before they become work. Accepted decisions stay in the plan after they become obsolete. Architectural assumptions survive after the project invalidates them. Changes accumulate until no one can trace why they belong.

The drift is co-authored. Sometimes your former self made the forgotten choice. Sometimes the agent filled a gap, and plausible language made the guess look intentional. As notes, code, and reviews reinforce one another, both of you can mistake momentum for direction.

## What changes when you use Pinboard

- **An idea can stay an idea.** A useful discovery is preserved without quietly joining the current feature.
- **“Yes” has an exact meaning.** Work starts from what you accepted, not from whichever suggestion appeared most recently.
- **An interruption does not rewrite the task.** Another session can recover the decision, current work, and evidence without reconstructing them from chat.
- **Review checks the direction as well as the implementation.** The reviewer sees the accepted request beside the exact change, so a clean, tested change can still be rejected when it builds the wrong thing.

## Pinboard makes the first delivery slower

Pinboard adds work before implementation: preserving discoveries, agreeing on scope, and carrying evidence into review. For a small or disposable task, using the coding agent directly is often the better choice.

Its return appears when work outlives the current conversation: another revision, interruption, reviewer, or task can continue from the decisions behind the work instead of reconstructing them.

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

Pinboard may activate a specialized skill automatically when the work calls for it. You can also invoke one directly:

- **Technical Writing** — `$technical-writing` or `/pinboard:technical-writing` — Shape substantial technical documents and human-facing project artifacts.
- **Repository Readiness** — `$repository-readiness` or `/pinboard:repository-readiness` — Map an unfamiliar repository before making reliable changes.
- **Slop Cleanup** — `$slop-cleanup` or `/pinboard:slop-cleanup` — Remove abandoned code and the residue it leaves behind.
- **Maintaining Agent Guidance** — `$maintaining-agent-guidance` or `/pinboard:maintaining-agent-guidance` — Put durable instructions at the owner that can keep them true.

These skills specialize part of the work. They are not alternate ways to start or manage Pinboard.

## Install from GitHub

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration.

### Codex

Add this repository as a marketplace, then install Pinboard:

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

By default, Pinboard keeps project decisions and evidence in ignored files inside the repository. You can choose another location on your machine. The installed plugin contains Pinboard's code and skills, not your project data.

## Learn more

- [How Pinboard works](HOW_IT_WORKS.md) follows the workflow from an idea to an accepted, reviewed change.
- [Install Pinboard](INSTALL.md) covers advanced setup, permissions, linked worktrees, Claude Code support, and troubleshooting.
- [Contributing](CONTRIBUTING.md) covers the development environment, checks, tests, and packaging.
- [Architecture](ARCHITECTURE.md) describes system ownership, boundaries, limitations, and failure semantics.
