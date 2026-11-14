"""Pinboard MCP tool use read from the raw turn streams every run directory already keeps.

A run's ``turn-<k>.jsonl`` files hold the runtime's own output for each scripted turn: the ``claude -p`` stream-json
event stream for Claude Code and the ``codex exec --json`` event stream for Codex. Both are accepted externally owned
protocol boundaries, so each consumed field is validated and unrelated additive fields or event kinds are ignored.
Reading needs no subprocess and no change to the stored run record, so runs recorded before this reader existed are
measurable too.

Every call to a Pinboard tool gets its origin and its outcome. A Claude Code call made inside a subagent carries the
spawning ``parent_tool_use_id``; every Codex call in the turn stream is the main agent's. The outcome is
``errored`` when the runtime or SDK reports an error (Claude ``is_error``, Codex ``failed``), ``rejected`` when the
Pinboard result's ``status`` is ``rejected``, ``accepted`` when the result carries any other status or none, and
``unobserved`` when the stream holds no result or a result whose status cannot be read. The status comes from the
structured result when the runtime keeps one (Claude ``tool_use_result.structuredContent``, Codex
``structured_content``), otherwise from the JSON text content; Claude subagent results carry only the text content,
and an oversized Claude result replaces the text content with a note while its structured result remains.

A run whose calls cannot all be observed is reported as unobservable with a reason, never as zero calls: an
Antigravity run, whose ``agy -p`` output exposes no individual tool calls; a run whose raw stream for a started turn
is missing; and a Codex run that started subagents, whose calls Codex records only in the session rollout.
"""

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import assert_never

import msgspec

from evals.behavioral.layout import Layout
from evals.behavioral.records import PinboardTool, RunKey, RunRecord, Runtime

CLAUDE_TOOL_PREFIX = "mcp__plugin_pinboard_pinboard__"
CODEX_SERVER = "pinboard"
REJECTED = "rejected"


class Origin(Enum):
    MAIN = "main"
    SUBAGENT = "subagent"


