# Local work-computer trial

Choose one private local trial home outside evidence-source repositories. Give each inquiry its own subdirectory and human-readable `<inquiry-id>/inquiry.md`; the shared `trial.jsonl` in the trial home records sessions from all inquiries in order. A fresh session first reads the selected inquiry's note; the collector is supporting trial evidence, not the source of truth for the investigation. Keep private locators and excerpts in the local note. Use neutral IDs in collector records so a return draft can preserve relations without copying originals.

For each session, have the agent write one JSON input file in the home and run:

```sh
uv run --locked python skills/investigation-focus/scripts/trial.py record --home /chosen/trial-home --input /chosen/trial-home/session.json
```

Run this from a checkout at the exact candidate commit. Use a distinct `inquiry_id` for each inquiry. A `session_id` may repeat in another inquiry, but not in the same one. `context` is `fresh` or `resumed`; `candidate_commit` is the exact 40-character Git commit installed for that session, even if a branch later advances. The session record also names model and reasoning setting, the local note revision, source revisions and windows, independently revised outputs and the exact source revisions and windows they use, human interventions, observed failures, available cost, and missing or withheld coverage. Use `null` for unavailable cost; do not invent zero. The `local_note` points to private context and is omitted from the return draft. Keep summaries and IDs safe to show or redact them before return.

The input shape is:

```json
{
  "schema": "investigation-trial-session/v1",
  "local_note": "harbor/inquiry.md revision 1",
  "session": {
    "inquiry_id": "harbor",
    "session_id": "morning-1",
    "context": "fresh",
    "candidate_commit": "0000000000000000000000000000000000000000",
    "model": "selected-model",
    "reasoning": "selected-setting",
    "sources": [{"source_id": "dashboard-a", "revision": "snapshot-1", "window": "09:00-09:30Z"}],
    "outputs": [{"output_id": "technical-note", "revision": "1", "sources": [{"source_id": "dashboard-a", "revision": "snapshot-1", "window": "09:00-09:30Z"}]}],
    "interventions": [{"kind": "direction", "summary": "Human chose diagnosis"}],
    "failures": [],
    "coverage": [{"source_id": "queue-dashboard", "status": "inaccessible", "summary": "Access unavailable"}],
    "cost_usd": null,
    "cost_basis": "unavailable"
  }
}
```

After several sessions, prepare a local return draft:

```sh
uv run --locked python skills/investigation-focus/scripts/trial.py export --home /chosen/trial-home
```

The command writes `trial-return-draft-N.json` in the selected home, where N is the number of recorded sessions, and refuses to overwrite an earlier draft. It omits the private `local_note` field, preserves global sequence and per-inquiry attribution, and never reads source files or sends data over a network. **This draft is not automatically safe to transfer.** The human reviews every ID, window, summary, and cost for confidential content, removes or generalizes what should stay local, and may add selected sanitized excerpts or output relations. Only that reviewed package is returned to the development item. Note withheld context and independently uncheckable local assessments explicitly. A later session or changed candidate adds another record; do not overwrite earlier sessions.
