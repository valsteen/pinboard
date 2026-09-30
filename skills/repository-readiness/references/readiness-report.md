# Readiness report contract

Produce a human-consumed Markdown report identified by `Format: agentic-readiness-report/v1`. Present it in the conversation until the human chooses a save path, normally outside tracked files. Assessment is read-only on the repository. No canonical JSON twin is required until a tool consumes the report.

## Bind the report to its evidence

Record the repository identifier, full assessed commit, date, selected boundary, representative entry point and traced neighbors or whole-repository coverage, and relevant working-tree differences. A commit alone does not identify dirty evidence. State exclusions and whether each claim is static, observed, or inferred.

Record the project tier as **proposed**, or **confirmed** with its decision owner and current project-policy selector. Explain its evidence and reopening condition. A report proposes policy; it does not create policy or write a tier into the Pinboard ledger.

Findings are immutable observations of the assessed state. Actions cite findings and describe work units. Decisions identify a user or team owner; an agent cannot settle them by converting the report to work. Updated evidence produces a new dated finding rather than silently rewriting the original observation.

## Report shape

Use the following sections, scaled to the selected boundary:

- **How to act on this report:** include the consumer instructions below, including explicit selected-action authority.
- **Summary:** one row per capability with its status, evidence strength, and main action or no repair needed. Use the statuses from [agentic-readiness.md](agentic-readiness.md), without a score or certification.
- **Decisions needed:** stable `D1` identities, one concrete question, user or team owner, recommendation, alternatives, and practical consequences.
- **Actions:** stable `A1` identities and imperative titles; each carries the fields below.
- **Findings:** stable `F1` identities, capability, status, observation, repository-relative evidence selectors, consequence, unanswered question, and repair owner. Carry relevant authority → consumers → projections → validation relationships and acknowledged limitations here.

Each action records:

| Field | Meaning |
| --- | --- |
| Kind | `direct-fix`, `needs-decision` with decision identities, or `project` for larger work requiring preparation |
| Size | `S`, `M`, or `L`, relative to other actions in this report; no unsupported time estimate |
| Depends on | Action identities, or none; distinguish execution dependencies from open decisions |
| Resolves | Finding identities |
| Change | Concrete product or repository effect and repository-relative owners |
| Done when | Observable outcome and relevant existing validation command; at least one explicit obligation with an identity and `allowed` or `forbidden` deferral policy |
| Recheck | Exact underlying finding and assumption to verify at the consumer's current commit before any mutation |
| Urgency | Observed consequence or explicit absence of urgency evidence |
| Checkout policy | `main`, `isolated`, or `coordinator-selected`; use the last unless the human has selected otherwise |

Use repository-relative evidence paths and selectors. Do not include absolute paths, pasted private logs, or secrets. Describe an external report's locator relative to an explicitly named report bundle; distinguish that bundle from the assessed repository so a ticket consumer does not mistake it for a repository file.

## Consumer instructions

The report's “How to act on this report” section must tell any agent:

1. Treat this report as assessment evidence. Obtain explicit authority for the selected actions and intended destination before saving proposals or tickets, implementing fixes, publishing a PR, or changing project policy. Report generation alone authorizes none of those effects.
2. Recheck each selected action against the current commit and working tree. Skip an action whose findings no longer hold, and preserve changed or unresolved evidence. Resolve its open decisions through the named owner.
3. For a project already using Pinboard, offer to save the human's selected actions with `$pinboard-intake` and the mapping below. Never save automatically or initialize Pinboard implicitly. Saving proposals authorizes no implementation.
4. For tickets, create one per selected action only when that external write is authorized. Use its title, change, done-when, recheck, and cited findings; link dependency tickets.
5. For one PR, implement only selected authorized `direct-fix` actions with resolved decisions and dependencies. Keep work units reviewable, run their checks, and list resolved findings in the description. Obtain any missing commit or publication authority before those effects.
6. Leave `needs-decision` work pending its owner's answer. Larger `project` work needs its accepted scope and preparation; report conversion does not bypass them.

## Convert selected actions to proposals

Inspect the connected tool's current proposal contract before conversion. `pinboard-proposal/v2` has the following required fields. Every text value must be a single line, contain no `|`, and have no leading or trailing whitespace. Condense prose without deleting decisions; deduplicate evidence, freshness assumptions, and obligation identities while retaining their order.

| Proposal field | Source and meaning |
| --- | --- |
| `schema` | `pinboard-proposal/v2` |
| `proposal_id` | Unique kebab-case identity chosen at authorized intake |
| `created_at` | Actual intake timestamp with timezone, not the assessment date |
| `source_task_id` | Trusted identity of the task performing intake, not the report author or an invented historical task |
| `user_label` | Action title |
| `trigger` | Human's actual selected-action request and report/action identity; distinguish intake request from assessment origin |
| `evidence` | First the resolvable report-bundle locator and action/finding selectors, then cited repository-relative evidence; preserve report origin truthfully |
| `why_it_matters` | Cited findings' consequences and affected capability |
| `relation` | Execution dependency mapping below; never encode an unanswered decision as a dependency item |
| `effect` | Action's Change |
| `unlock` | Action's observable Done when |
| `urgency_evidence` | Observed urgency; explicitly say when no failure or timing evidence was observed |
| `freshness_assumptions` | Assessed commit, dirty-evidence qualification, findings, and action Recheck |
| `checkout_policy` | Action's selected policy, otherwise `coordinator-selected` |
| `obligations` | At least one Done-when obligation with unique kebab-case `obligation_id`, single-line `statement`, and `deferral_policy` `allowed` or `forbidden` |

`position` is optional and one-based; omit it unless queue placement was requested. Do not invent a report path if the report exists only in conversation. Before intake, either preserve the report at the human's chosen location or use a genuine retrievable conversation selector; identify that provenance separately from repository evidence. The current intake task remains `source_task_id` in either case.

For an authorized selection, save dependency actions first. If A2 depends on A1 and A1 already exists as an item, A2 uses `follow-up` with A1's actual item identity. If a new A1 must block an existing live A2 item, A1 uses `prerequisite` with A2's actual identity. Without a related item, use `independent` with JSON `null`; unresolved required dependencies must be settled before saving the dependent action as executable work. The proposal relation names one item: do not silently discard additional dependencies or place an action identity in `relation.item`. Resolve the remaining dependency graph through the existing accepted-definition workflow or ask its owner before persistence. Other relation kinds retain their current intake semantics.
