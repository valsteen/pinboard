"""Codex agent-under-test driver: one fresh ``codex exec`` thread per run, resumed for every scripted turn.

Each run uses its own isolated home (see ``credentials``) whose harness-written ``config.toml`` names the explicit
model and reasoning effort, disables memories, bundled system skills, account apps and remote plugins, sets
approval policy ``on-request`` with separate ``auto_review`` and the documented Pinboard permission profile (extends ``:workspace`` and reopens only
the project's ``.pinboard``), and declares the exported revision as a local plugin marketplace whose ``pinboard``
plugin supplies the skills and the Pinboard MCP server. Only that server's own tools are pre-approved; approval
requests still receive independent runtime review. The harness never adds ``--add-dir``, another profile or
``--dangerously-bypass-approvals-and-sandbox``.

``codex exec --json`` output is an accepted externally owned protocol boundary: each consumed field is validated
and unrelated additive fields or event kinds are ignored. Non-final agent messages of a turn are its progress
commentary; the last agent message is the final reply. A turn's ``turn.completed`` usage is the thread's cumulative
total, so a turn's own usage and cost are the difference from the previous turn's total.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import msgspec

from evals.behavioral import processes
from evals.behavioral.claude_driver import now
from evals.behavioral.records import (
    CodexAccounting,
    CodexRunDetails,
    Hook,
    InventoryEntry,
    PermissionDenial,
    ReviewerUsage,
    TurnEvidence,
)

TURN_TIMEOUT_SECONDS = 5400
PERMISSION_PROFILE = "pinboard"
APPROVAL_POLICY = "on-request"
MARKETPLACE_FILE = Path(".agents") / "plugins" / "marketplace.json"
PRICE_SOURCE = (
    "OpenAI API standard list price, short context, per 1M tokens (https://developers.openai.com/api/docs/pricing, "
    "retrieved 2026-09-29): gpt-6-sol input 2.00, cached input 0.20, cache writes 2.50, output 10.00 USD; Codex "
    "reports input tokens including cached and cache-write tokens, and output tokens including reasoning tokens"
)
DENIAL_PATTERN = re.compile(r"Operation not permitted|Read-only file system|Permission denied|sandbox", re.IGNORECASE)
GIT_PATTERN = re.compile(
    r"(^|[\s/'\"])(\.git|origin\.git)([\s/'\"]|$)|\bgit\s+(commit|merge|push|worktree|checkout|branch|fetch|pull|cherry-pick|rebase|reset|tag|add)\b"
)


@dataclass(frozen=True)
class Price:
    uncached_input: float
    cache_write: float
    cached_input: float
    output: float


PRICES_PER_MILLION = {"gpt-6-sol": Price(uncached_input=2.00, cache_write=2.50, cached_input=0.20, output=10.00)}


class CodexUnavailableError(Exception):
    """Codex cannot be run as the harness requires: no recorded list price or plugin installation failed."""


class CodexStreamError(Exception):
    """A resumed thread reported cumulative token usage below the previous turn's, so the turn's own usage is unknown."""


class Event(msgspec.Struct, frozen=True):
    type: str


class ThreadStarted(msgspec.Struct, frozen=True):
    thread_id: str


class ItemKind(msgspec.Struct, frozen=True):
    type: str


class ItemEvent(msgspec.Struct, frozen=True):
    item: ItemKind


class AgentMessage(msgspec.Struct, frozen=True):
    text: str


class AgentMessageEvent(msgspec.Struct, frozen=True):
    item: AgentMessage


class CommandExecution(msgspec.Struct, frozen=True):
    command: str
    aggregated_output: str
    exit_code: int | None
    status: str


class CommandEvent(msgspec.Struct, frozen=True):
    item: CommandExecution


class McpError(msgspec.Struct, frozen=True):
    message: str


class McpCall(msgspec.Struct, frozen=True):
    server: str
    tool: str
    status: str
    error: McpError | None


class McpCallEvent(msgspec.Struct, frozen=True):
    item: McpCall


class FileChange(msgspec.Struct, frozen=True):
    status: str


class FileChangeEvent(msgspec.Struct, frozen=True):
    item: FileChange


class Usage(msgspec.Struct, frozen=True):
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    @property
    def uncached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cached_input_tokens - self.cache_write_input_tokens, 0)


class TurnCompleted(msgspec.Struct, frozen=True):
    usage: Usage


def usage_since(previous: Usage | None, cumulative: Usage) -> Usage:
    """One turn's own usage: ``turn.completed`` reports the thread's cumulative totals, also after ``exec resume``."""
    if previous is None:
        return cumulative
    turn = Usage(
        input_tokens=cumulative.input_tokens - previous.input_tokens,
        cached_input_tokens=cumulative.cached_input_tokens - previous.cached_input_tokens,
        cache_write_input_tokens=cumulative.cache_write_input_tokens - previous.cache_write_input_tokens,
        output_tokens=cumulative.output_tokens - previous.output_tokens,
        reasoning_output_tokens=cumulative.reasoning_output_tokens - previous.reasoning_output_tokens,
    )
    if (
        min(
            turn.input_tokens,
            turn.cached_input_tokens,
            turn.cache_write_input_tokens,
            turn.output_tokens,
            turn.reasoning_output_tokens,
        )
        < 0
    ):
        raise CodexStreamError(f"cumulative usage {cumulative} fell below the previous turn's {previous}")
    return turn


class PromptPart(msgspec.Struct, frozen=True):
    type: str


class PromptText(msgspec.Struct, frozen=True):
    text: str


class PromptMessage(msgspec.Struct, frozen=True):
    content: list[msgspec.Raw]


class McpTransport(msgspec.Struct, frozen=True):
    command: str
    args: list[str]
    cwd: str | None


class McpServer(msgspec.Struct, frozen=True):
    name: str
    enabled: bool
    transport: McpTransport


@dataclass
class TurnReading:
    thread_id: str | None
    messages: list[str]
    usage: Usage | None
    denials: list[PermissionDenial]
    git_write_denied: bool
    mcp_approval_denied: bool
    mcp_calls_completed: list[str]
    errors: list[str]


def toml_string(value: str) -> str:
    return json.dumps(value)


def marketplace_name(plugin_root: Path) -> str:
    class Marketplace(msgspec.Struct, frozen=True):
        name: str

    return msgspec.json.decode((plugin_root / MARKETPLACE_FILE).read_bytes(), type=Marketplace).name


def write_config(home: Path, plugin_root: Path, model: str, reasoning_effort: str, window: processes.Window) -> None:
    marketplace = marketplace_name(plugin_root)
    text = f"""model = {toml_string(model)}
