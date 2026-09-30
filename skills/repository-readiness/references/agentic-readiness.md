# Agentic readiness

Assess whether a capable agent arriving without conversation history can understand, operate, change, diagnose, and recover the selected product path while recognizing human-owned decisions. Use the skill's representative boundary by default; a whole-repository assessment requires explicit selection. Trace each answer from semantic authority through consumers and projections to validation. Repository evidence describes what is supported, not what a generic checklist says must exist.

## Match effort to the project's effects

Propose a tier from the state the project owns, the actors and processes sharing it, and the cost of undoing a mistake. The following rows are candidate expectations for discussion, not automatic acceptance obligations. Confirm the tier with the human before recording it as project policy during authorized improvement. An equivalent existing policy may already settle the needed guarantees.

| Project shape | Candidate readiness expectations |
| --- | --- |
| Script or stateless tool | Plain overview, reproducible run and checks, meaningful outputs and exit codes |
| CLI or library owning files or local state | Also validated boundary shapes, truthful effect and retry failures, full validation |
| Stateful service or long-running process | Also transactional effects, durable history, structured correlated diagnostics, one diagnostic read, recoverable backups |
| State shared across machines or people | Also version agreement, per-actor attribution, previewable and journaled procedures for migrations and moves |

State the evidence for the proposed row and the observable change that would justify reconsidering it. Size each capability check to actual ownership: a stateless script need not acquire a procedure journal, and a local tool need not gain remote leases. The method does not select the optional engineering-health baseline or authorize hardening.

## Check the seven capabilities

### 1. Intent is legible

Find current product purpose, users, core flows, non-goals, and limitations at their established owners. Distinguish accepted requirements from implementation facts and inferred intent. Limitations explain their consequences and reopening conditions; unresolved intent names a human decision.

**Check:** can a fresh agent explain what the selected behavior must do and classify a surprise as a defect, deliberate limitation, or open question without reconstructing delivery history?

### 2. Structure tells the same story

Trace one representative flow from entry point to decisions, conversions, effects, and validation. Compare that order with the product overview. Check whether names truthfully promise provenance, timing, durability, and authority. Identify generated projections and their producers, and use the architecture map to locate owners and dependency direction. Tests should establish observable behavior rather than freeze implementation constants.

**Check:** can a fresh agent find the owner of one user-visible behavior and predict the necessary edits to add a sibling without discovering a second copy of the same decision?

### 3. The program is operable

Find a reproducible setup command, run command, and check command or short documented set aligned with CI. Verify declared toolchains and locked dependencies where the project uses them. Look for deterministic checks and a supported way to drive the real program with fixtures or controlled external systems. Identify ambient prerequisites and configuration fallbacks that the supported setup does not explain.

**Check:** can a fresh agent set up, run, and check the selected path, and tell what prerequisite or supported operation a setup failure requires?

### 4. Inputs and outputs are actionable

Inspect supported entry points, boundary shapes, operation discovery, and expected failures. Where agents consume machine data, find the declared schema and its owner; distinguish canonical data from human-readable projections. A failure should expose observations, changed state and surfaces, retry safety and form, and one safe next step or exact human decision. Stable codes carry meaning independently of prose. Never infer safe replay from a generic failure after a possible commit.

**Check:** given the selected result without source code, can a fresh agent determine what happened, whether anything changed, and how to continue?

### 5. Effects are safe to perform

Use the project's supported state and contention model to assess stale-action rejection, transaction boundaries, and held authority. Inspect resource exclusion only where the operation actually requires it. For multi-step effects, trace the exact previewed plan, commit point, durable step identities and next step, interruption states, and idempotent continuation. Check retention, backups before migration, and reversal through the supported procedure. Do not infer a guarantee from independently constructed fixtures.

**Check:** after interruption at a meaningful boundary, can a fresh agent identify the valid authority and safely leave the previous state intact or finish the committed procedure?

### 6. Events can be reconstructed and recovery explained

Find the history and diagnostic evidence the program owns. Inspect actor attribution, before and after identities, stable event codes and their catalog, cross-process correlation where needed, and truthful effect and retry disposition. Determine whether one diagnostic read explains configuration selection, versions, unfinished procedures, recent events, and relevant dependency or backup health. Distinguish ordinary diagnostics from full validation. Check retention and explicit opt-in for exact payload capture; never paste private payloads into the report. Recovery should use ordinary supported operations, with honest limits for adjacent state the program cannot repair.

**Check:** from one symptom, can a fresh agent explain what happened and name a supported recovery, or identify exactly which evidence is missing?

### 7. Human authority is explicit

Find the boundaries for product intent, material limitations, scope expansion, irreversible effects, external actions, and deletion of retained data. Determine which implementation choices the agent may make and which require a decision owner. Keep a decision question concrete enough that the human can evaluate its consequences. Procedural complexity may remain with the agent while the human surface stays simple and safe.

**Check:** can a fresh agent distinguish an authorized next action from a human-owned decision and ask one useful question without expanding the task?

## Record evidence, uncertainty, and repair ownership

For every capability, use exactly one status within the declared boundary:

- **answered from the repository:** established owners and evidence answer the check; state whether evidence is static or experimentally observed;
- **needed guessing:** partial, conflicting, or inferred material leaves a named question unresolved;
- **absent:** the selected search found no answer; record what was searched, rather than claiming repository-wide absence from a representative scan.

Record evidence, the unanswered question if any, practical consequence, and the owner of the smallest durable repair. Preserve useful structures whose purpose is supported. Repair the earliest misleading owner: implementation, current-story documentation, then recurring guidance. A proposed repair remains an action, not permission to change anything.

Use [readiness-report.md](readiness-report.md) for the portable findings, actions, and decisions contract. Do not produce a score or certification, copy another project's limitations, or retrofit runtime guarantees merely to satisfy these checks. Stop when the selected boundary has an evidence-backed answer or explicit uncertainty for every capability.
