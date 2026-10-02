# Variability criteria

These criteria decide when a behavioral comparison can say that a candidate skills revision is **improved** or **no worse** than its baseline, and when it must say **inconclusive**. The `compare` command implements them exactly; the parameters live in `decision.CRITERIA`. A guidance change that uses this harness as its acceptance check cites this file and the harness by path and revision.

## What a comparison needs

- **A registered scenario set that declares the targeted rules.** The set file registers its held-out scenarios by SHA-256 and lists, in `targeted_rules`, the checklist rules the guidance change is meant to move. Both are fixed before the last guidance edit the comparison evaluates. An empty list declares that the change targets no single rule, so the verdict rests on the total alone.
- **Enough runs on each side:** at least six runs per scenario and at least thirty per side, each with one blind score from the pinned scorer model (`claude-opus-5-5`). That is six runs per scenario on a five-scenario set and ten on a three-scenario set. A comparison with fewer runs on either side of any scenario is inconclusive, whatever its numbers show.
- **One recorded-spend cap of 120 USD** for everything the comparison spends: agent runs on both sides, scorer sessions and, for Codex, substance assessments. Every paid command reserves a projected cost before it starts, then records usage after the session. A single session can exceed the remaining amount because the runtimes do not provide an in-session dollar cutoff. Further paid work stops after an overshoot or incomplete cost accounting; the comparison reports inconclusive when the required runs are unavailable. Baseline runs already recorded for the same export, runtime, model and scenario set may be reused, which halves the cost.

## Decision rule

For every checklist item, and for the total over all items, a run's value is its number of failing replies. Each side's rate is failures per run over all its runs; a side or scenario without a scored run has no rate and is reported as `none`.

For each item and for the total, the comparison estimates the candidate-minus-baseline difference as the mean over scenarios of the per-scenario difference of means, so each scenario weighs the same whatever its run count. Its standard error uses the pooled within-scenario standard deviation of both sides, never below a floor: 3.0 failures per run for the total and 0.5 for an item. The interval is the difference plus or minus two standard errors. Each estimate is classified:

- **Improved:** the interval lies entirely below zero.
- **No worse:** the interval's upper bound is at most the no-worse margin, 2.0 failures per run for the total and 0.75 for an item.
- **Inconclusive:** anything else, including too few runs.

The verdict rests on the total and the targeted rules:

- **Improved** when every targeted rule is improved and the total is improved or no worse. With no targeted rule, the total itself must be improved.
- **No worse** when the total and every targeted rule are improved or no worse, but the comparison is not improved.
- **Inconclusive** otherwise.

Every other checklist item is advisory. It never changes the verdict, but the output lists it as a warning when its interval's upper bound lies above the item margin, so a reviewer sees a rule that may have worsened. The output also lists every item's role, rates, difference, interval, classification and pooled standard deviation, the run counts, each run's failed items and the criteria.

**How often an equal candidate passes.** Simulating a candidate that truly equals its baseline, with the measured standard deviations below, `compare` classifies it no worse or better in about 72% of comparisons when only the total decides, 71% with one typical targeted rule such as P9, and 43% when the targeted rule is the noisiest item, P11. Targeting P3, P5 and P11 together drops it to 31%, about what requiring all thirteen items would give. The simulation draws each rule independently of the total, which understates the joint chance when a rule and the total move together. The remaining chance is the price of a two-standard-error interval at thirty runs per side: an inconclusive comparison of an equal candidate means the evidence could not rule out a worsening, not that one occurred.

## Evidence

Three measurements feed the parameters. All scores are single blind scores unless stated.

| Source | What varies | Runs | Total failures per run, within-scenario SD |
| --- | --- | --- | --- |
| Repeat batch: Claude Sonnet 5.5 on the skills at `82a4c4b`, scenarios s13 to s17, three runs each | agent and scorer | 15 | 2.89 (df 10) |
| Same repeat batch, each run scored twice | scorer only | 15 pairs | 1.28 per score |
| Same repeat batch, mean of two scores per run | agent (scorer halved) | 15 | 2.74 (df 10), agent SD about 2.6 |
| Existing clarify-experiment-closeout-status-1 runs repeated on the same skills variant and scenario | agent and scorer | 58 in 28 groups | 2.10 (df 30) |
| Of those, the held-out s13 to s17 candidate pairs | agent and scorer | 10 in 5 pairs | 4.10 (df 5) |
| Existing s13 to s17 transcripts rescored with this harness's scorer | scorer and scorer configuration | 16 | mean change −0.25, SD of change 1.34 (about 0.95 per score) |

