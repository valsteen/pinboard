"""Claude Code agent-under-test driver: one fresh ``claude -p`` session per run, resumed for every scripted turn.

The agent loads only project settings (``--setting-sources project``), the exported revision as its one plugin
(``--plugin-dir``) and no claude.ai account connectors (``ENABLE_CLAUDEAI_MCP_SERVERS=false``), and runs with
``--permission-mode bypassPermissions``. Output is the ``claude -p`` stream-json event stream, an accepted
externally owned protocol boundary whose final result record is the ``--output-format json`` result: each consumed
field is validated and unrelated additive fields are ignored. Its result reports the session's cumulative cost, so a
turn's cost is the difference from the previous turn's total, and a result that reports an error is a failed turn.

The init event lists skill names without their source. The first turn therefore also writes Claude Code's debug log
to a temporary file, and the driver reads the skill-loading summary lines from that captured text (searched with
patterns, not decoded as a structured format) to establish where every loaded skill came from. The file is deleted
once read. A skill from anywhere but the exported plugin or Claude Code's own bundled set, a missing summary, or an
unexpected plugin or MCP server is an isolation finding that stops the run for a human decision.
"""

import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import msgspec

from evals.behavioral import processes
from evals.behavioral.records import ClaudeRunDetails, Hook, InventoryEntry, PermissionDenial, TurnEvidence

TURN_TIMEOUT_SECONDS = 5400
HOST_PATTERN = re.compile(r'host_id: "([^"]+)"')
ISOLATION_ENVIRONMENT = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
EXPORTED_SKILL_PREFIX = "pinboard:"
EXPORTED_MCP_SERVER = "plugin:pinboard:pinboard"
HOST_VARIABLE_PREFIXES = ("CLAUDE", "ANTHROPIC")
LOADED_SKILLS = re.compile(
    r"Loaded \d+ unique skills \(\d+ unconditional, \d+ conditional, managed: (\d+), user: (\d+), project: (\d+), "
    r"additional: (\d+), legacy commands: (\d+)\)"
)
RETURNED_SKILLS = re.compile(
    r"getSkills returning: (\d+) skill dir commands, (\d+) plugin skills, (\d+) bundled skills, "
    r"(\d+) builtin plugin skills"
)


class Event(msgspec.Struct, frozen=True):
    type: str


class SystemEvent(msgspec.Struct, frozen=True):
    subtype: str


class InitPlugin(msgspec.Struct, frozen=True):
    name: str
    path: str
    source: str


class InitServer(msgspec.Struct, frozen=True):
    name: str
    status: str


class InitEvent(msgspec.Struct, frozen=True):
    session_id: str
    model: str
    permission_mode: str = msgspec.field(name="permissionMode")
    claude_code_version: str
    plugins: list[InitPlugin]
    skills: list[str]
    mcp_servers: list[InitServer]


class HookStarted(msgspec.Struct, frozen=True):
    hook_name: str


class HookResponse(msgspec.Struct, frozen=True):
    hook_name: str
    hook_event: str
    output: str


class HookContext(msgspec.Struct, frozen=True):
    additional_context: str = msgspec.field(name="additionalContext")


class HookOutput(msgspec.Struct, frozen=True):
    hook_specific_output: HookContext = msgspec.field(name="hookSpecificOutput")


class Denial(msgspec.Struct, frozen=True):
    tool_name: str


class Usage(msgspec.Struct, frozen=True):
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int


class ResultEvent(msgspec.Struct, frozen=True):
    subtype: str
    session_id: str
    is_error: bool
    total_cost_usd: float
    usage: Usage
    permission_denials: list[Denial]


class SuccessText(msgspec.Struct, frozen=True):
    result: str


class StreamError(Exception):
    """The claude stream ended without a decodable final result record or reported an impossible cost total."""


@dataclass(frozen=True)
class SkillProvenance:
    """Claude Code's own count of loaded skills by source, read from its debug log."""

    managed: int
    user: int
    project: int
    additional: int
    legacy_commands: int
    skill_dir_commands: int
    plugin: int
    bundled: int
    builtin_plugin: int

    def foreign(self) -> int:
        return (
            self.managed
            + self.user
            + self.project
            + self.additional
            + self.legacy_commands
            + self.skill_dir_commands
            + self.builtin_plugin
        )

    def describe(self) -> str:
        return (
            f"debug log: managed {self.managed}, user {self.user}, project {self.project}, additional "
            f"{self.additional}, legacy commands {self.legacy_commands}, skill dir commands {self.skill_dir_commands}, "
            f"plugin {self.plugin}, bundled {self.bundled}, builtin plugin {self.builtin_plugin}"
        )


