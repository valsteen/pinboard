"""The cheap Codex isolation probe that must pass before any paid Codex run.

In a fresh isolated home it renders the model-visible context without a model call, checks that only the exported
skills, the exported Pinboard MCP server and the probe project's ``AGENTS.md`` load, then spends two tiny turns
(``codex exec`` and ``codex exec resume``) to prove one thread survives resumption, that a Pinboard MCP tool runs
without an approval prompt, and to observe token accounting.
A failed isolation check spends nothing.
"""

import asyncio
import tempfile
from pathlib import Path

from evals.behavioral import board, codex_driver, credentials, processes, world
from evals.behavioral.claude_driver import now
from evals.behavioral.layout import Layout
from evals.behavioral.records import ExportRecord, InventoryEntry, ProbeRecord, Runtime, write_new
from evals.behavioral.runner import CredentialConflictError, require_codex_world_location
from evals.behavioral.spend import Budget, Category

PROBE_TURNS = (
    "Reply with exactly: OK",
    "Call the pinboard_overview tool of the pinboard MCP server once, with project_root set to the current "
    "directory's absolute path and work_root set to its .pinboard subdirectory, then reply with exactly: OK again",
)


def probe_codex(
    layout: Layout,
    budget: Budget,
    export: ExportRecord,
    name: str,
    model: str,
    reasoning_effort: str,
    worlds: Path,
    credential_source: Path,
) -> ProbeRecord | None:
    require_codex_world_location(worlds)
    codex_driver.price(model)
    projected = budget.reserve(Category.PROBE)
    if projected is None:
        return None
    directory = layout.probe_file(name).parent
    directory.mkdir(parents=True, exist_ok=False)
    root = worlds / f"probe-{name}"
    root.mkdir(parents=True, exist_ok=False)
    built = world.World(
        root=root,
        project=root / "tally",
        origin=root / "origin.git",
        scratch_board=None,
        launcher=Path(export.plugin_root) / "scripts" / "pinboard",
        window=budget.window,
    )
    world.create_project(built, Runtime.CODEX)
    world.init_board(built.launcher, built.project, None, budget.window)
    findings: list[str] = []
    entries: list[InventoryEntry] = []
    cost: float | None = 0.0
    try:
        with (
            credentials.exclusive_codex_session(Path(tempfile.gettempdir()), budget.window),
            credentials.isolated_home(credential_source, None, budget.window) as home,
        ):
            codex_driver.write_config(home.path, Path(export.plugin_root), model, reasoning_effort, budget.window)
            context = codex_driver.loaded_context(home.path, built.project, budget.window)
            (directory / "prompt-input.txt").write_text(context.prompt_text)
            entries = [
                *context.entries,
                InventoryEntry(kind="runtime-bundled", name="sandbox_mode", source=context.sandbox_mode),
                InventoryEntry(kind="runtime-bundled", name="approval_policy", source=context.approval_policy),
                *(
                    InventoryEntry(kind="runtime-bundled", name="writable-root", source=root_path)
                    for root_path in context.writable_roots
                ),
                InventoryEntry(
                    kind="runtime-bundled", name="cli-version", source=codex_driver.codex_version(budget.window)
                ),
            ]
            findings = codex_driver.isolation_findings(context, home.path / "plugins" / "cache", built.project)
            entries.extend(served_tools(home.path / "plugins" / "cache", root / "mcp.log", budget.window))
            if not findings:
                complete = False
                try:
                    cost, turn_findings = probe_turns(home.path, built.project, model, directory, budget.window)
                    findings.extend(turn_findings)
                    complete = not turn_findings
                finally:
                    rollout = credentials.without_login(codex_driver.rollout_text(home.path), home.copied)
                    (directory / "rollout.jsonl").write_text(rollout)
                    write_new(directory / "accounting.json", codex_driver.rollout_accounting(rollout, model, complete))
        settlement = home.settlement
        entries.append(
            InventoryEntry(
                kind="runtime-bundled",
                name="credential-settlement",
                source=settlement.value if settlement else "unsettled",
            )
        )
        record = ProbeRecord(
            schema="pinboard-behavioral-probe/v1",
            name=name,
            runtime=Runtime.CODEX,
            description=f"codex isolation probe at {now()}: rendered context, then two tiny resumed turns",
            cost_usd=cost,
            passed=not findings,
            findings=findings,
            inventory=entries,
        )
        write_new(layout.probe_file(name), record)
        if settlement is credentials.CredentialSettlement.REFUSED_SOURCE_CHANGED:
            raise CredentialConflictError("~/.codex/auth.json changed during the probe; nothing was written back")
        return record
    finally:
        budget.release(projected)


def served_tools(plugin_cache: Path, log: Path, window: processes.Window) -> list[InventoryEntry]:
    """Start each installed plugin copy's own launcher as Codex would and list the MCP tools it serves."""
    return [
        InventoryEntry(kind="mcp-server", name=f"served tool {name}", source=str(launcher))
        for launcher in sorted(plugin_cache.glob("*/*/*/scripts/pinboard"))
        for name in asyncio.run(board.tool_names(launcher, log, window))
    ]


def probe_turns(
    home: Path, project: Path, model: str, directory: Path, window: processes.Window
) -> tuple[float | None, list[str]]:
    """Send the probe turns; the probe costs its thread's final cumulative usage at the recorded list price."""
    findings: list[str] = []
    cumulative: codex_driver.Usage | None = None
    thread: str | None = None
    for index, text in enumerate(PROBE_TURNS, start=1):
        completed, reading = codex_driver.run_turn(
            home, project, thread, text, directory / f"turn-{index}.jsonl", window
        )
        if reading.usage is None:
            findings.append(f"turn {index} reported no token usage: {completed.stderr.strip()[:500]}")
        else:
            cumulative = reading.usage
        if thread is not None and reading.thread_id not in (None, thread):
            findings.append(f"turn {index} started thread {reading.thread_id} instead of resuming {thread}")
        thread = thread or reading.thread_id
        if thread is None:
            findings.append(f"turn {index} reported no thread")
            break
        if completed.returncode != 0 or reading.errors:
            findings.append(f"turn {index} failed: {'; '.join(reading.errors)[:500]} {completed.stderr.strip()[:500]}")
        findings.extend(f"turn {index} permission denial: {denial.tool}: {denial.detail}" for denial in reading.denials)
        if index == len(PROBE_TURNS) and "pinboard.pinboard_overview" not in reading.mcp_calls_completed:
            findings.append(f"turn {index} completed no pinboard_overview call without approval")
        if reading.usage is None or completed.timed_out or completed.returncode != 0:
            break
    return (None if cumulative is None else codex_driver.turn_cost(model, cumulative)), findings
