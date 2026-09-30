# Behavioral evaluation harness

This harness measures how agents that load Pinboard's skills talk to a person. It replays scripted conversations against a Claude Code or Codex agent in disposable projects, has a blind scorer grade every reply against a frozen checklist, and compares a baseline skills revision with a candidate per checklist rule. [CRITERIA.md](CRITERIA.md) states how many runs a comparison needs, how the result is classified as improved, no worse or inconclusive, and what one comparison costs.

A guidance change cites this harness and its criteria by path and revision as its acceptance check. It does not copy the harness.

The harness is a development tool outside the installed Pinboard package. It evaluates Pinboard only as an external client: through an exported revision's own `scripts/pinboard` launcher, skills and MCP server. Live runs spend real money and are verified on macOS; the unit tests that cover its arithmetic and credential handling run on macOS and Linux in CI, which never runs an agent or a scorer.

## What you need

- A prepared source checkout (`scripts/prepare-worktree`). Every command runs as `uv run --locked python -m evals.behavioral <command>` from the checkout root.
- `claude` on `PATH` for Claude Code runs, blind scoring and substance assessment.
- `codex` on `PATH` and a Codex login in `~/.codex/auth.json` for Codex runs.
- An output directory outside the repository for evidence, and a world directory for disposable projects. For Codex runs the world directory must lie outside the system temporary directories, because Codex's workspace profile makes those writable and the sandbox would no longer match a normal project.

Every paid command takes `--cap-usd`. A session starts only when its projected known cost still fits. Missing main, scorer or assessor usage stops further paid work. Automatic approval reviewers have separate token accounting and an unknown price; continuing with that unknown price requires the explicit human exception described below, and cannot establish a full-dollar comparison cap.

## Evaluate a skills revision

The representative path exports each revision, runs a scenario set for one runtime, scores, then compares.

1. **Export** each revision to evaluate. The export is `git archive` of the commit plus a private runtime prepared by that export's own launcher:

   ```sh
   uv run --locked python -m evals.behavioral export --revision <commit> --dest <exports>/<name>
   ```

2. **Run** a scenario set against one runtime. Each run seeds a fresh world, sends every scripted human turn to one agent session and records the evidence:

   ```sh
   uv run --locked python -m evals.behavioral run claude --export <exports>/<name> \
     --scenario-set evals/behavioral/data/scenario-sets/s13-s17.json --variant <label> --runs <n> \
     --model claude-sonnet-5-5 --out <out> --worlds <worlds> --cap-usd <usd> [--jobs <parallel>]

   uv run --locked python -m evals.behavioral run codex --export <exports>/<name> \
     --scenario-set evals/behavioral/data/scenario-sets/s13-s17.json --variant <label> --runs <n> \
     --model gpt-6-sol --reasoning-effort high \
     --out <out> --worlds <worlds> --cap-usd <usd>
   ```

   Before the first paid Codex run with a new Codex version or configuration, prove isolation cheaply:

   ```sh
   uv run --locked python -m evals.behavioral probe codex --export <exports>/<name> --name <probe> \
     --model gpt-6-sol --reasoning-effort high --out <out> --worlds <worlds> --cap-usd <usd>
   ```

3. **Score** every recorded run that has no score yet (`--scores-per-run 2` rescored runs to measure scorer variance):

   ```sh
   uv run --locked python -m evals.behavioral score --out <out> --cap-usd <usd> [--variant <label>]
   ```

4. **Compare** a baseline with a candidate over the scenario set, or **report** one variant alone. The verdict rests on the total and the checklist rules the set declares as targeted; every other item is reported as advisory and warns when it may have worsened:

   ```sh
   uv run --locked python -m evals.behavioral compare --out <out> --scenario-set <set> --baseline <label> --candidate <label>
   uv run --locked python -m evals.behavioral report --out <out> --scenario-set <set> --variant <label>
   ```

5. For Codex runs, **assess** whether each turn's substance reaches the final reply or stays in progress commentary. This is separate from checklist scoring:

   ```sh
   uv run --locked python -m evals.behavioral assess --out <out> --cap-usd <usd>
   ```

6. **Total spend**, itemized by category and group:

   ```sh
   uv run --locked python -m evals.behavioral spend --out <out>
   ```

## Bounded Codex coverage

`coverage-codex` exports one frozen candidate, runs an isolation probe, and alternates s14 and s16. Each target run is blind-scored and assessed before the next target starts. The registered scenario bytes and `targeted_rules=[]` remain unchanged. This is targeted coverage; it does not produce an improved or no-worse statistical verdict.

The human must explicitly authorize the unknown reviewer-price exception for this batch before selecting `--accept-unknown-reviewer-price`. This flag permits unknown reviewer dollars only. It does not permit unreported primary, scorer or assessor usage, and it does not waive [CRITERIA.md](CRITERIA.md)'s full-dollar comparison requirement.

```sh
uv run --locked python -m evals.behavioral coverage-codex \
  --source <checkout> --revision <commit> --export <fresh-export> \
  --scenario-set evals/behavioral/data/scenario-sets/s13-s17.json --variant <label> \
  --runs-per-target 6 --maximum-seconds 10800 \
  --model gpt-6-sol --reasoning-effort high --out <fresh-out> --worlds <fresh-worlds> \
  --cap-usd 120 --accept-unknown-reviewer-price
```

