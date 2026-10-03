# Pinboard

## Keep your coding agent building the product you meant

<img align="right" width="430" src="assets/pinboard-investigation-office.png" alt="A 1970s office worker explaining a wall-sized investigation board covered with a map, notes, portraits, diagrams, colored markers, and connecting thread">

Working with a coding agent can stay fluid: follow an idea, ask for the change, and keep moving. But the backlog grows. Ideas lose their context, priorities become unclear, and unfinished work gets harder to pick up.

Pinboard saves work through the conversation you already have with your coding agent. It keeps the goal, your decisions, and progress available when you return later, helping you pick up after an interruption or in a new session.

It works in local Git-backed repositories. When you start saved work, the agent implements the agreed goal and brings material choices back to you. A separate coding agent reviews the proposed change, and you choose what happens to it in the repository.

<br clear="right">
<br>

[![CI](https://github.com/valsteen/pinboard/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/pinboard/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Use it in conversation

Use `$pinboard` in Codex or `/pinboard:pinboard` in Claude Code, then ask naturally:

> I found a database issue. Save it for later and keep working on this.

The idea stays on the board without starting it or interrupting your current work.

> What should we work on next?

The agent compares saved goals, priorities, and dependencies. You can choose what to start or change the order.

> Let's work on the API cleanup next.

The agent carries the agreed goal through implementation and review. Later, you can ask where it left off and pick up from the saved change and evidence.

[Using Pinboard in conversation](GUIDE.md) has examples of reviewing changes, resuming work, trying experiments, and working on several tasks.

## Explore your backlog

You can ask the agent to compare the size and uncertainty of saved work before deciding what to start. Here it compares four queued Pinboard improvements:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/pinboard-work-selection-dark.png">
  <img src="assets/pinboard-work-selection.png" alt="A native Codex conversation comparing four queued Pinboard improvements by relative size and explaining uncertainty">
</picture>

The answer links to the saved tasks. Its size judgments are qualitative; comparing work does not start implementation or change your priority order.

## When Pinboard is worth it

Pinboard is most useful when a decision needs to survive another revision, interruption, reviewer, or task. It helps you and the agent return to why a change belongs, instead of letting forgotten choices or plausible guesses become the product's direction.

**The tradeoff:** recording goals, scope, and evidence makes the first delivery slower. That cost pays back when you revisit the work. For small or disposable changes, using the coding agent directly is often the better choice.

## Install from GitHub

Pinboard supports macOS and Linux. Codex is the primary, stress-tested integration. Have [uv](https://docs.astral.sh/uv/) available for the plugin's first runtime setup.

### Codex

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

### Claude Code

```sh
claude plugin marketplace add valsteen/pinboard
claude plugin install pinboard@pinboard
```

On Claude Code's first session, reconnect the `pinboard` server with `/mcp` or restart once after its runtime preparation finishes. See [Claude Code setup](INSTALL.md#claude-code) for preparation and context settings.

Start a task in your project and ask:

> Set up Pinboard here and explain how I can use it from one task or several tasks.

The [installation guide](INSTALL.md) covers permissions, linked worktrees, trying one Claude Code session without installing, and troubleshooting.

## More skills

Pinboard can use specialized skills when the work calls for them. You can also invoke them directly:

| Skill | Use it to | Codex | Claude Code |
| --- | --- | --- | --- |
| Investigation Focus | Investigate a question and produce an evidence-based result. | `$investigation-focus` | `/pinboard:investigation-focus` |
| Technical Writing | Shape substantial technical documents. | `$technical-writing` | `/pinboard:technical-writing` |
| Repository Readiness | Understand an unfamiliar repository before changing it. | `$repository-readiness` | `/pinboard:repository-readiness` |
| Slop Cleanup | Remove abandoned code and its residue. | `$slop-cleanup` | `/pinboard:slop-cleanup` |
| Maintaining Agent Guidance | Keep durable instructions at the right owner. | `$maintaining-agent-guidance` | `/pinboard:maintaining-agent-guidance` |

For investigation setup and continuity across sessions, see [Use Investigation Focus](INSTALL.md#use-investigation-focus).

## Local data

Project decisions and evidence stay in the repository's ignored `.pinboard` directory, shared by primary and linked worktrees. Pinboard's runtime is separate from your project's Python environment. See [Local data](INSTALL.md#local-data) for locations and migration.

Exact invocation capture is off by default. Its opt-in and handling of potentially sensitive values are described in [contributor diagnostics](CONTRIBUTING.md#diagnose-pinboard-invocations-during-contributor-work).

## Learn more

- [How Pinboard works](HOW_IT_WORKS.md) follows an idea through a reviewed change, your repository decision, and recorded completion.
- [Contributing](CONTRIBUTING.md) covers development, checks, and packaging.
- [Architecture](ARCHITECTURE.md) describes system ownership, boundaries, and limitations.