Per item, the largest single-score within-scenario SDs in the repeat batch were P11 1.29, P5 1.10, P3 0.73 and N1 0.68; every other item stayed at or below 0.45. The same items lead in the existing runs (P11 0.79, P3 0.71, P5 0.62, P10 0.53). The repeat batch's rates on s13 to s17 were 5.9 failed items per run in total (s13 10.0, s14 7.0, s15 3.8, s16 6.7, s17 2.0).

## How the parameters follow

1. Agent variance dominates. The scorer contributes about 1.3 of the roughly 2.9 standard deviation, so a second score per run would shrink the per-run variance by about a tenth for an eighth more cost. Each run is therefore scored once and the money goes into runs.
2. The total's standard-deviation floor is 3.0, just above the best-supported estimate (2.89 with ten degrees of freedom). The observed pooled value replaces the floor when it is larger, as it is for the noisy round-2 held-out pairs (4.1).
3. The item floor is 0.5, at the level most items reach; the items that vary more (P11, P5, P3) carry their own larger pooled values into the interval.
4. With the same number of runs n in each of S scenarios, the total's interval half-width at the floor is 2 × 3.0 × √(2 / (n × S)): it depends only on the runs per side. It is 1.90 at twenty runs per side, 1.55 at thirty and 1.34 at forty. Thirty is the smallest count whose half-width sits clearly inside the total's no-worse margin, while a true reduction of about three failures per run is classified improved almost always. Six runs per scenario stays as a lower bound so that no scenario rests on a handful of runs.
5. The no-worse margins, 2.0 for the total (about a third of the repeat batch's 5.9) and 0.75 for an item, let an equal candidate pass at thirty runs per side for the noisiest item (P11: half-width about 0.67) and still stop a candidate that is materially worse.
6. Two standard errors approximates a 95% interval. The floors, rather than a larger multiplier, absorb the small degrees of freedom.
7. The verdict uses only the total and the declared targeted rules because every additional required rule is another chance for an equal candidate to fail: requiring all thirteen items would pass an equal candidate only about 31% of the time. Declaring the targets before the last guidance edit keeps a comparison from choosing its deciding rules after seeing the results.

Applied to the round-2 held-out scores of clarify-experiment-closeout-status-1 (baseline against candidate-r2v6, one or two runs per scenario and side, no targeted rule), `compare` reproduces that evaluation's per-item rates (totals 6.5 and 5.8 over 6 and 10 runs) and classifies every item and the total as inconclusive for too few runs.

## Scenario sets per runtime

`data/scenario-sets/s13-s17.json` is the held-out set for both runtimes. Codex requests approval for the exact human-authorized Git actions in s14 and s16 while retaining the narrow permission profile. `data/scenario-sets/codex-s13-s15-s17.json` is the historical three-scenario subset; those measurements cannot establish Git-action coverage.

A separately authorized bounded coverage batch may run at most six s14 and six s16 target runs within one three-hour window, including export, setup, isolation probe, scoring and substance assessment. Runs alternate between the targets, with each run scored and assessed before the next target. This batch remains inconclusive under the comparison counts above. Coverage requires at least one completed blind-scored run with the required Git effects for each target. Missing or unscored coverage of either target is incomplete.

The coverage batch's 120 USD limit applies to known-priced primary, scorer and assessor usage. If the human explicitly accepts unknown automatic-reviewer prices for that batch, the harness records deduplicated reviewer tokens separately and leaves reviewer dollars and total dollars unknown. This exception does not establish compliance with a full-dollar comparison cap. Missing primary, scorer or assessor usage also remains unknown and prevents another paid session.

## Expected cost

| Runtime and set | Per run (agent + one score) | One comparison, both sides |
| --- | --- | --- |
| Claude Code, Claude Sonnet 5.5, `s13-s17` at six runs per scenario | 0.52 + 0.15 = 0.67 USD, measured over 15 runs | 40 USD; 20 USD when the baseline is reused |
| Codex, gpt-6-sol with high reasoning effort, `codex-s13-s15-s17` at ten runs per scenario | 0.22 + 0.13 + 0.04 (substance assessment) = 0.38 USD, measured over 18 runs | 23 USD; 11.5 USD when the baseline is reused |

Both runtimes report session totals when a session is resumed, and the harness records each turn's own share. The historical per-run costs below exclude automatic-reviewer usage; current Codex coverage reports that usage separately with an unknown price. Codex spend is its reported token usage at the OpenAI API list price recorded in each run (gpt-6-sol short context: input 2.00, cache writes 2.50, cached input 0.20, output 10.00 USD per million tokens). Scenario cost varies widely: s13 costs about 0.08 USD per run in either runtime, s15 about 0.26 (Claude) and 0.15 (Codex), s17 about 0.64 and 0.43, s14 about 0.52 and 0.72, and s16 about 1.11 and 0.47, so a different scenario set needs its own estimate before it starts.

## Limits of the evidence

- Sparse eligible baseline failures can establish no worse without enough evidence to prove improvement. The accepted clean-context P7/P8 boundary permits that limited claim; it changes no criterion or other required improvement. Reopen with a materially different eligible baseline or context, or an explicitly approved prospective study, never favorable resampling.
- One LLM scorer family grades every run. Its own run-to-run SD is about 1.3 failures per run, and a change of scorer model or scorer prompt invalidates these parameters.
- The variance was measured on Claude Sonnet 5.5 runs of five scenarios at one skills revision. Six Codex runs per scenario on the same revision gave a pooled within-scenario SD of 1.31 failures per run (df 16, from the 20 scored runs), well below the Claude estimate, so the Claude-derived floors are conservative for Codex; they stay in force because the Codex evidence comes from fewer scored runs and one revision.
- The equal-candidate probabilities come from a simulation with normal, independent draws at the measured SDs, not from repeated real comparisons.
- Codex runs depend on the Codex CLI version and on what its disposable home loads. Each run records the version and the rendered context. For delivery runs through `runner.codex_run`, including bounded coverage, the freshly observed version, exact export commit and skills digest, model and reasoning effort must match a prior passing isolation probe with no findings. Missing, failed, incomplete or mismatched evidence stops before an agent turn; a later probe cannot qualify earlier runs. The separate investigation route observes the version and checks rendered context before its agent turns without requiring a prior probe. The delivery check does not certify the executable remains unchanged during a run.
- Claude calls use the process owner's explicit OS/login allowlist and documented authentication-only ANTHROPIC_API_KEY when present, with account connectors forced off and desktop/session/messaging context stripped. Real HOME retains existing local OAuth discovery; no Claude credential files are copied and only passed variable names are recorded. The live login smoke covers that OS/local OAuth path, not every authentication provider. Each agent run records Claude Code's own count of skills by source and stops if any skill comes from outside the exported plugin and its bundled set. Both comparison sides must use the same clean instrumentation; inherited-host baseline records do not establish clean-baseline evidence. The historical variance measurements above used inherited host context; current repeated comparisons supply observed clean-context variance without changing the frozen scorer or checklist.
- Board seeding uses the current MCP tool schemas. An older revision whose MCP contract differs cannot be seeded by this harness; that is an evidence limit, not something compatibility code repairs.
- Worlds are a small synthetic project. Codex worlds must sit outside the system temporary directories.
- The parameters were measured on five-scenario sets and carried to the three-scenario Codex set by holding thirty runs per side. A set with fewer scenarios leans on very few scenario means, however many runs each has.

## When to re-derive

Re-derive the parameters, recording the new measurements here, when new repeated runs change the measured variance, when the checklist, scorer model or scorer prompt changes, when the scenario set changes size or cost profile materially, or when more Codex repeated runs are measured.