One monotonic deadline begins before export/setup and bounds setup, credential locking, runtime turns, hooks, state reads, scoring and assessment. The limit is at most three hours and twelve target runs, six per target. No new effect starts after expiry. Active subprocess groups are killed and reaped before credentials settle; the MCP client's cancellation cleanup closes its server. Mandatory cleanup can finish after the deadline. Raw partial output and completed evidence remain available. `window.json` fixes the start, deadline, candidate, scenario digests and spending exception; `coverage.json` records completed, scored and assessed runs and known-priced spend. Unknown reviewer dollars keep total dollars unknown. Coverage is incomplete if either target lacks a completed, blind-scored run with the required Git effects; assess those effects from the recorded state and score.

## Scenarios, checklist and worlds

`data/scenarios/` holds the scripted conversations. Each scenario fixes its human turns, the hooks the harness runs between turns on the human's behalf, its world kind and any extra seeding, and the ground truth the scorer checks accuracy against. `data/world-facts-full.md` describes the full world every full-world scenario starts from. A scenario set in `data/scenario-sets/` registers its members by SHA-256, and the harness refuses to run a set whose files changed. It also lists, in `targeted_rules`, the checklist rules a comparison over it targets; the shipped sets target none. `s13-s17.json` is the held-out set for both runtimes. Codex uses exact-action approval requests for the Git actions in s14 and s16. `codex-s13-s15-s17.json` preserves the historical subset used before that route; its measurements do not establish coverage of s14 or s16 (see [CRITERIA.md](CRITERIA.md)). A comparison's held-out scenarios and targeted rules must be registered this way before the last guidance edit it evaluates; a new held-out scenario may stay in the owning item's private evaluation directory until then, because tracked scenarios are visible to later tuning agents.

`data/checklist.md` is the frozen scoring checklist. Scoring verifies its SHA-256 before every session and never runs with an edited copy.

A world is a small shell project called `tally` with a bare `origin`, a Pinboard board seeded through the exported revision's own MCP tools, and, for the minimal world, an empty scratch board. Seeding uses the synthetic host id `eval-host`, and each run verifies that every seeded item reached its declared state. The fixture keeps project guidance in `data/fixture/` under non-instruction file names and writes `CLAUDE.md` and `AGENTS.md` only inside a world. The one runtime difference is declared and recorded per run: a Codex world's `AGENTS.md` also carries the project guidance that a Claude Code world keeps in `CLAUDE.md`, because Codex reads only `AGENTS.md`.

## Runtimes

A Claude Code run uses one `claude -p` session resumed for every turn, with `--setting-sources project`, the export as its only `--plugin-dir`, `--permission-mode bypassPermissions` and claude.ai account connectors switched off. The run records the loaded plugins, skills, MCP servers and hooks from its stream, the instruction files in the world, the names of the Claude host variables the agent inherited, the host id the agent saw at session start, each turn's own cost (the stream reports the session total), and permission denials. Because the stream names skills without their source, the first turn also writes Claude Code's debug log to a temporary file, from which the run records Claude Code's own count of skills by source before deleting the file. A skill from outside the exported plugin and Claude Code's bundled set, an unexpected plugin or MCP server, or a missing count stops the run and every Claude Code run not yet started, for a human decision. A turn that Claude reports as an error ends the run as failed and unscored.

A Codex run uses one `codex exec` thread resumed for every turn, in its own disposable Codex home that holds only a private copy of `~/.codex/auth.json` and the harness-written `config.toml`. That configuration names the explicit model and reasoning effort, turns off memories, bundled system skills, account apps and remote plugins, sets approval policy `on-request` and the optional `auto_review` reviewer with the documented Pinboard permission profile, installs the export as the only plugin and pre-approves only that plugin's Pinboard MCP server tools, as Pinboard's own Claude Code hook does for its tools. The run records the model-visible context Codex renders for that home, the sandbox and writable roots, each turn's progress commentary apart from its final reply, each turn's own token usage (Codex reports thread totals) priced at the recorded API list price, and permission denials. It also keeps the session rollout Codex wrote in that home, because `codex exec --json` leaves out tool calls that the sandbox or approval policy refused before they ran; refusals are read from both. An ordinary sandbox refusal remains evidence while the agent requests approval for the exact human-authorized Git action. A genuine reviewer-owned final `deny`, or an explicitly rejected tool approval, stops that run for a human decision. Successful recovery continues to the next scripted turn. Quoted guardian history and instruction text do not establish rejection. The harness records the latest cumulative usage per primary or reviewer thread, deduplicates response identifiers, prices the primary thread at its declared model price and reports reviewer usage separately with an unknown dollar price. Codex runs never overlap. If Codex refreshed the copied login during a run, the refreshed copy is written back only while `~/.codex/auth.json` is unchanged; otherwise nothing is written and further Codex runs stop. The disposable home is removed after every run, after subprocess cleanup and credential settlement. A timed-out or usage-incomplete turn retains raw output and unreported cost as unknown.

## Scoring and outputs

A blind scorer is a fresh `claude -p` session with the pinned scorer model, no tools, no skills and no MCP server. It sees the checklist, the scenario's ground truth and world facts, the observed Git and board state after each turn, the hooks log and the human-visible transcript under a random label, with local paths redacted. It never sees the runtime, revision or variant. An answer that does not match the checklist's output shape is recorded as a scoring failure and never counted.

The output directory holds `runs/<scenario>/<variant>-<n>/` (run record, raw runtime output and, for Codex, the session rollout, observed state, scorer input), `scores/<label>/` (scorer prompt, raw answer, score and session record), `labels/` (the label-to-run map, kept apart from the scores), `assessments/` and `probes/`. Spend is always recomputed from these records.