model_reasoning_effort = {toml_string(reasoning_effort)}
approval_policy = {toml_string(APPROVAL_POLICY)}
approvals_reviewer = "auto_review"
default_permissions = {toml_string(PERMISSION_PROFILE)}

[permissions.{PERMISSION_PROFILE}]
extends = ":workspace"

[permissions.{PERMISSION_PROFILE}.filesystem.":workspace_roots"]
".pinboard" = "write"

[features]
memories = false
apps = false
remote_plugin = false
tool_suggest = false

[skills.bundled]
enabled = false

[marketplaces.{marketplace}]
source_type = "local"
source = {toml_string(str(plugin_root))}

[plugins."pinboard@{marketplace}"]
enabled = true

[plugins."pinboard@{marketplace}".mcp_servers.pinboard]
default_tools_approval_mode = "approve"
"""
    descriptor = os.open(home / "config.toml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    installed = codex(
        ["plugin", "add", f"pinboard@{marketplace}"], home=home, cwd=home, timeout_seconds=600, window=window
    )
    if installed.returncode != 0:
        raise CodexUnavailableError(f"codex plugin add failed: {installed.stderr.strip()}"[:2000])


def codex(
    arguments: list[str], *, home: Path, cwd: Path, timeout_seconds: float, window: processes.Window
) -> processes.Completed:
    return processes.run_tool(
        processes.Tool.CODEX,
        arguments,
        cwd=cwd,
        environment=os.environ | {"CODEX_HOME": str(home), "RUST_LOG": "warn"},
        stdin=None,
        timeout_seconds=timeout_seconds,
        window=window,
    )


def codex_version(window: processes.Window) -> str:
    completed = processes.run_tool(
        processes.Tool.CODEX,
        ["--version"],
        cwd=Path.cwd(),
        environment=os.environ,
        stdin=None,
        timeout_seconds=60,
        window=window,
    )
    return completed.stdout.strip()


def price(model: str) -> Price:
    if model not in PRICES_PER_MILLION:
        raise CodexUnavailableError(f"no recorded API list price for model {model}; record one before running it")
    return PRICES_PER_MILLION[model]


def turn_cost(model: str, usage: Usage) -> float:
    rates = price(model)
    return (
        usage.uncached_input_tokens * rates.uncached_input
        + usage.cache_write_input_tokens * rates.cache_write
        + usage.cached_input_tokens * rates.cached_input
        + usage.output_tokens * rates.output
    ) / 1_000_000


@dataclass(frozen=True)
class LoadedContext:
    entries: list[InventoryEntry]
    sandbox_mode: str
    approval_policy: str
    writable_roots: list[str]
    prompt_text: str


def loaded_context(home: Path, project: Path, window: processes.Window) -> LoadedContext:
    """Read the model-visible context and MCP servers this isolated home loads for the project (no model call)."""
    rendered = codex(["debug", "prompt-input"], home=home, cwd=project, timeout_seconds=300, window=window)
    text = prompt_text(rendered.stdout)
    servers = codex(["mcp", "list", "--json"], home=home, cwd=project, timeout_seconds=300, window=window)
    entries = [*skill_entries(text), *instruction_entries(text), *section_entries(text)]
    entries.extend(
        InventoryEntry(
            kind="mcp-server",
            name=server.name,
            source=f"{'enabled' if server.enabled else 'disabled'}: {server.transport.command} "
            f"{' '.join(server.transport.args)} (cwd {server.transport.cwd})",
        )
        for server in msgspec.json.decode(servers.stdout.encode(), type=list[McpServer])
    )
    sandbox = re.search(r"`sandbox_mode` is `([a-z-]+)`", text)
    approval = re.search(r"Approval policy is currently ([a-z-]+)", text)
    return LoadedContext(
        entries=entries,
        sandbox_mode=sandbox.group(1) if sandbox else "unreported",
        approval_policy=approval.group(1) if approval else "unreported",
        writable_roots=writable_roots(text),
        prompt_text=text,
    )


def prompt_text(stdout: str) -> str:
    parts = [
        part
        for message in msgspec.json.decode(stdout.encode(), type=list[PromptMessage])
        for part in message.content
        if msgspec.json.decode(part, type=PromptPart).type in {"input_text", "output_text", "text"}
    ]
    return "\n".join(msgspec.json.decode(part, type=PromptText).text for part in parts)


def skill_entries(text: str) -> list[InventoryEntry]:
    roots = {
        str(match.group(1)): str(match.group(2))
        for match in re.finditer(r"^- `(r\d+)` = `([^`]+)`$", text, re.MULTILINE)
    }
    skills = re.finditer(r"^- (\S+): .*\(file: (r\d+)/", text, re.MULTILINE)
    return [
        InventoryEntry(
            kind="skill", name=str(match.group(1)), source=roots.get(str(match.group(2)), str(match.group(2)))
        )
        for match in skills
    ]


def instruction_entries(text: str) -> list[InventoryEntry]:
    return [
        InventoryEntry(kind="instruction-file", name="AGENTS.md", source=directory)
        for directory in re.findall(r"^# AGENTS\.md instructions for (.+)$", text, re.MULTILINE)
    ]


def section_entries(text: str) -> list[InventoryEntry]:
    names = sorted(set(re.findall(r"^<([a-z_ ]+)>", text, re.MULTILINE)) - {"INSTRUCTIONS"})
    return [InventoryEntry(kind="runtime-bundled", name=name, source="codex developer context") for name in names]


def writable_roots(text: str) -> list[str]:
    return sorted(set(re.findall(r'<entry access="write"><(?:special|path)>([^<]+)</', text)))


def isolation_findings(context: LoadedContext, plugin_cache: Path, project: Path) -> list[str]:
    """Return every loaded source beyond the exported skills, its Pinboard MCP server and the project AGENTS.md."""
    findings = []
    for entry in context.entries:
        match entry.kind:
            case "skill":
                if not entry.name.startswith("pinboard:") or not Path(entry.source).resolve().is_relative_to(
                    plugin_cache.resolve()
                ):
                    findings.append(f"unexpected skill {entry.name} from {entry.source}")
            case "mcp-server":
                if entry.name != "pinboard" or not entry.source.startswith("enabled"):
                    findings.append(f"unexpected MCP server {entry.name}: {entry.source}")
            case "instruction-file":
                if Path(entry.source).resolve() != project.resolve():
                    findings.append(f"unexpected instruction file for {entry.source}")
            case "plugin" | "hook" | "runtime-bundled":
                pass
            case _ as unreachable:
                raise AssertionError(unreachable)
    if not any(entry.kind == "mcp-server" for entry in context.entries):
        findings.append("the Pinboard MCP server is not configured")
    if not any(entry.kind == "skill" for entry in context.entries):
        findings.append("no exported skill is loaded")
    return findings


def run_turn(
    home: Path, project: Path, thread_id: str | None, human: str, raw_path: Path, window: processes.Window
) -> tuple[processes.Completed, TurnReading]:
    arguments = (
        ["exec", "--strict-config", "--json", "-C", str(project), human]
        if thread_id is None
        else ["exec", "resume", "--strict-config", "--json", thread_id, human]
    )
    try:
        completed = codex(arguments, home=home, cwd=project, timeout_seconds=TURN_TIMEOUT_SECONDS, window=window)
    except (processes.ProcessInterrupted, processes.CleanupUnconfirmed) as interrupted:
        try:
            raw_path.write_text(interrupted.stdout)
            raw_path.with_suffix(".stderr").write_text(interrupted.stderr)
        except BaseException as evidence_failure:
            interrupted.args = (*interrupted.args, f"partial-output capture failed: {evidence_failure!r}")
            raise interrupted from evidence_failure
        raise
    raw_path.write_text(completed.stdout)
    raw_path.with_suffix(".stderr").write_text(completed.stderr)
    return completed, read_events(completed.stdout)


def read_events(stdout: str) -> TurnReading:
    reading = TurnReading(
        thread_id=None,
        messages=[],
        usage=None,
        denials=[],
        git_write_denied=False,
        mcp_approval_denied=False,
        mcp_calls_completed=[],
        errors=[],
    )
    for line in stdout.splitlines():
        if not line.strip().startswith("{"):
            continue
        raw = line.encode()
        match msgspec.json.decode(raw, type=Event).type:
            case "thread.started":
                reading.thread_id = msgspec.json.decode(raw, type=ThreadStarted).thread_id
            case "item.completed":
                read_item(raw, reading)
            case "turn.completed":
                reading.usage = msgspec.json.decode(raw, type=TurnCompleted).usage
            case "turn.failed" | "error":
                reading.errors.append(line[:2000])
            case _:
                pass
    return reading


def read_item(raw: bytes, reading: TurnReading) -> None:
    match msgspec.json.decode(raw, type=ItemEvent).item.type:
        case "agent_message":
            reading.messages.append(msgspec.json.decode(raw, type=AgentMessageEvent).item.text)
        case "command_execution":
            command = msgspec.json.decode(raw, type=CommandEvent).item
            if command.exit_code not in (0, None) and DENIAL_PATTERN.search(command.aggregated_output):
                reading.denials.append(
                    PermissionDenial(tool=command.command[:500], detail=command.aggregated_output[:1000])
                )
                if GIT_PATTERN.search(command.command) or GIT_PATTERN.search(command.aggregated_output):
                    reading.git_write_denied = True
        case "mcp_tool_call":
            call = msgspec.json.decode(raw, type=McpCallEvent).item
            if call.status == "failed" and call.error is not None and "approval" in call.error.message:
                reading.denials.append(PermissionDenial(tool=f"{call.server}.{call.tool}", detail=call.error.message))
                reading.mcp_approval_denied = True
            if call.status == "completed":
                reading.mcp_calls_completed.append(f"{call.server}.{call.tool}")
        case "file_change":
            change = msgspec.json.decode(raw, type=FileChangeEvent).item
            if change.status == "failed":
                reading.denials.append(PermissionDenial(tool="file_change", detail=raw.decode()[:1000]))
        case _:
            pass


GIT_WRITE_REFUSAL = re.compile(
    r"(?:Unable to create|could not lock|cannot lock ref|unable to write|could not write|unable to create)"
    r"[^\n\"\\]{0,300}?(?:\.git|origin\.git)"
    r"|(?:\.git|origin\.git)[^\n\"\\]{0,300}?(?:Operation not permitted|Read-only file system|Permission denied)"
)
APPROVAL_REFUSAL = re.compile(r"[^\n\"\\]{0,200}but approval policy is never[^\n\"\\]{0,100}")


@dataclass(frozen=True)
class RolloutRefusals:
    git_writes: list[str]
    approvals: list[str]


def rollout_text(home: Path) -> str:
    """The session rollout Codex kept in the disposable home: the complete tool calls and outputs of the thread.

    ``codex exec --json`` omits tool calls that the sandbox or approval policy refused before they ran, so refusals
    are searched for in this captured text rather than decoded as a structured format.
    """
    return "".join(path.read_text() for path in sorted((home / "sessions").rglob("rollout-*.jsonl")))


class RolloutEnvelope(msgspec.Struct, frozen=True):
    type: str
    payload: msgspec.Raw


class RolloutTurn(msgspec.Struct, frozen=True):
    turn_id: str
    model: str


class RolloutOutput(msgspec.Struct, frozen=True):
    call_id: str
    output: str | list[msgspec.Raw]
    internal_chat_message_metadata_passthrough: msgspec.Raw


class OutputOwner(msgspec.Struct, frozen=True):
    turn_id: str


class TextPart(msgspec.Struct, frozen=True):
    type: str
    text: str


class ShellOutput(msgspec.Struct, frozen=True):
    exit_code: int
    output: str


class GuardianEvent(msgspec.Struct, frozen=True):
    type: str
    thread_id: str
    turn_id: str
    item: msgspec.Raw


class GuardianMessage(msgspec.Struct, frozen=True):
    type: str
    phase: str
    content: list[TextPart]


class GuardianDecision(msgspec.Struct, frozen=True):
    outcome: str


class GuardianRationale(msgspec.Struct, frozen=True):
    rationale: str


class UsageRecord(msgspec.Struct, frozen=True):
    thread_id: str
    turn_id: str
    response_id: str
    thread_token_usage: Usage


def rollout_records(text: str) -> list[RolloutEnvelope]:
    lines = text.splitlines()
    if lines and not text.endswith("\n"):
        try:
            msgspec.json.decode(lines[-1].encode(), type=RolloutEnvelope)
        except msgspec.DecodeError:
            lines.pop()  # The final write was interrupted; its retained bytes remain raw evidence.
    return [msgspec.json.decode(line.encode(), type=RolloutEnvelope) for line in lines if line.strip()]


def rollout_refusals(text: str) -> RolloutRefusals:
    """Read actual primary tool outputs and reviewer-owned final decisions, never quoted transcript history.

    Native exec JSON omits some sandbox failures. The rollout's turn_context identifies the primary and guardian
    turns, and only their genuine output records are evidence. A deny fixture uses the installed guardian's
    declared outcome schema; it does not claim a rejection was experimentally observed.
    """
    records = rollout_records(text)
    turns = {
        t.turn_id: t.model
        for r in records
        if r.type == "turn_context"
        for t in [msgspec.json.decode(r.payload, type=RolloutTurn)]
    }
    git_writes: list[str] = []
    approvals: list[str] = []
    for record in records:
        if (
            record.type == "response_item"
            and msgspec.json.decode(record.payload, type=Event).type == "custom_tool_call_output"
        ):
            failures, rejections = primary_tool_refusals(record.payload, turns)
            git_writes.extend(failures)
            approvals.extend(rejections)
        elif record.type == "event_msg" and msgspec.json.decode(record.payload, type=Event).type == "item_completed":
            if (denied := reviewer_refusal(record.payload, turns)) is not None:
                approvals.append(denied)

    return RolloutRefusals(git_writes=git_writes, approvals=approvals)


def primary_tool_refusals(raw: msgspec.Raw, turns: dict[str, str]) -> tuple[list[str], list[str]]:
    output = msgspec.json.decode(raw, type=RolloutOutput)
    owner = msgspec.json.decode(output.internal_chat_message_metadata_passthrough, type=OutputOwner)
    if owner.turn_id not in turns or turns[owner.turn_id] == "codex-auto-review":
        return [], []
    texts = (
        [output.output]
        if isinstance(output.output, str)
        else [
            msgspec.json.decode(part, type=TextPart).text
            for part in output.output
            if msgspec.json.decode(part, type=Event).type == "input_text"
        ]
    )
    git_writes: list[str] = []
    approvals: list[str] = []
    for text_part in texts:
        try:
            shell = msgspec.json.decode(text_part.encode(), type=ShellOutput)
        except msgspec.DecodeError:
            if APPROVAL_REFUSAL.search(text_part):
                approvals.append(text_part[:1000])
            continue
        if shell.exit_code != 0 and GIT_WRITE_REFUSAL.search(shell.output):
            git_writes.append(shell.output[:1000])
    return git_writes, approvals


def reviewer_refusal(raw: msgspec.Raw, turns: dict[str, str]) -> str | None:
    event = msgspec.json.decode(raw, type=GuardianEvent)
    if (
        turns.get(event.turn_id) != "codex-auto-review"
        or msgspec.json.decode(event.item, type=Event).type != "AgentMessage"
    ):
        return None
    message = msgspec.json.decode(event.item, type=GuardianMessage)
    if message.phase != "final_answer":
        return None
    decision_text = "".join(part.text for part in message.content if part.type == "Text")
    decision = msgspec.json.decode(decision_text.encode(), type=GuardianDecision)
    match decision.outcome:
        case "deny":
            return msgspec.json.decode(decision_text.encode(), type=GuardianRationale).rationale
        case "allow":
            return None
        case _:
            raise CodexStreamError("guardian reported an unsupported decision; stop for investigation")


def rollout_accounting(text: str, model: str, complete: bool) -> CodexAccounting:
    """Keep the latest cumulative usage per actual thread; do not count replayed response copies twice."""
    records = rollout_records(text)
    turns = {
        t.turn_id: t.model
        for r in records
        if r.type == "turn_context"
        for t in [msgspec.json.decode(r.payload, type=RolloutTurn)]
    }
    latest: dict[str, UsageRecord] = {}
    seen: set[str] = set()
    for record in records:
        if record.type != "token_usage_record":
            continue
        usage = msgspec.json.decode(record.payload, type=UsageRecord)
        if usage.response_id not in seen:
            latest[usage.thread_id] = usage
            seen.add(usage.response_id)
    reviewers: list[ReviewerUsage] = []
    known = 0.0
    main_observed = False
    for thread, record in latest.items():
        u = record.thread_token_usage
        observed_model = turns[record.turn_id]
        if observed_model == "codex-auto-review":
            reviewers.append(
                ReviewerUsage(
                    thread_id=thread,
                    model=observed_model,
                    input_tokens=u.input_tokens,
                    cached_input_tokens=u.cached_input_tokens,
                    cache_write_input_tokens=u.cache_write_input_tokens,
                    output_tokens=u.output_tokens,
                    reasoning_output_tokens=u.reasoning_output_tokens,
                )
            )
        elif observed_model == model:
            known += turn_cost(model, u)
            main_observed = True
        else:
            raise CodexStreamError(f"unpriced auxiliary thread model {observed_model}; stop for investigation")
    return CodexAccounting(
        schema="pinboard-behavioral-codex-accounting/v1",
        main_known_cost_usd=known,
        main_usage_complete=complete and main_observed,
        reviewer_usage=reviewers,
        reviewer_price_usd=None,
    )


def details(model_effort: str, context: LoadedContext, write_back: bool) -> CodexRunDetails:
    return CodexRunDetails(
        reasoning_effort=model_effort,
        permission_profile=PERMISSION_PROFILE,
        sandbox_mode=context.sandbox_mode,
        approval_policy=context.approval_policy,
        writable_roots=context.writable_roots,
        price_source=PRICE_SOURCE,
        credential_write_back=write_back,
    )


def turn_evidence(
    index: int,
    human: str,
    hook_ran: Hook | None,
    thread_id: str,
    reading: TurnReading,
    previous: Usage | None,
    model: str,
    started: str,
) -> TurnEvidence:
    """Record one turn with its own usage and cost: ``previous`` is the thread's cumulative usage before the turn."""
    usage = None if reading.usage is None else usage_since(previous, reading.usage)
    return TurnEvidence(
        index=index,
        human=human,
        hook_ran=hook_ran,
        session_id=thread_id,
        final_reply=reading.messages[-1] if reading.messages else "",
        commentary=reading.messages[:-1],
        started_at=started,
        finished_at=now(),
        cost_usd=None if usage is None else turn_cost(model, usage),
        uncached_input_tokens=None if usage is None else usage.uncached_input_tokens,
        cached_input_tokens=None if usage is None else usage.cached_input_tokens,
        cache_write_input_tokens=None if usage is None else usage.cache_write_input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        reasoning_output_tokens=None if usage is None else usage.reasoning_output_tokens,
        permission_denials=reading.denials,
    )