def skill_provenance(debug_log: str) -> SkillProvenance | None:
    """Sum every skill-loading summary in the debug log; ``None`` when either summary line is missing."""
    loaded = LOADED_SKILLS.findall(debug_log)
    returned = RETURNED_SKILLS.findall(debug_log)
    if not loaded or not returned:
        return None
    managed, user, project, additional, legacy = (sum(int(row[i]) for row in loaded) for i in range(5))
    last = returned[-1]
    return SkillProvenance(
        managed=managed,
        user=user,
        project=project,
        additional=additional,
        legacy_commands=legacy,
        skill_dir_commands=int(last[0]),
        plugin=int(last[1]),
        bundled=int(last[2]),
        builtin_plugin=int(last[3]),
    )


@dataclass(frozen=True)
class ClaudeTurn:
    evidence: TurnEvidence
    problem: str | None


@dataclass
class ClaudeSession:
    plugin_root: Path
    model: str
    project: Path
    session_id: str
    init: InitEvent | None
    provenance: SkillProvenance | None
    hooks: set[str]
    observed_host_ids: list[str]
    reported_cost_usd: float

    @classmethod
    def start(cls, plugin_root: Path, model: str, project: Path) -> ClaudeSession:
        return cls(plugin_root, model, project, str(uuid.uuid4()), None, None, set(), [], 0.0)

    def loaded_context(self) -> list[InventoryEntry]:
        hooks = [
            InventoryEntry(kind="hook", name=name, source="claude stream hook event") for name in sorted(self.hooks)
        ]
        started = [] if self.init is None else claude_inventory(self.init, self.plugin_root, self.provenance)
        return [*started, host_environment(), *hooks, *instruction_files(self.project)]

    def isolation_findings(self) -> list[str]:
        if self.init is None:
            return ["the claude stream reported no init event, so its loaded context is unknown"]
        return isolation_findings(self.init, self.plugin_root, self.provenance)

    def details(self) -> ClaudeRunDetails:
        return ClaudeRunDetails(permission_mode="bypassPermissions", cost_basis="claude total_cost_usd")

    def turn(self, index: int, human: str, hook_ran: Hook | None, raw_path: Path) -> ClaudeTurn:
        session = ["--session-id", self.session_id] if index == 1 else ["--resume", self.session_id]
        started = now()
        with tempfile.TemporaryDirectory(prefix="pinboard-eval-claude-debug-") as scratch:
            debug_file = Path(scratch) / "debug.log"
            debug = ["--debug-file", str(debug_file)] if index == 1 else []
            completed = processes.run_tool(
                processes.Tool.CLAUDE,
                [
                    "-p",
                    "--model",
                    self.model,
                    "--setting-sources",
                    "project",
                    "--plugin-dir",
                    str(self.plugin_root),
                    "--permission-mode",
                    "bypassPermissions",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                    "--include-hook-events",
                    *debug,
                    *session,
                    human,
                ],
                cwd=self.project,
                environment=os.environ | ISOLATION_ENVIRONMENT,
                stdin=None,
                timeout_seconds=TURN_TIMEOUT_SECONDS,
                window=processes.Window(None),
            )
            if index == 1:
                self.provenance = skill_provenance(debug_file.read_text() if debug_file.is_file() else "")
        raw_path.write_text(completed.stdout)
        raw_path.with_suffix(".stderr").write_text(completed.stderr)
        result, reply = self.read_stream(completed.stdout)
        cost = result.total_cost_usd - self.reported_cost_usd
        if cost < 0:
            raise StreamError(
                f"turn {index} reported a session cost total {result.total_cost_usd} below the previous turn's "
                f"{self.reported_cost_usd}"
            )
        self.reported_cost_usd = result.total_cost_usd
        evidence = TurnEvidence(
            index=index,
            human=human,
            hook_ran=hook_ran,
            session_id=result.session_id,
            final_reply=reply,
            commentary=[],
            started_at=started,
            finished_at=now(),
            cost_usd=cost,
            uncached_input_tokens=result.usage.input_tokens,
            cached_input_tokens=result.usage.cache_read_input_tokens,
            cache_write_input_tokens=result.usage.cache_creation_input_tokens,
            output_tokens=result.usage.output_tokens,
            reasoning_output_tokens=0,
            permission_denials=[
                PermissionDenial(tool=denial.tool_name, detail="") for denial in result.permission_denials
            ],
        )
        return ClaudeTurn(evidence, turn_problem(result, completed))

    def read_stream(self, stdout: str) -> tuple[ResultEvent, str]:
        result: ResultEvent | None = None
        reply = ""
        for line in stdout.splitlines():
            if not line.strip():
                continue
            raw = line.encode()
            match msgspec.json.decode(raw, type=Event).type:
                case "system":
                    self.read_system(raw)
                case "result":
                    result = msgspec.json.decode(raw, type=ResultEvent)
                    reply = (
                        msgspec.json.decode(raw, type=SuccessText).result
                        if result.subtype == "success" and not result.is_error
                        else ""
                    )
                case _:
                    pass
        if result is None:
            raise StreamError("the claude stream carried no result record")
        return result, reply

    def read_system(self, raw: bytes) -> None:
        match msgspec.json.decode(raw, type=SystemEvent).subtype:
            case "init":
                if self.init is None:
                    self.init = msgspec.json.decode(raw, type=InitEvent)
            case "hook_started":
                self.hooks.add(msgspec.json.decode(raw, type=HookStarted).hook_name)
            case "hook_response":
                response = msgspec.json.decode(raw, type=HookResponse)
                if response.hook_event == "SessionStart":
                    self.observed_host_ids.extend(session_start_hosts(response.output))
            case _:
                pass


