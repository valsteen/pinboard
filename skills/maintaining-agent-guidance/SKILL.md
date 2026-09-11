---
name: maintaining-agent-guidance
description: Use when establishing or evaluating a project's durable AI-facing guidance, recommending what guidance it needs, resolving unclear ownership, or untangling duplicated or conflicting instructions. Do not use for routine wording, link, or metadata corrections when the authoritative owner is already settled.
---

# Maintaining Agent Guidance

Place each durable proposition at the smallest authoritative owner that can prevent the problem or answer the recurring decision. Keep complementary layers distinct and avoid duplicated policy.

## Take the settled-owner fast path

When the requested change is a bounded correction and its owner is already clear:

1. Inspect nearby guidance and any thin route that could become inconsistent.
2. Edit the authoritative owner and reconcile only directly affected references.
3. Validate the changed surface.

Stop there. Do not reopen the full ownership question merely because durable guidance changed. Use the remaining workflow when the edit introduces a proposition, challenges placement, exposes duplication, or changes who needs the information.

## Establish or assess a project baseline

When asked to onboard a project, evaluate its guidance, or recommend improvements, inspect the current authorities and real project evidence before proposing a baseline. Unless the user asks for edits, remain read-only.

Look for the smallest evidence-supported set of guidance:

- thin scoped routes to the relevant authorities;
- canonical human-facing truth for stable architecture, product behavior, and contributor knowledge;
- mechanical enforcement for objective invariants;
- non-obvious local constraints that relevant work must know;
- supported commands or entry points that contributors and agents need to find; and
- deliberate no-artifact decisions for local, temporary, obvious, or unproven concerns.

Do not install generic boilerplate, repeat notices across layers, or change the project automatically. Return a compact recommendation that identifies each proposed proposition, its authoritative owner, any conflict or stale copy to reconcile, any thin route needed for discovery, and any deliberate no-artifact decision.

## Start from the problem or decision pressure

Describe the evidence before drafting guidance:

- What behavior, ambiguity, repeated rediscovery, navigation cost, contradiction, or maintenance pressure was observed?
- What visible consequence followed or is likely to recur?
- Is the concern cross-task and durable, or local and temporary?
- Which code, tool, test, document, instruction, or skill already partly owns it?

Do not manufacture an agent-failure narrative for a structural or human-facing problem. Generalize only the decision future work needs.

Trace each new durable proposition to the user's request, an existing project authority, or evidence of a real recurring need. Treat unsupported discoveries as recommendations or questions, not new mandatory guidance.

## Separate propositions from delivery layers

Split the proposed guidance into propositions precise enough to own. Give each proposition one authoritative owner.

Complementary layers may coexist when they perform distinct jobs:

- **Enforcement** makes an invariant mechanically true through code, types, tests, validation, or tooling.
- **Canonical truth** explains stable architecture, domain behavior, or contributor knowledge for humans and agents.
- **Automatic routing** uses scoped `AGENTS.md` guidance to point every relevant task at the authority or state a boundary-specific constraint.
- **Specialization** uses a skill for a triggerable cross-project workflow or judgment pattern.

Keep routes and specializations thin when another layer owns the proposition. Multiple files are not duplication merely because they cooperate; duplication exists when they independently restate or can disagree about the same proposition.

## Choose the durable owner

Prefer the earliest owner that can prevent or resolve the concern:

1. **Code, types, names, or module boundaries** when the implementation permits the wrong state or hides its meaning.
2. **Automation, lint, validation, tests, or templates** when the rule is mechanical and objectively checkable.
3. **Public contributor or architecture documentation** when humans and agents need stable project truth.
4. **The nearest scoped `AGENTS.md`** when a constraint or route should apply automatically within that repository boundary.
5. **A reusable skill** when a triggerable workflow or judgment pattern applies across projects or should load on demand.
6. **The task brief or no durable artifact** when the fact is local, temporary, readily reversible, or not shown to recur.

Do not use prose to compensate for a misleading API, missing type, broken command, or enforceable invariant. Do not put project facts in a global skill merely because an agent encountered them.

When agents are expected to adopt a schema, vocabulary, filename, or entry point, give that interface a deliberate owner and identify a real producer or consumer. Do not create an agent-facing convention that no supported workflow uses.

When repeated agent failures come from discovering or recovering a mechanical operation, repair the executable interface before adding more prose. A weak-model-friendly boundary exposes the exact supported operation, strict input or starter, observable effect, stable mismatch facts, retry safety, and bounded next action without source archaeology. Keep the residual limitation explicit when correctness still depends on semantic source selection, independent review, or a runtime capability the tool cannot provide.

## Minimize context and maintenance cost

- Prefer updating an existing authority over adding another copy.
- Make automatic guidance earn its recurring context cost; prefer scoped placement and precise triggers.
- Colocate change-sensitive guidance with the implementation or automation that invalidates it.
- Improve human navigation when possible instead of adding an agent-only layer.
- Record a reopen condition only when a concrete future observation would justify deferred guidance.

## Make the coherent change

1. State the desired future behavior in observable terms.
2. Update the authoritative owner, then remove or narrow stale wording in directly affected layers.
3. Keep thin routes short and say exactly when or why to consult the authority.
4. Preserve product vocabulary and useful distinctions; do not hide a product decision behind workflow terminology.
5. Prefer no durable artifact when the evidence does not justify recurring guidance.

When guidance repeatedly fails under an observed decision pressure, make the correction a falsifiable loop. Name the one future decision or output that must change, place that proposition at the smallest established owner, and replay a representative scenario that preserves the motivating pressure. Stop when the replay produces the intended decision without a new contradiction. If it does not, widen only to the earliest effective owner the replay demonstrates; widen to an executable interface specifically when the failure is discovering or recovering a mechanical operation.

## Validate proportionately

Where relevant, check syntax, metadata, links, triggering language, contradictions, thin routes, and the resulting diff. Re-read the motivating problem against the revised ownership: could a fresh contributor or agent find the authority and make the intended decision?

When guidance claims a workflow or architecture, trace one representative path from its supported entry point through the named authority to a real consumer or effect. Validate mechanical structure and observable behavior; do not freeze prose semantics with tests that merely require or forbid wording.

Run one representative scenario when the change materially shapes judgment and inspection is insufficient. Compare with a no-guidance baseline or use repeated samples only when causal attribution, consequence, or model variance warrants it.

Finish when every proposition has an authoritative owner or a deliberate no-artifact decision, complementary layers have distinct roles, active contradictions are removed, the trigger is discoverable, and validation matches the risk.
