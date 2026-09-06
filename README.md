# Pinboard

[![CI](https://github.com/valsteen/pinboard/actions/workflows/ci.yml/badge.svg)](https://github.com/valsteen/pinboard/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20with-uv-DE5FE9?logo=uv)](https://docs.astral.sh/uv/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Keep Codex building the product you meant

<img align="right" width="430" src="assets/pinboard-investigation-board.png" alt="Fantasy adventurer explaining an investigation board covered with maps, clues, portraits, and red thread">

Pinboard is a repository-local work ledger for long-running Codex projects, built first for solo developers whose requirements arrive while they build. It preserves the difference between what you requested, what an agent proposed, what you accepted, what was implemented, and what was reviewed.

<br clear="right">

## The first pass is easy. Drift starts on the second.

A clear prompt can produce an impressive first version. The trouble often starts a few iterations later: implementation uncovers adjacent ideas, review suggests extra hardening, another task resumes with partial context, and useful discoveries begin to look like requirements simply because they are now written down.

Human requests, agent suggestions, technical hypotheses, and accepted product decisions gradually blend together. The code can remain tested and technically plausible while the product grows away from what you meant to build. AI does not create this problem, but it can turn a familiar slow accumulation of product and code slop into a large diff within hours.

## Codex already runs the development loop

Codex can plan, delegate, implement, test, review, use isolated worktrees, and carry a task over time. Pinboard does not replace that harness or make the model more capable. It gives those activities one repository-local ledger of proposals, accepted work, attempts, and evidence shared across tasks and interruptions.

With Pinboard, a discovery can remain a proposal instead of quietly joining the feature. An implementation attempt stays tied to an exact accepted definition and brief. Current ownership is explicit, stale actions are rejected, and review examines one exact candidate with its evidence. In AI-native SDLC terms, Codex performs the work; Pinboard keeps planning, implementation, and review about the same product decision.

Pinboard is Codex-only today. It does not decide the product, choose priorities, or create Codex tasks. It makes the human and agent decisions around those capabilities durable and distinguishable.

## The format is strict. The meaning is still yours.

A Pinboard brief has named places for the outcome, accepted scope, provenance, non-goals, acceptance criteria, reviewed sources, verification, and remaining work. Code enforces the shape, cross-references, identity, and exact artifact bytes. It cannot know whether a sentence filed under `non_goals` truly belongs there or route that sentence to the right file.

The model interprets the words, the human accepts the product decision, and an independent reviewer challenges the result. The structure keeps those distinctions stable. Alongside named reviewed sources and coverage, it lets Codex reason from new prose to the project surfaces that appear to be affected—for example, from a visitor-facing decision to a workflow guide and a durable design principle—without Pinboard encoding an impact graph or requiring every file to be revisited. Human acceptance and review decide whether that connection is real. That is information architecture refined through experience, not semantic enforcement.

## Pinboard makes the first delivery slower

Pinboard usually makes managed work take longer than sending the same request directly to Codex. It spends additional turns preserving discoveries, agreeing on exact scope, acquiring work, rereading the brief before implementation, recording evidence, and reviewing one exact candidate against that brief. That overhead is real.

Pinboard is not designed to win the first-prompt race. It is designed for the fifth, fifteenth, and fiftieth change, when losing or confusing a decision can lead to long sessions reconstructing intent, separating accepted requirements from agent suggestions, removing incidental features, and rebuilding trust in the code. Use Codex directly when that risk costs less than the process.

## A campaign that keeps discoveries out of the feature

Imagine you are building *Ashfall Keep*, a small action RPG. While one Codex task works on the dragon boss's second phase, two useful but distracting discoveries arrive: save games capture temporary animation state, and controller mappings identify abilities by inventory position.

<img width="20" height="20" src="assets/quest-scroll.png" alt="Sealed quest scroll"> **Capture an idea without expanding the feature.** `$pinboard-intake` records the save-game concern with its trigger, evidence, and likely consequence. It enters intake without becoming ready or interrupting the dragon attempt.

> **Saved for later — the animation-state concern is now in `save-game-animation-state` (`intake`); dragon work continues.**

<img align="right" width="390" src="assets/party-crossroads.png" alt="Fantasy adventurer comparing routes toward a riverside village, a dragon keep, and a crystal cave while a scout investigates">

<img width="20" height="20" src="assets/quick-quest-log.png" alt="Open quest ledger and compass"> **Decide from one current project view.** `$pinboard` shows the dragon phase as active, controller mapping as ready, and the save-game concern as intake. Any task can accept, defer, connect, or close work through atomic ledger changes without reconstructing the plan.

<img width="20" height="20" src="assets/safe-camp.png" alt="Campfire and bedroll checkpoint"> **Resume the decision, not the conversation.** If stable ability IDs become a real prerequisite, the dragon attempt records where it stopped and what must change. The same attempt later resumes from its accepted brief and evidence.

<img width="20" height="20" src="assets/ready-to-build.png" alt="Crossed sword and blacksmith hammer"> **Review what was approved, not merely what now exists.** `$pinboard-deliver` is the implementer route for an already prepared active attempt: it follows the exact definition and brief, records the candidate and evidence, and returns them for review by a separate Codex reviewer. If later accepted direction changes the target, the complete definition and brief change too, and the resulting exact candidate receives a new review before wrap-up.

The code, branch, and conversation remain ordinary Codex work. Pinboard keeps their product decisions connected. [How Pinboard works](HOW_IT_WORKS.md) starts with this workflow, then follows it into the detailed lifecycle, persistence model, and package boundaries.

## Pinboard became its own use case

Pinboard is used to build Pinboard. Its product decisions, parallel work, exact briefs, implementation evidence, and reviews move through the same workflow it ships.

Pinboard did not begin as a complete human-authored product specification. The problem and final product judgment were human; Codex proposed much of the solution between them. Keeping those roles distinct made it possible to examine what emerged, accept or reject its shape, and then refine its identity, principles, code, and explanation through the same workflow. Writing [How Pinboard works](HOW_IT_WORKS.md) was part of discovering what had actually been built, not documentation added after the fact.

Its bundled Slop Cleanup skill has also been applied to this repository: tracing residue from revised features, removing test-only production paths, and repeating the scan until it found no new candidates. Repository Readiness's developer-navigation and storytelling lenses were developed here, stress-tested against earlier revisions by fresh Codex reviewers, and then used to reconcile names, code, and documentation.

That makes this codebase one concrete case study, not proof that Pinboard eliminates every mistake or makes every repository clean.

> Eventually, the conspiracy board needed its own conspiracy board.

## What it covers

- **Intake:** preserve a discovery without silently changing priority or starting work.
- **Planning:** make readiness, deferral, closure, and dependencies explicit.
- **Revisioned definitions:** replace a complete accepted definition with compare-and-swap safety, retain every prior revision, and inspect current or paginated history as typed JSON.
- **Execution:** give each accepted attempt an exact brief and independent renewable ownership.
- **Interruption and recovery:** block, deliberately pause otherwise runnable work, rebind current accepted scope with a corrected Git baseline, resume, or recover without rebuilding context from chat history or silently changing the checkout.
- **Parallel work:** preview independent items and recheck the group as each attempt starts, without creating tasks on the user's behalf.
- **Review:** keep the submitted candidate and its evidence exact, then use a separate Codex reviewer—normally a subagent that returns to the owning task—to accept it or return it for correction.
- **Wrap-up:** reconcile later accepted direction and repository changes before candidate presentation or acceptance, then let the human choose the repository disposition and confirm terminal completion.
- **Handover:** export one revision-stamped JSON package of supported project facts—admitted work, pending proposals, relationships, decisions, and verified review evidence—without choosing a team-tool vendor. Live lease authority remains local.
- **Repository readiness:** use `$repository-readiness` to map the real authority, consumers, projections, and validation behind a representative change before improving an unfamiliar repository; select whole-repository coverage explicitly.
- **Recursive cleanup:** use `$slop-cleanup` to remove approved residue from revised or abandoned features and repeat until a fresh pass finds nothing new.
- **Durable agent guidance:** use `$maintaining-agent-guidance` to place recurring AI-facing knowledge at its smallest authoritative owner without installing generic boilerplate.

The `$pinboard`, `$pinboard-intake`, and `$pinboard-deliver` skills provide the conversational workflows. The `pinboard` command validates and updates the repository-local ledger, rejects stale actions, and keeps unrelated attempts from invalidating one another.

The three repository-care skills are optional and independently usable; none requires a Pinboard ledger.

For the most reliable workflow, keep each outcome with the task that owns it through its final repository decision. That task can delegate bounded research, implementation, and review to subagents whose results return automatically. Start another task for a genuinely independent outcome you intend to follow separately.

Private working state stays in ignored local files:

```text
.codex/pinboard/
  state.sqlite3               # authoritative lifecycle, dependencies, leases, and history
  artifacts/                  # immutable briefs, evidence, proposals, and reviews
  views/                      # generated human-readable projections
```

SQLite is the current ledger authority. Commands read its complete typed state, then commit only the relations named by one accepted mutation; stale or failed changes leave the prior ledger intact. Immutable artifacts retain long-form contracts and review evidence; generated views are convenient projections, not fallback state. The [architecture map](ARCHITECTURE.md) explains package ownership, persistence boundaries, and failure semantics for contributors and agents.

The installed definition commands are:

```sh
pinboard item definition --item-id <item> --json
pinboard item definition-history --item-id <item> --limit 20 --json
pinboard item revise --file <pinboard-item-revision-v1.json> --task-id <task> --host-id <host> --json
```

The current-definition read returns the project revision, item subject revision, definition revision, and definition digest from one snapshot. Preparation acquisition accepts those exact values, and a rejected stale request identifies which observed precondition changed.

Revision files replace the whole `pinboard-work-item-definition/v1`; partial patches are rejected. Blocking can only name dependencies already present in that definition and never changes accepted dependencies itself.

Run `pinboard handover --json` to materialize the strict `pinboard-project-handover/v2` document. The command reads one validated SQLite snapshot, verifies every accepted immutable artifact, embeds its exact bytes as UTF-8 text or base64, and writes nothing unless the complete exported project-facts subset is ready. Preparation and attempt leases stay in the local ledger; the handover document does not transfer live authority.

## Install from GitHub

The plugin currently supports macOS and Linux. It uses [uv](https://docs.astral.sh/uv/) to provide its Python 3.14 runtime and installed command.

Add this repository as a Codex marketplace, then install the plugin:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Start a Codex task in the repository and ask:

> Set up the pinboard here and explain how I can use it from one chat or several chats.

After the first successful setup, Pinboard prints one optional next-steps pointer to `$repository-readiness`, `$slop-cleanup`, and `$maintaining-agent-guidance`. It does not run a skill, create work, or change configuration, and reopening an existing Pinboard or a failed setup does not print it.

Pinboard may separately recommend one setting for long Codex tasks when the user setting `model_auto_compact_token_limit_scope` is absent. It only reads the user config at `~/.codex/config.toml` (or the equivalent under `CODEX_HOME`) and never edits user or project Codex configuration. A trusted project's `.codex/config.toml` can override that user default. Reopening an existing Pinboard, a failed setup, or an unreadable or malformed user config produces no setting recommendation.

When another task uncovers something worth keeping, ask it:

> Add this to the repository work queue as intake: saving a boss fight currently captures temporary animation state. Include what you found and why it could block phase-two save support.

For a quick current picture, ask:

> Give me the quick live-work overview. Then offer the deeper views I can ask for.

## Runtime and development

The repository currently pins Python 3.14.7 and uv 0.12.10. msgspec provides immutable records and strict JSON decoding at repository boundaries. uv manages Python installation, the project environment, Python dependencies, the checked-in Python lockfile, and Python command execution.

jscpd is the sole non-Python development tool. It requires Node.js 18 or newer and npm, but no global package installation. Install the pinned native binary into this repository's ignored `node_modules/` directory:

```sh
npm ci --prefer-offline --no-audit --no-fund
```

The checked-in `package-lock.json` makes that installation repeatable. The install prefers npm's local cache and skips registry audit and funding requests that are unrelated to this development-only binary. `npm run duplication` performs an aggressive local scan at four lines and 40 tokens; its matches are prompts for judgment, not failures to eliminate mechanically. CI uses calmer eight-line and 60-token limits, rejects every clone that is new relative to `origin/main`, and enforces a 0.3% ceiling; the current accepted scan reports 0.2%. Lower the ceiling when later cleanup reduces that result rather than raising it to accommodate new duplication.

```sh
uv sync --locked
uv run --locked python -m docs.how_it_works.render --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked pyrefly check
uv run --locked pyrefly coverage check src --strict --fail-under 100
uv run --locked coverage run -m unittest discover -v
uv run --locked coverage report
uv run --locked python scripts/validate-metadata.py
npm run duplication
npm run duplication:ci
uv build --no-sources
scripts/pinboard --help
```

Local Python checks, CI, and the plugin launcher all use the package installed by uv. Copy-paste detection runs separately through the project-local jscpd installation. Every pull request and main-branch update runs the macOS and Linux checks, including plugin and skill validation.