def turn_problem(result: ResultEvent, completed: processes.Completed) -> str | None:
    """Why a turn with a result record did not complete, or ``None`` when it did."""
    if result.subtype == "success" and not result.is_error and completed.returncode == 0:
        return None
    return (
        f"claude reported result {result.subtype} (is_error {str(result.is_error).lower()}, exit code "
        f"{completed.returncode}): {completed.stderr.strip()}"
    )[:2000]


def session_start_hosts(output: str) -> list[str]:
    try:
        context = msgspec.json.decode(output.strip().encode(), type=HookOutput).hook_specific_output.additional_context
    except msgspec.DecodeError:
        return []
    return HOST_PATTERN.findall(context)


def bundled_source(provenance: SkillProvenance | None) -> str:
    if provenance is None:
        return "unknown: the debug log reported no skill provenance"
    return f"claude-code bundled skill ({provenance.describe()})"


def claude_inventory(init: InitEvent, plugin_root: Path, provenance: SkillProvenance | None) -> list[InventoryEntry]:
    entries = []
    for plugin in init.plugins:
        bundled = plugin.path == "builtin"
        entries.append(
            InventoryEntry(
                kind="runtime-bundled" if bundled else "plugin",
                name=plugin.name,
                source=plugin.source if bundled else plugin.path,
            )
        )
    for skill in init.skills:
        exported = skill.startswith(EXPORTED_SKILL_PREFIX)
        entries.append(
            InventoryEntry(
                kind="skill" if exported else "runtime-bundled",
                name=skill,
                source=str(plugin_root) if exported else bundled_source(provenance),
            )
        )
    entries.extend(
        InventoryEntry(kind="mcp-server", name=server.name, source=server.status) for server in init.mcp_servers
    )
    entries.append(InventoryEntry(kind="runtime-bundled", name="model", source=init.model))
    entries.append(InventoryEntry(kind="runtime-bundled", name="permission-mode", source=init.permission_mode))
    if provenance is not None:
        entries.append(InventoryEntry(kind="runtime-bundled", name="skill-provenance", source=provenance.describe()))
    return entries


def isolation_findings(init: InitEvent, plugin_root: Path, provenance: SkillProvenance | None) -> list[str]:
    """Every loaded source beyond the exported plugin, its MCP server and Claude Code's own bundled plugins and skills."""
    findings = [
        f"unexpected plugin {plugin.name} from {plugin.path}"
        for plugin in init.plugins
        if plugin.path != "builtin" and Path(plugin.path).resolve() != plugin_root.resolve()
    ]
    findings.extend(
        f"unexpected MCP server {server.name}" for server in init.mcp_servers if server.name != EXPORTED_MCP_SERVER
    )
    exported = [skill for skill in init.skills if skill.startswith(EXPORTED_SKILL_PREFIX)]
    others = [skill for skill in init.skills if not skill.startswith(EXPORTED_SKILL_PREFIX)]
    if provenance is None:
        findings.append(
            "the debug log reported no skill provenance, so these skills have an unknown source: " + ", ".join(others)
        )
        return findings
    if provenance.foreign():
        findings.append(
            f"skills from outside the exported plugin and Claude Code's bundled set ({provenance.describe()})"
        )
    if provenance.plugin != len(exported):
        findings.append(f"{len(exported)} exported skills listed but {provenance.plugin} plugin skills loaded")
    if len(others) > provenance.bundled:
        findings.append(f"{len(others)} unprefixed skills listed but only {provenance.bundled} bundled skills loaded")
    return findings


def host_environment() -> InventoryEntry:
    """The names (never the values) of the Claude host variables the agent inherits; they change the bundled skills."""
    names = sorted(name for name in os.environ if name.startswith(HOST_VARIABLE_PREFIXES))
    return InventoryEntry(
        kind="runtime-bundled",
        name="host-environment",
        source="inherited variables: " + (", ".join(names) if names else "none"),
    )


def claude_version() -> str:
    completed = processes.run_tool(
        processes.Tool.CLAUDE,
        ["--version"],
        cwd=Path.cwd(),
        environment=os.environ,
        stdin=None,
        timeout_seconds=60,
        window=processes.Window(None),
    )
    return completed.stdout.strip()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def instruction_files(project: Path) -> list[InventoryEntry]:
    return [
        InventoryEntry(kind="instruction-file", name=name, source="world project (harness-written)")
        for name in ("CLAUDE.md", "AGENTS.md")
        if (project / name).is_file()
    ]
