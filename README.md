# Pinboard

## Keep your coding agent building the product you meant

<img align="right" width="430" src="assets/pinboard-investigation-office.png" alt="A 1970s office worker explaining a wall-sized investigation board covered with a map, notes, portraits, diagrams, colored markers, and connecting thread">

Long-running agent work can drift into a coherent, well-tested product that no one actually decided to build. Pinboard is a repository-local record of what you proposed, accepted, built, and reviewed, shared across tasks and interruptions.

Your coding agent still does the work. Pinboard keeps that work tied to the decision.

<br clear="right">

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

## What it covers

- **Intake:** preserve a discovery without silently changing priority or starting work.
- **Planning:** make readiness, deferral, closure, and dependencies explicit.
- **Readable evidence:** link every conversational item or evidence reference to its confirmed human-readable Markdown artifact, so the supporting context is directly usable without exposing machine-only workflow state.
- **Revisioned definitions:** replace a complete accepted definition with compare-and-swap safety, retain every prior revision, and inspect current or paginated history as typed JSON.
- **Execution:** start preparation atomically from the current accepted definition, freeze the accepted outcome and stable authorities in an exact brief, then let the worker derive concrete impact from the candidate under independent renewable ownership.
- **Interruption and recovery:** block, deliberately pause otherwise runnable work, rebind current accepted scope with a corrected Git baseline, resume, or recover without rebuilding context from chat history or silently changing the checkout.
- **Parallel work:** preview independent items and recheck the group as each attempt starts, without creating tasks on the user's behalf.
- **Review:** derive the next operation from current ledger state; optionally bind one explicitly selected, separately validated checkpoint package; bind correction rounds to an exact return receipt plus the current prior review; then use a separate reviewer in the current coding-agent runtime—Codex as the primary, stress-tested integration; Claude Code experimentally—to examine changed and unclassified surfaces, reuse unchanged evidence, and widen for architecture, persistence, wire, lifecycle, dynamic consumers, or a demonstrated blind spot.
- **Wrap-up:** reconcile later accepted direction and repository changes, report exact current work item and attempt states, then execute only the repository disposition, verified disposable-worktree and branch cleanup, and terminal transition the human explicitly authorizes—including as one ordered instruction.
- **Handover:** export one revision-stamped JSON package of supported project facts—admitted work, pending proposals, relationships, decisions, and verified review evidence—without choosing a team-tool vendor. Live lease authority remains local.
- **Technical writing:** use `$technical-writing` to shape substantial project documents through meaningful checkpoints or quietly improve briefs, reports, readable artifacts, and pull-request descriptions when the accepted facts are already available.
- **Repository readiness:** use `$repository-readiness` to map the real authority, consumers, projections, and validation behind a representative change before improving an unfamiliar repository; select whole-repository coverage explicitly.
- **Recursive cleanup:** use `$slop-cleanup` to remove approved residue from revised or abandoned features and repeat until a fresh pass finds nothing new.
- **Durable agent guidance:** use `$maintaining-agent-guidance` to place recurring AI-facing knowledge at its smallest authoritative owner without installing generic boilerplate.

The `$pinboard`, `$pinboard-intake`, and `$pinboard-deliver` skills provide the coordination workflows. `$technical-writing` joins them only for substantial document work or human-facing artifacts; routine Pinboard coordination does not load it. The `pinboard` command validates and updates the repository-local ledger, rejects stale actions, and keeps unrelated attempts from invalidating one another.

The three repository-care skills are optional and independently usable; none requires a Pinboard ledger.

For the most reliable workflow, keep each outcome with the task that owns it through its final repository decision. That task can delegate bounded research, implementation, and review to subagents whose results return automatically. Start another task for a genuinely independent outcome you intend to follow separately.

Private project data stays in ignored local files. The installed plugin cache contains packaged, immutable code and skill assets; routine Pinboard work never writes there.

```text
.codex/pinboard/
  state.sqlite3               # authoritative lifecycle, dependencies, leases, and history
  artifacts/                  # immutable briefs, evidence, proposals, and reviews
  views/                      # generated human-readable projections
```

