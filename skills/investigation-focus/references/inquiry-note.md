# Inquiry note example

This is a caller-owned, human-readable note in the selected inquiry home. Its IDs connect observations, human decisions, and output revisions; the agent maintains it across bounded passes. Keep original locators and short excerpts here when permitted. The trial collector uses neutral IDs and does not replace this note.

```markdown
# Harbor checkout latency — inquiry harbor

Purpose: Determine what can be said about the slowdown and what evidence would change the diagnosis.
Current human direction: Investigate upstream retry amplification; prepare a leadership summary. Agreed in session 2, superseding the first pass's deployment diagnosis focus.

## Pass 1 — diagnosis, 45 minutes

Sources checked:
- E1 observed: Latency dashboard snapshot 1, 09:00–09:30Z, [direct locator]. p95 rose after deployment. The window covers one region.
- E2 observed: Trace sample 1, 09:12Z, [direct locator and short excerpt]. One slow request spent time waiting upstream.
- E3 observed: Deployment diff revision abc, [direct locator]. It did not change the slow operation.

Inference: The deployment's timing and E1 correlate; causation remains unproven. E3 weakens the direct-code-change lead.
Dismissed lead: Direct deployment-code cause, based on E3. Reopen only if another affected code path is identified.
Coverage: Queue dashboard inaccessible; team note discoverable but unsearched at the bound.
Stop: Time bound reached. Next question: Did retry load or backlog change in a comparable window?
Output: Technical note T1 revision 1 uses E1–E3 and this uncertainty. Its own file is [local path].

## Pass 2 — corrected direction, 30 minutes

Human correction: Investigate upstream retry amplification and prepare a leadership summary. Keep pass 1 observations intact.
Sources checked:
- E4 observed: Queue snapshot 2, 10:00–10:30Z, [direct locator]. Backlog rose. This is later than E1 and cannot by itself explain E1's window.
- E5 observed: Retry-policy record revision 2, [direct locator]. It lists a changed retry limit.
- E6 observed: Team note revision 1, [direct locator]. It says no retry change occurred. E5 and E6 disagree.

Inference: Retries may have amplified load. The disagreement and mismatched windows prevent a causal conclusion.
Human acknowledgement needed: Confirm whether E5 is the applied policy and whether a comparable 09:00–09:30Z queue window exists.
Coverage: Original-window queue snapshot still unavailable.
Stop: Necessary source unavailable. Next question: Can the owner provide the applied policy and original-window queue snapshot?
Output: Leadership summary L1 revision 1 uses E1, E4–E6 and the pass 2 human direction. Technical note T1 remains revision 1 until deliberately revised.

## Output revisions after the second pass

- T1 revision 2 adds E4's later backlog window and keeps the original deployment inference qualified; L1 stays at revision 1.
- After the human acknowledges the E5–E6 disagreement, L1 revision 2 records that acknowledgement and narrows its requested measurement; T1 stays at revision 2. The acknowledgement does not rewrite E5 or E6.
```

In a fresh session, read the whole current note before using the latest conclusion. Add a new observation ID for a changed source window or revision. Correct an earlier interpretation by referring to it; keep the earlier observation and human agreement visible. Record each output revision separately so a new audience or draft does not silently rewrite another output's basis.