class CallOutcome(Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERRORED = "errored"
    UNOBSERVED = "unobserved"


@dataclass(frozen=True)
class ToolCall:
    turn: int
    tool: str
    origin: Origin
    outcome: CallOutcome


@dataclass(frozen=True)
class Observed:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class Unobservable:
    reason: str


type RunToolUse = Observed | Unobservable


class Event(msgspec.Struct, frozen=True):
    type: str


class Block(msgspec.Struct, frozen=True):
    type: str


class StatusCarrier(msgspec.Struct, frozen=True):
    """Any Pinboard result object; only its optional top-level ``status`` matters here."""

    status: str | msgspec.UnsetType = msgspec.UNSET


class ClaudeToolUse(msgspec.Struct, frozen=True):
    id: str
    name: str


class TextPart(msgspec.Struct, frozen=True):
    type: str
    text: str | msgspec.UnsetType = msgspec.UNSET


class ClaudeToolResult(msgspec.Struct, frozen=True):
    tool_use_id: str
    content: str | list[TextPart] | msgspec.UnsetType | None = msgspec.UNSET
    is_error: bool | msgspec.UnsetType = msgspec.UNSET


class ClaudeMessage(msgspec.Struct, frozen=True):
    content: str | list[msgspec.Raw]


class ClaudeMessageEvent(msgspec.Struct, frozen=True):
    message: ClaudeMessage
    parent_tool_use_id: str | msgspec.UnsetType | None = msgspec.UNSET
    tool_use_result: msgspec.Raw | msgspec.UnsetType = msgspec.UNSET


class ClaudeStructuredResult(msgspec.Struct, frozen=True):
    structured_content: StatusCarrier | msgspec.UnsetType | None = msgspec.field(
        name="structuredContent", default=msgspec.UNSET
    )


class CodexResult(msgspec.Struct, frozen=True):
    content: list[TextPart]
    structured_content: StatusCarrier | msgspec.UnsetType | None = msgspec.UNSET


class CodexError(msgspec.Struct, frozen=True):
    message: str


class CodexItemKind(msgspec.Struct, frozen=True):
    id: str
    type: str


class CodexMcpCall(msgspec.Struct, frozen=True):
    id: str
    server: str
    tool: str
    status: str
    result: CodexResult | None
    error: CodexError | None


class CodexItemEvent(msgspec.Struct, frozen=True):
    item: msgspec.Raw


def text_status(content: str | list[TextPart] | msgspec.UnsetType | None) -> str | msgspec.UnsetType | None:
    """The ``status`` of a JSON-object text result, ``None`` when it has none; ``UNSET`` when the text is not one."""
    match content:
        case None | msgspec.UnsetType():
            return msgspec.UNSET
        case str() as text:
            joined = text
        case list() as parts:
            joined = "".join(part.text for part in parts if part.type == "text" and isinstance(part.text, str))
    try:
        status = msgspec.json.decode(joined.encode(), type=StatusCarrier).status
    except msgspec.DecodeError:
        return msgspec.UNSET
    return None if status is msgspec.UNSET else status


def outcome(
    errored: bool, structured: StatusCarrier | msgspec.UnsetType | None, text: str | msgspec.UnsetType | None
) -> CallOutcome:
    """Classify one finished call: a structured status wins over the text status, and an unreadable status is
    unobserved, never accepted."""
    if errored:
        return CallOutcome.ERRORED
    if isinstance(structured, StatusCarrier):
        status: str | msgspec.UnsetType | None = None if structured.status is msgspec.UNSET else structured.status
    else:
        status = text
    if status is msgspec.UNSET:
        return CallOutcome.UNOBSERVED
    return CallOutcome.REJECTED if status == REJECTED else CallOutcome.ACCEPTED


def claude_structured(raw: msgspec.Raw | msgspec.UnsetType) -> StatusCarrier | msgspec.UnsetType | None:
    if isinstance(raw, msgspec.UnsetType):
        return msgspec.UNSET
    decoded = msgspec.json.decode(raw, type=ClaudeStructuredResult | str | list[msgspec.Raw] | None)
    return decoded.structured_content if isinstance(decoded, ClaudeStructuredResult) else msgspec.UNSET


def claude_calls(turn: int, stdout: str) -> list[ToolCall]:
    """Every Pinboard call in one Claude Code turn stream, main agent and subagents alike, in call order."""
    started: dict[str, tuple[str, Origin]] = {}
    finished: dict[str, CallOutcome] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        raw = line.encode()
        kind = msgspec.json.decode(raw, type=Event).type
        if kind not in {"assistant", "user"}:
            continue
        event = msgspec.json.decode(raw, type=ClaudeMessageEvent)
        if isinstance(event.message.content, str):
            continue
        for block in event.message.content:
            match msgspec.json.decode(block, type=Block).type:
                case "tool_use":
                    use = msgspec.json.decode(block, type=ClaudeToolUse)
                    if use.name.startswith(CLAUDE_TOOL_PREFIX) and use.id not in started:
                        origin = Origin.SUBAGENT if isinstance(event.parent_tool_use_id, str) else Origin.MAIN
                        started[use.id] = (use.name.removeprefix(CLAUDE_TOOL_PREFIX), origin)
                case "tool_result":
                    result = msgspec.json.decode(block, type=ClaudeToolResult)
                    if result.tool_use_id in started:
                        finished[result.tool_use_id] = outcome(
                            result.is_error is True,
                            claude_structured(event.tool_use_result),
                            text_status(result.content),
                        )
                case _:
                    pass
    return [
        ToolCall(turn, tool, origin, finished.get(call_id, CallOutcome.UNOBSERVED))
        for call_id, (tool, origin) in started.items()
    ]


@dataclass(frozen=True)
class TurnCalls:
    calls: list[ToolCall]
    started_subagents: bool


def codex_calls(turn: int, stdout: str) -> TurnCalls:
    """Every Pinboard call in one Codex turn stream; only the main agent's calls appear there."""
    started: dict[str, str] = {}
    finished: dict[str, CallOutcome] = {}
    subagents = False
    for line in stdout.split("\n"):
        if not line.strip().startswith("{"):
            continue
        raw = line.encode()
        kind = msgspec.json.decode(raw, type=Event).type
        if kind not in {"item.started", "item.completed"}:
            continue
        item = msgspec.json.decode(raw, type=CodexItemEvent).item
        match msgspec.json.decode(item, type=CodexItemKind).type:
            case "mcp_tool_call":
                call = msgspec.json.decode(item, type=CodexMcpCall)
                if call.server != CODEX_SERVER:
                    continue
                started.setdefault(call.id, call.tool)
                if kind == "item.completed":
                    finished[call.id] = outcome(
                        call.status == "failed" or call.error is not None,
                        msgspec.UNSET if call.result is None else call.result.structured_content,
                        msgspec.UNSET if call.result is None else text_status(call.result.content),
                    )
            case "collab_tool_call":
                subagents = True
            case _:
                pass
    calls = [
        ToolCall(turn, tool, Origin.MAIN, finished.get(call_id, CallOutcome.UNOBSERVED))
        for call_id, tool in started.items()
    ]
    return TurnCalls(calls, subagents)


def claude_turn(turn: int, stdout: str) -> TurnCalls:
    return TurnCalls(claude_calls(turn, stdout), started_subagents=False)


def read_run(layout: Layout, record: RunRecord) -> RunToolUse:
    """The Pinboard calls of one recorded run, from the raw stream of every turn it started."""
    match record.runtime:
        case Runtime.ANTIGRAVITY:
            return Unobservable("Antigravity output exposes no individual tool calls")
        case Runtime.CLAUDE_CODE:
            read_turn = claude_turn
        case Runtime.CODEX:
            read_turn = codex_calls
        case _ as unreachable:
            assert_never(unreachable)
    directory = layout.run_directory(record.run)
    calls: list[ToolCall] = []
    for turn in record.turns:
        path = directory / f"turn-{turn.index}.jsonl"
        if not path.is_file():
            return Unobservable(f"{record.run.display()} has no raw stream {path.name} for a started turn")
        reading = read_turn(turn.index, path.read_text())
        if reading.started_subagents:
            return Unobservable(
                f"{record.run.display()} started Codex subagents, whose calls only its session rollout records"
            )
        calls.extend(reading.calls)
    return Observed(tuple(calls))


@dataclass(frozen=True)
class Tally:
    calls: int
    outcomes: dict[CallOutcome, int]
    subagent: int

    @property
    def not_accepted(self) -> int:
        return self.calls - self.outcomes[CallOutcome.ACCEPTED]

    def measures(self) -> list[tuple[str, int]]:
        """The reported counts, in report order."""
        return [
            ("calls", self.calls),
            *((kind.value, self.outcomes[kind]) for kind in CallOutcome),
            ("subagent calls", self.subagent),
            ("not accepted", self.not_accepted),
        ]


EMPTY_TALLY = Tally(calls=0, outcomes=dict.fromkeys(CallOutcome, 0), subagent=0)


def tally(calls: tuple[ToolCall, ...], tools: frozenset[str] | None) -> Tally:
    """Count calls to ``tools``, or to every Pinboard tool when ``tools`` is ``None``."""
    selected = [call for call in calls if tools is None or call.tool in tools]
    counts = Counter(call.outcome for call in selected)
    return Tally(
        calls=len(selected),
        outcomes={kind: counts[kind] for kind in CallOutcome},
        subagent=sum(call.origin is Origin.SUBAGENT for call in selected),
    )


def tally_text(counts: Tally) -> str:
    outcomes = " ".join(f"{kind.value}={counts.outcomes[kind]}" for kind in CallOutcome)
    return f"calls={counts.calls} {outcomes} subagent={counts.subagent}"


def report(layout: Layout) -> str:
    """Per-run Pinboard call counts for every recorded run in an output directory; no paid session."""
    lines = ["run\truntime\tpinboard calls\ttools"]
    totals: Counter[str] = Counter()
    unobservable = 0
    for record in layout.run_records():
        reading = read_run(layout, record)
        match reading:
            case Unobservable(reason=reason):
                unobservable += 1
                lines.append(f"{record.run.display()}\t{record.runtime.value}\tunobservable: {reason}")
            case Observed(calls=calls):
                counts = tally(calls, None)
                totals["runs"] += 1
                totals["calls"] += counts.calls
                for kind in CallOutcome:
                    totals[kind.value] += counts.outcomes[kind]
                totals["subagent"] += counts.subagent
                tools = Counter(call.tool for call in calls)
                named = ",".join(f"{tool}:{count}" for tool, count in sorted(tools.items())) or "none"
                lines.append(f"{record.run.display()}\t{record.runtime.value}\t{tally_text(counts)}\t{named}")
            case _ as unreachable:
                assert_never(unreachable)
    summary = " ".join(f"{kind.value}={totals[kind.value]}" for kind in CallOutcome)
    lines.append(
        f"observed runs={totals['runs']} calls={totals['calls']} {summary} subagent={totals['subagent']}; "
        f"unobservable runs={unobservable}"
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Coverage:
    """Which member scenarios declare exercising any of the changed tools."""

    surface: frozenset[str]
    exercising: dict[str, frozenset[str]]

    @property
    def covered(self) -> bool:
        return bool(self.exercising)

    def describe(self) -> str:
        changed = ", ".join(sorted(self.surface))
        if not self.exercising:
            return f"changed tools {changed}: no scenario in the set declares exercising them"
        declared = "; ".join(
            f"{scenario} ({', '.join(sorted(tools))})" for scenario, tools in sorted(self.exercising.items())
        )
        return f"changed tools {changed}: declared exercised by {declared}"


def coverage(declared: dict[str, frozenset[PinboardTool]], surface: frozenset[str]) -> Coverage:
    exercising = {scenario: frozenset(tools & surface) for scenario, tools in declared.items() if tools & surface}
    return Coverage(surface, exercising)


def observed_exercise(layout: Layout, runs: list[RunKey], surface: frozenset[str]) -> str:
    """What the runs of one command actually did with the changed tools, one line per recorded run."""
    lines = [f"observed calls to {', '.join(sorted(surface))}:"]
    for key in runs:
        record = layout.run_record(key)
        if record is None:
            continue
        match read_run(layout, record):
            case Unobservable(reason=reason):
                lines.append(f"  {key.display()}\tunobservable: {reason}")
            case Observed(calls=calls):
                lines.append(f"  {key.display()}\t{tally_text(tally(calls, surface))}")
            case _ as unreachable:
                assert_never(unreachable)
    return "\n".join(lines) + "\n"