SQLite `sqlite-v6` is the current ledger authority. Explicit versioned planned-replacement records stop preparation, continuation, submission, and acceptance for one affected task without changing its lifecycle state; a human may instead record temporary retention for the exact relation revision and accepted switching cost. Named item, attempt, authority, exact action-ID, leased-action, dispatch, and mutation paths select only their exact facts and direct decision relationships; observer action discovery and unleased worker/preparer rejection read no project state. Item status includes at most the current nonterminal attempt rather than retained attempt history. Status reads active attempt identities and a fixed set of transactionally maintained state counts. Overview, project-role action discovery without an exact identity, and empty-selection all-safe preview intentionally read the current live portfolio, excluding retained definition history, terminal attempt bodies, artifacts, and transition receipts. Initialization, validation, view rebuild, and handover are explicit project-wide operations, but only validation reads every authoritative relation: rebuild reads declared projection facts and handover reads exported relations and artifacts. State-independent commands compose no store; every other invocation resolves one durable layout, constructs one store at the outer command boundary, and passes required capabilities inward. Ordinary mutations validate and reload only changed facts, while stale or failed changes leave the prior ledger intact. Generated views are per-item, per-attempt, and per-receipt projections; ordinary work refreshes only affected paths and preserves equal files, while explicit rebuild reconciles the whole set and removes legacy aggregate queue and history files. The [architecture map](ARCHITECTURE.md) explains the exact scope inventory, current scaling limits, and failure semantics.

The installed command describes its own supported operations without opening project state:

```sh
pinboard tool-contract --json
pinboard tool-contract --operation transition --json
pinboard tool-contract --operation transition:attempt --json
pinboard tool-contract --action-kind submit-review --json
pinboard tool-contract --brief-starter local --json
```

The compact index is derived from the installed parser leaves, exact command union, lifecycle action family, and closed brief-boundary family. Every operation is classified as static, focused, current-project, selection-dependent, or explicit project-wide work. A selected detail reports that data scope and its consequence alongside the installed CLI usage, optional global-root placement, purpose, effect class, exact acquisition or retained authority, subject, lifecycle precondition, strict input or artifact schema, actual success receipt, and retry semantics. Action details name an execution route so advisory runtime actions are not mistaken for payload-bearing transitions. Brief publication additionally provides complete unresolved local and cross-boundary starters, every applicable structural-union choice with exact replacement templates, canonical byte rules, relational constraints, and the exact fact-validation boundary. Use the boundary-specific selector, choose one returned variant at every applicable selection path, replace only those explicit structural values, then fill every semantic `null` without dropping fields. Publication validates structure, cross-references, and canonical bytes; it does not resolve the declared branch or base revision against Git or prove that semantic claims are true.

JSON-capable commands also return a stable `pinboard-rejected-operation/v1` document for malformed input, expected rejection, and known infrastructure failure. It separates unchanged rejection from a committed evidence-publication effect, reports exact mismatches and retry safety, and may include fresh same-subject action receipts. This makes mechanical command selection and recovery possible without reading Pinboard source. It does not make product meaning mechanical: choosing source authority, writing truthful scope and verification, providing an independent reviewer, and deciding repository disposition still require model or human judgment. If the Codex runtime cannot create a review subagent, the exact candidate remains in review for a capable runtime; Pinboard does not silently substitute the implementer or the user.

The installed definition commands are:

```sh
pinboard item definition --item-id <item> --json
pinboard item definition-history --item-id <item> --limit 20 --json
pinboard item revise --file <pinboard-item-revision-v1.json> --task-id <task> --host-id <host> --json
```

The ordinary preparation command selects the current accepted definition and either creates the first claim or transfers an inactive retained claim atomically:

```sh
pinboard preparation start --item-id <item> --task-id <task> --host-id <host> --ttl-seconds 7200 --json
```

A live conflicting claim or an item that is no longer ready is rejected without change. The lower-level acquire and transfer commands remain available for exact recovery and diagnosis.

Task and host strings on direct project operations are audit attribution supplied by the invoking Codex task. They are not authenticated credentials. Preparation and attempt lease IDs plus fencing generations are the retained operation authority.

