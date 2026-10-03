# Behavioral evaluation harness

This harness measures how agents that load Pinboard's skills talk to a person. It replays scripted conversations against a Claude Code or Codex agent in disposable projects, has a blind scorer grade every reply against a frozen checklist, and compares a baseline skills revision with a candidate per checklist rule. Separate fictional investigation trials record evidence substance without applying that delivery verdict. [CRITERIA.md](CRITERIA.md) states how many runs a delivery comparison needs, how the result is classified as improved, no worse or inconclusive, and what one comparison costs.

A guidance change runs this harness when its recorded drift estimate finds a plausible effect on authority, lifecycle, review, safety or other correctness-relevant behavior, or when it rewrites or reorders existing rules; [CONTRIBUTING.md](../../CONTRIBUTING.md#evaluate-guidance-behavior) describes the estimate. A change that runs it cites this harness and its criteria by path and revision as its acceptance check and does not copy the harness.

The harness is a development tool outside the installed Pinboard package. It evaluates Pinboard only as an external client: through an exported revision's own `scripts/pinboard` launcher, skills and MCP server. Live runs spend real money and are verified on macOS; the unit tests that cover its arithmetic and credential handling run on macOS and Linux in CI, which never runs an agent or a scorer.

## What you need

- A prepared source checkout (`scripts/prepare-worktree`). Every command runs as `uv run --locked python -m evals.behavioral <command>` from the checkout root.
- `claude` on `PATH` for Claude Code runs, blind scoring and substance assessment.
- `codex` on `PATH` and a Codex login in `~/.codex/auth.json` for Codex runs.
- An output directory outside the repository for evidence, and a world directory for disposable projects. For Codex runs the world directory must lie outside the system temporary directories, because Codex's workspace profile makes those writable and the sandbox would no longer match a normal project.

Delivery commands take `--cap-usd`; investigation trials use a fixed 120 USD aggregate and a caller-named 15 USD batch. A session starts only when its projected known cost still fits, then its reported usage is added afterward. One session may overshoot either remaining amount; further paid work stops after an overshoot or unknown primary, scorer, assessor or reviewer cost. Automatic approval reviewers have separate token accounting and an unknown price; the investigation route uses `approvals_reviewer=user` to avoid that unknown-priced path.

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

   Before paid Codex delivery runs, explicitly prove isolation for each exact export, CLI version, model and reasoning effort:

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

One monotonic deadline begins before export/setup and bounds setup, credential locking, runtime turns, hooks, state reads, scoring and assessment. The limit is at most three hours and twelve target runs, six per target. No new effect starts after expiry. Codex receives a bounded native SIGINT interruption/shutdown request, which owns its separate tool groups. Other subprocesses receive group interruption. A forced fallback leaves cleanup unconfirmed: the batch stops and retains the private Codex home without settling or removing credentials, naming it for diagnosis. The MCP client's cancellation cleanup closes its server. Subprocess starts reserve twenty seconds for bounded shutdown and reap; MCP connections reserve seven seconds for the SDK’s bounded teardown. Starts too late to provide cleanup are refused, and cancellation waits remain within the original deadline. Raw partial output and completed evidence remain available. Every started subprocess failure that prevents result delivery carries its available output to the existing evidence writer; a scorer or assessor without validated terminal usage is recorded with unknown cost, including communication or result-close failure. `window.json` fixes the start, deadline, candidate, scenario digests and spending exception; `coverage.json` records completed, scored and assessed runs and known-priced spend. Unknown reviewer dollars keep total dollars unknown. Command completion requires at least one completed, validly blind-scored run per target. Malformed scoring or assessment is incomplete. Operation coverage also requires explicit review of the required Git effects from the recorded state and score; loop completion does not accept those effects.

## Scenarios, checklist and worlds

`data/scenarios/` holds the scripted conversations. Each scenario fixes its human turns, the hooks the harness runs between turns on the human's behalf, its world kind and any extra seeding, and the ground truth the scorer checks accuracy against. `data/world-facts-full.md` describes the full world every full-world scenario starts from. A scenario set in `data/scenario-sets/` registers its members by SHA-256, and the harness refuses to run a set whose files changed. It also lists, in `targeted_rules`, the checklist rules a comparison over it targets; the shipped sets target none. `s13-s17.json` is the held-out set for both runtimes. Codex uses exact-action approval requests for the Git actions in s14 and s16. `codex-s13-s15-s17.json` preserves the historical subset used before that route; its measurements do not establish coverage of s14 or s16 (see [CRITERIA.md](CRITERIA.md)). A comparison's held-out scenarios and targeted rules must be registered this way before the last guidance edit it evaluates; a new held-out scenario may stay in the owning item's private evaluation directory until then, because tracked scenarios are visible to later tuning agents.

`data/scenario-sets/review-after-merge.json` registers content-preserving rebase and squash-merge close requests, a merged change without commissioned ready review, and a status-only control. Its held-out digests and targets P5/P11 are fixed before the guidance revision. `tests/test_behavioral_merge_evidence.py` builds those disposable worlds through the candidate launcher's MCP tools without an agent or paid scorer: it verifies changed commit identities, content presence, independent review evidence, completion or its missing-review rejection, and read-only status effects. These checks prove native fixture conditions; agent-reply improvement requires the maintained live comparison and remains unmeasured until spending is authorized.

`data/checklist.md` is the frozen scoring checklist. Scoring verifies its SHA-256 before every session and never runs with an edited copy.

A world is a small shell project called `tally` with a bare `origin`, a Pinboard board seeded through the exported revision's own MCP tools, and, for the minimal world, an empty scratch board. Seeding uses the synthetic host id `eval-host`, and each run verifies that every seeded item reached its declared state. The fixture keeps project guidance in `data/fixture/` under non-instruction file names and writes `CLAUDE.md` and `AGENTS.md` only inside a world. The one runtime difference is declared and recorded per run: a Codex world's `AGENTS.md` also carries the project guidance that a Claude Code world keeps in `CLAUDE.md`, because Codex reads only `AGENTS.md`.

### Fictional investigation worlds

The separate `data/investigation/sets/` registrations cover urgent diagnosis and cross-service planning in tuning and held-out variants. Each set fixes exact scenario bytes by SHA-256 and declares the targeted investigation measures before guidance tuning. Agent-visible scenario records contain scripted human turns and source material. Hidden keys, including accepted observations, inferences, contradictions, windows, alternatives and inaccessible facts, belong in a private directory outside the source checkout and exported plugin. Loading a key requires its expected digest. The plugin export excludes the development `evals/` and `tests/` trees, so registered held-out fixtures and answer-bearing evaluation prose are absent from the agent-visible plugin.

The `bounded-heldout` case adds a clear one-page deliverable and a fresh-session correction alongside the existing diffuse `cross-heldout` case. Its registered digest and targeted measures are fixed before the corrected skill guidance. Review the first reply and complete file directly for mandatory home/outcome questions, requested coverage, source windows, uncertainty and scope; no private key or key-backed assessor score is claimed for this case.

Build one world without an agent or paid scorer call:

```sh
uv run --locked python -m evals.behavioral investigation-world --scenario-set evals/behavioral/data/investigation/sets/heldout.json --case urgent-heldout --out <new-world-directory>
```

The world has independent Git histories for two fictional services, alongside transcripts, dashboards, metrics, logs, traces and documents under `sources/`. The `inquiry/` directory is the writable investigation home; evidence sources are read inputs. Inventories are deliberately incomplete, and at least one named fact is inaccessible. The source trail supports several conclusions but has no single answer summary. The urgent held-out case requires acknowledgement of current impact, a checkable retry finding and dismissal of a worker-release lead after the human's correction. The cross-service held-out case starts with several plausible goals, records the human's choice of an observability plan, and tests whether later evidence across sources changes the plan without replacing that choice.

Run one Codex trial at a time after inspecting the no-cost exported plugin and loaded context. `ordinary` receives only the human turns, `guidance` receives the focus note, and `structured` receives its record instructions and a strict post-turn JSON shape check. All arms use the same registered source bytes and human turns. The arm text digest, export commit, scenario/set digests, Luna-high setting, actual thread identities, raw output, usage and elapsed time are recorded under one private output home. A fresh turn starts a new thread; saved-file reads count only when a successful command actually reads an inquiry note. Compaction remains unobserved without a runtime event.

```sh
uv run --locked python -m evals.behavioral investigation-codex \
  --export <export> --scenario-set evals/behavioral/data/investigation/sets/tuning.json \
  --case urgent-tuning --arm ordinary --index 1 --out <private-out> --worlds <private-worlds> --batch tuning-1
uv run --locked python -m evals.behavioral investigation-assess \
  --scenario-set evals/behavioral/data/investigation/sets/tuning.json \
  --key-directory <private-keys> --case urgent-tuning --arm ordinary --index 1 \
  --out <private-out> --batch tuning-1
```

The bounded Haiku comparison uses the same registered `bounded-heldout` source seed, exported skill revision and scripted human turns as the Luna-high `guidance` trial. It copies one world, starts a new Claude Code session for each turn in the same inquiry directory, and configures Claude Code's available tools to include only file and shell tools, excluding Pinboard MCP tools. It sends no effort or extended-thinking option. Before each session, the existing aggregate and batch guard reserves projected spend. The route writes that session's raw stream, final reply, token counts, actual Claude-reported cost and strict run record before it can start the next session. A missing cost, failed session or batch overshoot stops further paid work; an interrupted pair with a completed first record can continue its second turn with the same command and exact world. Claude and Codex investigation records retain separate accounting shapes, and `spend` totals both without pricing Claude tokens as Codex tokens.

```sh
uv run --locked python -m evals.behavioral investigation-claude \
  --export <same-export> --scenario-set evals/behavioral/data/investigation/sets/heldout.json \
  --case bounded-heldout --arm guidance --index 1 --out <private-out> --worlds <private-worlds> --batch <batch>
```

The two replies and `kiosk-brief.md` need direct review for completeness, source windows, consequential conclusions, dismissed leads and human effort. A successful saved-brief tool read and distinct session IDs establish this fictional fresh-turn route; they do not establish real-use continuity or a statistical model ranking.

Each new batch records the spend at its start and reserves no more than the smaller remaining amount of its 15 USD target and the 120 USD aggregate. Keep all trials and assessments in the same output home. The assessor receives the private key and unlabeled replies in a fresh tool-free session; its cost and result stay private. New investigation measures are descriptive until separately calibrated, and `decision.py` remains exclusive to the frozen delivery checklist.

`investigation.exercise_sessions` supplies no prior runtime identity for a new or fresh turn and the current identity for a continuation. `record_sessions` rejects a fresh turn that reuses an identity or fails to read saved inquiry evidence. A controlled driver can exercise this route without a paid call. A real agent run must record its actual session or thread identity; a same-session resumed turn is neither fresh-session recovery nor compaction. Compaction remains `unobserved` unless a runtime event is captured explicitly. This checkpoint's deterministic exercises do not establish actual agent recovery or compaction behavior.

Investigation assessment uses `investigation.assessment_prompt` and a separate strict `pinboard-investigation-assessment/v1` result. Its measures cover grounded conclusion, discovery, contradiction and unknown retention, provenance and windows, fresh recovery, repeated work, human effort, document usefulness and stopping. Separate measures cover consequential finding salience, human acknowledgement, direct checkability, dismissed red herrings, focus coaching and source breadth after the goal choice. Every measure receives an independent pass or fail with a specific reason. The frozen delivery checklist and [CRITERIA.md](CRITERIA.md) statistical verdict do not apply to these measures; later controlled comparisons need their own observed variance and authorized spending before claiming improved or no worse.

## Runtimes

A Claude Code run uses one `claude -p` session resumed for every turn, with `--setting-sources project`, the export as its only `--plugin-dir`, `--permission-mode bypassPermissions` and claude.ai account connectors switched off. The existing process boundary constructs a clean Claude environment for agent, version, scorer and assessor calls: present HOME, PATH, TMPDIR, USER, LOGNAME, SHELL and LANG preserve execution identity and existing local OAuth discovery; documented authentication-only ANTHROPIC_API_KEY passes when present; ENABLE_CLAUDEAI_MCP_SERVERS is forced false. Desktop, session, messaging and other host context is removed. The agent session retains its exact passed environment and records variable names only, never values or copied Claude credential files. Additional authentication variables need a named primary CLI/help or documentation authority before retention. The current live login probe covers the retained OS/local OAuth path, not API-key or every provider compatibility. The run records the loaded plugins, skills, MCP servers and hooks from its stream, the instruction files in the world, those actual passed variable names, the host id the agent saw at session start, each turn's own cost (the stream reports the session total), and permission denials. Because the stream names skills without their source, the first turn also writes Claude Code's debug log to a temporary file, from which the run records Claude Code's own count of skills by source before deleting the file. A skill from outside the exported plugin and Claude Code's bundled set, an unexpected plugin or MCP server, or a missing count stops the run and every Claude Code run not yet started, for a human decision. A turn that Claude reports as an error ends the run as failed and unscored.

Before every paid Codex delivery run through `runner.codex_run`, including bounded coverage, the runner freshly observes the CLI version and matches a prior passing probe with no findings against that version, the export commit and skills digest, model and reasoning effort. Missing, failed, incomplete or mismatched probe evidence, or an unavailable version observation, stops before an agent turn and names the explicit probe command above. The runner never launches a probe automatically. A later probe does not qualify earlier runs, and the check does not guarantee against an executable change during a run. The separate `investigation-codex` route observes the version and checks its rendered context before `run_turn`; it does not require a prior probe.

A Codex run uses one `codex exec` thread resumed for every continued turn, in its own disposable Codex home that holds only a private copy of `~/.codex/auth.json` and the harness-written `config.toml`. That configuration names the explicit model and reasoning effort, turns off memories, bundled system skills, account apps and remote plugins, sets approval policy `on-request` with automatic review for delivery routes and user review for investigation trials, and uses the documented Pinboard permission profile. It installs the export as the only plugin and pre-approves only that plugin's Pinboard MCP server tools. The run records the model-visible context Codex renders for that home, the sandbox and writable roots, each turn's progress commentary apart from its final reply, each turn's own token usage (Codex reports thread totals) priced at the recorded API list price, and permission denials. It also keeps the session rollout Codex wrote in that home, because `codex exec --json` leaves out tool calls that the sandbox or approval policy refused before they ran; refusals are read from both. An ordinary sandbox refusal remains evidence while the agent requests approval for the exact human-authorized Git action. An explicitly rejected tool approval stops that run for a human decision. The harness records the latest cumulative usage per primary or reviewer thread, deduplicates response identifiers, prices the primary thread at its declared model price and reports reviewer usage separately with an unknown dollar price. Codex runs never overlap. If Codex refreshed the copied login during a run, the refreshed copy is written back only while `~/.codex/auth.json` is unchanged; otherwise nothing is written and further Codex runs stop. The disposable home is removed after confirmed subprocess cleanup and credential settlement. Unconfirmed native shutdown retains the private home for diagnosis without touching the original credential. A timed-out or usage-incomplete turn retains raw output and unreported cost as unknown.

## Scoring and outputs

A blind scorer receives the same clean Claude process environment and is a fresh `claude -p` session with the pinned scorer model, no tools, no skills and no MCP server. It sees the checklist, the scenario's ground truth and world facts, the observed Git and board state after each turn, the hooks log and the human-visible transcript under a random label, with local paths redacted. It never sees the runtime, revision or variant. An answer that does not match the checklist's output shape is recorded as a scoring failure and never counted.

The output directory holds `runs/<scenario>/<variant>-<n>/` (run record, raw runtime output and, for Codex, the session rollout, observed state, scorer input), `scores/<label>/` (scorer prompt, raw answer, score and session record), `labels/` (the label-to-run map, kept apart from the scores), `assessments/` and `probes/`. Spend is always recomputed from these records.

Scripted long-gap and slowdown turns exercise approval and conversation rules. They do not establish actual context compaction or cancellation of live typing; the supported harness controls scripted turns and declared hooks.
