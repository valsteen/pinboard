# Pinboard

## Keep your coding agent building the product you meant

<img align="right" width="430" src="assets/pinboard-investigation-office.png" alt="A 1970s office worker explaining a wall-sized investigation board covered with a map, notes, portraits, diagrams, colored markers, and connecting thread">

Working with a coding agent can stay fluid: follow an idea, ask for the change, and keep moving. But the backlog grows. Ideas lose their context, priorities become unclear, and unfinished work gets harder to pick up.

Pinboard helps you save work through the conversation you already have with your coding agent. It keeps the goal and decisions available when you return to them later.

Today, Pinboard supports this work in a local Git-backed repository. Its broader goal is to help people complete suitable agentic work without learning the workflow behind it.

When you ask the agent to start a saved task, it works from the agreed goal and asks you about choices that would change it. A separate coding agent reviews the proposed change. You choose what happens to the reviewed change, and later sessions can pick up from the recorded progress and evidence.

<br clear="right">
<br>

[![CI](https://github.com/valsteen/pinboard/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/pinboard/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What Pinboard does

You decide what belongs in the product. You can save an idea, ask the agent to start it, settle choices that change its scope or behavior, and choose what happens to a reviewed change in the repository.

Pinboard keeps those decisions attached to the work. It:

- preserves ideas without quietly starting them;
- saves your explicit priority order and dependencies;
- turns the agreed goal into a clear brief;
- carries that goal through implementation and review by a separate coding agent; and
- restores the decision, current work, and evidence after an interruption.

The coding agent operates that workflow and brings material choices back to you in ordinary language.

You can save an idea as a work item without starting it. When you ask the agent to begin, it checks whether the work can start. [How Pinboard works](HOW_IT_WORKS.md) follows that request through a reviewed change, your repository decision, and recorded completion.

## Explore your backlog in conversation

The same context that guides implementation also helps your agent reason about what comes next. Ask it to compare tasks, explain dependencies, or identify work that fits your current goals.

Here, the agent uses Pinboard's own backlog to compare the relative size of queued improvements and explain where their scope remains uncertain.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/pinboard-work-selection-dark.png">
  <img src="assets/pinboard-work-selection.png" alt="A native Codex conversation comparing four queued Pinboard improvements by relative size and explaining uncertainty">
</picture>

The answer links to the recorded tasks and makes qualitative judgments, not measured effort estimates. The smallest apparent change differs from the first task in the saved priority order, and existing evidence reuse may already suffice. Comparing work does not start implementation.

When related tasks accumulate, select the live items you want to compare and, if you have one in mind, a proposed owner. Your agent checks their exact current definitions, accounts for each concern, and shows what one outcome, useful checkpoints, or separate items would preserve. It also shows affected dependencies, priority order, and work already underway. You decide on the proposal separately; only then does the agent use Pinboard's existing actions to make the approved changes.

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

## When Pinboard is worth it

Pinboard is most useful when a decision must survive more than the current conversation. That may mean another revision, interruption, reviewer, or task. When work lives that long, ideas can disappear before they become work, accepted decisions can remain after becoming obsolete, architectural assumptions can outlive the evidence that invalidated them, and changes can accumulate until no one can trace why they belong.

The drift is co-authored. Sometimes your former self made the forgotten choice. Sometimes the agent filled a gap, and plausible language made the guess look intentional. As notes, code, and reviews reinforce one another, both of you can mistake momentum for direction.

**The tradeoff:** Pinboard makes the first delivery slower because discoveries, scope, and evidence are recorded before and during implementation. That cost pays back across another revision, interruption, reviewer, or task. For small or disposable work, using the coding agent directly is often the better choice.

[How Pinboard works](HOW_IT_WORKS.md) follows the complete workflow and the decisions behind it.

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

Add this repository as a marketplace, then install Pinboard:

```sh
claude plugin marketplace add valsteen/pinboard
claude plugin install pinboard@pinboard
```

Start Claude Code in your project. The first session prepares the installed version's private runtime, which needs [uv](https://docs.astral.sh/uv/). Because the `pinboard` MCP server starts before that preparation finishes, the first session reports it as failed: reconnect it with `/mcp` or restart Claude Code once, then ask Claude Code to set up Pinboard there. Running `~/.claude/plugins/cache/pinboard/pinboard/*/scripts/pinboard --prepare-runtime` yourself before the first session avoids that one reconnect.

Once prepared, the plugin approves its own MCP tool calls automatically, so Pinboard does not prompt for each tool while your saved deny and ask rules still apply. The [installation guide](INSTALL.md#permissions) explains how to be asked instead and the settings-rule fallback.

<details>
<summary>Try Pinboard for one Claude Code session without installing it</summary>

```sh
git clone https://github.com/valsteen/pinboard.git
/path/to/pinboard/scripts/pinboard --prepare-runtime
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard
```

</details>

The [installation guide](INSTALL.md) covers first setup, Codex and Claude Code permissions, linked worktrees, the Claude Code routes, local data, and troubleshooting.

## Local data

Pinboard keeps project decisions and evidence in the managed repository's ignored `.pinboard` directory, shared by primary and linked worktrees, and never creates or borrows a Python environment in your project. The [Local data](INSTALL.md#local-data) section of the installation guide covers the exact locations, the CLI's role, and migrating an older `.codex/pinboard` layout.

Exact invocation capture is off by default. Contributors can enable private project or item traces for ordinary Pinboard work, or use the existing one-off CLI and dedicated MCP capture forms. Exact values may contain secrets and are never automatically redacted. [The contributor guide](CONTRIBUTING.md#diagnose-pinboard-invocations-during-contributor-work) explains the opt-in, trace location, retention, and diagnosis workflow.

## Learn more

- [Using Pinboard in conversation](GUIDE.md) gives practical requests for saving, starting, reviewing, resuming, and deciding what happens to work.
- [How Pinboard works](HOW_IT_WORKS.md) follows the workflow from an idea to an accepted, reviewed change.
- [Install Pinboard](INSTALL.md) covers advanced setup, Codex and Claude Code permissions, linked worktrees, local data, and troubleshooting.
- [Contributing](CONTRIBUTING.md) covers the development environment, checks, tests, and packaging.
- [Architecture](ARCHITECTURE.md) describes system ownership, boundaries, limitations, and failure semantics.