Revision files replace the whole `pinboard-work-item-definition/v1`; partial patches are rejected. Blocking can only name dependencies already present in that definition and never changes accepted dependencies itself.

After a transition, `--json` includes continuation derived from the exact affected attempt when one is involved. The same record is available directly, and an exact candidate-bound review job can be rendered without mutating the ledger:

```sh
pinboard attempt inspect --attempt-id <attempt> --json
pinboard review-job --attempt-id <attempt> --candidate-revision <candidate> --json
pinboard validate --json
pinboard review-job --attempt-id <attempt> --candidate-revision <candidate> --checkpoint-history-id <history> --correction-history-id <history> --json
```

The review job names the outcome-owning task and records the accepted brief and current `result.md` paths and SHA-256 digests. Its optional history IDs are caller-carried, exact same-attempt selections: use `--checkpoint-history-id` only after the separate validation command succeeds, and capture the successful return-for-correction transition JSON's `history_id` for `--correction-history-id` to bind the preserved rejected candidate and reason independently from the current `review.md` bytes. New correction returns record a strict canonical reason input; historical generic-input receipts remain readable ledger evidence but are not eligible correction context. A fresh reviewer verifies those bytes, compares each selected historical candidate with the current candidate, and reopens evidence whose owning relationships changed. Pinboard does not create or wake a Codex task: the owning task invokes a review subagent through the runtime and keeps responsibility for the verdict. If that runtime capability is absent, the exact candidate remains safely in review until a capable runtime continues it.

Run `pinboard handover --json` to materialize the strict `pinboard-project-handover/v5` document. The command reads the exact exported SQLite relations, including versioned planned replacements and exact-revision temporary-retention dispositions, in one consistent read transaction. It verifies every exported immutable artifact, validates accepted checkpoint and covered-completion packages against their historical receipts and complete immutable closure, and exposes valid packages as typed portable evidence while retaining their generic artifact references and exact UTF-8 or base64 bytes. It writes nothing unless the complete exported project-facts subset is ready. Preparation and attempt leases, state counters, and disposed proposal collections stay in the local ledger; the handover document does not transfer live authority.

## Install from GitHub

The plugin currently supports macOS and Linux. It uses [uv](https://docs.astral.sh/uv/) to provide its Python 3.14 runtime and installed command.

For the primary Codex integration, add this repository as a marketplace, then install the plugin:

```sh
codex plugin marketplace add valsteen/pinboard
codex plugin add pinboard@pinboard
```

Pinboard's first default initialization first adds the exact local-only exclusion `/.codex/pinboard/` to `.git/info/exclude`, then creates `.codex/pinboard/`. Approve that exact `pinboard init` command once: it needs the narrow Git-metadata write for this setup only. It does not edit `.gitignore`, and it leaves sibling `.codex` content visible to Git. If a later initialization step fails, JSON reports exactly which surfaces that invocation already committed: the Git exclusion, and also the ledger when database publication finished before a generated-view failure. A repeat that publishes neither surface reports an unchanged result. Linked worktrees share the repository-local exclusion, so repeating initialization remains idempotent.

For routine commands from a normal checkout at the default work root, use a named [Codex permission profile](https://learn.chatgpt.com/docs/permissions) that reopens only Pinboard's project-data directory inside the otherwise protected `.codex` tree:

```toml
default_permissions = "pinboard"

[permissions.pinboard]
extends = ":workspace"

[permissions.pinboard.filesystem.":workspace_roots"]
".codex/pinboard" = "write"
```

From a linked worktree, the shared default ledger is outside that checkout. Add a direct filesystem rule for only the exact resolved shared-repository directory reported by `pinboard root`, which already emits JSON—for example, `"/path/to/shared-repository/.codex/pinboard" = "write"` under `[permissions.pinboard.filesystem]`. Do not add the shared repository as a workspace root or grant its `.git`, sibling `.codex` paths, or the installed plugin cache. An explicit `--work-root` likewise needs a direct write rule for that exact selected directory. Remove legacy `sandbox_mode` and `sandbox_workspace_write` settings before relying on the profile because those settings override permission profiles. If a routine mutation lacks the rule, JSON output reports `SQLITE_READONLY`, the exact database and operation, recovery naming `.codex/pinboard` for a normal default checkout or the exact absolute effective work root for a linked worktree or explicit root, `do-not-retry`, and whether the ledger stayed unchanged or a separately published immutable artifact already committed.

Start a Codex task in the repository and ask:

> Set up the pinboard here and explain how I can use it from one chat or several chats.

For the experimental Claude Code integration, clone this repository, then either try it for one session or install it as a local marketplace plugin:

```sh
# One session only, no persistent registration:
claude plugin validate /path/to/pinboard --strict
cd /path/to/your-project
claude --plugin-dir /path/to/pinboard

# Or install it once and keep it across sessions:
claude plugin marketplace add /path/to/pinboard
claude plugin install pinboard@pinboard
```

Ask Claude Code to set up the Pinboard in the opened project. The shared skills invoke the same `pinboard` CLI and `.codex/pinboard` SQLite authority. In either case, this is a local, repository-sourced Claude Code plugin. The persistent route uses Claude Code's marketplace mechanism; Pinboard is not published in or installed from Anthropic's official marketplace, and neither route claims live Codex/Claude sharing. The free Claude chat plan and Claude Code access are separate product surfaces; check [Anthropic's current authentication options](https://code.claude.com/docs/en/authentication) before the authenticated smoke because access can change.

After the first successful setup, Pinboard prints one optional next-steps pointer to `$repository-readiness`, `$slop-cleanup`, and `$maintaining-agent-guidance`. It does not run a skill, create work, or change configuration, and reopening an existing Pinboard or a failed setup does not print it.

Pinboard may separately recommend one setting for long Codex tasks when the user setting `model_auto_compact_token_limit_scope` is absent. It only reads the user config at `~/.codex/config.toml` (or the equivalent under `CODEX_HOME`) and never edits user or project Codex configuration. A trusted project's `.codex/config.toml` can override that user default. Reopening an existing Pinboard, a failed setup, or an unreadable or malformed user config produces no setting recommendation.

When another task uncovers something worth keeping, ask it:

> Add this to the repository work queue as intake: saving a boss fight currently captures temporary animation state. Include what you found and why it could block phase-two save support.

Conditional wording stays bounded. For example, “if this proves to be a production defect, follow it up” creates at most one follow-up or independent intake item after evidence proves the condition. A false or unproved condition creates nothing, exact existing coverage is reused, and the wording does not authorize starting or implementing the new work.

For a quick current picture, ask:

> Give me the quick live-work overview. Then offer the deeper views I can ask for.

## Runtime and development

The repository currently pins Python 3.14.7 and uv 0.12.10. msgspec provides immutable records and strict JSON decoding at repository boundaries. uv manages Python installation, the project environment, Python dependencies, the checked-in Python lockfile, and Python command execution. The installed plugin launcher runs the package from its already prepared cached environment; a source checkout falls back to its locked development environment. Users should not need to select a uv cache directory or tolerate dependency-update warnings during ordinary installed use.

jscpd is the sole non-Python development tool. It requires Node.js 18 or newer and npm, but no global package installation. After cloning the repository or creating a new implementation worktree, prepare both locked development environments with one command:

```sh
scripts/prepare-worktree
```

The command runs `uv sync --locked` and `npm ci --prefer-offline --no-audit --no-fund` from the selected checkout. Each checkout keeps its own ignored `.venv/` and `node_modules/` directories while uv and npm may reuse their package caches. The checked-in locks make setup repeatable. The npm install prefers its local cache and skips registry audit and funding requests that are unrelated to this development-only binary. `npm run duplication` performs an aggressive local scan at four lines and 40 tokens; its matches are prompts for judgment, not failures to eliminate mechanically. CI uses calmer eight-line and 60-token limits, rejects every clone that is new relative to `origin/main`, and enforces a 0.3% ceiling; the current accepted scan reports 0.2%. Lower the ceiling when later cleanup reduces that result rather than raising it to accommodate new duplication.

```sh
scripts/prepare-worktree
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
