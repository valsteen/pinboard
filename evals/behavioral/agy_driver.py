"""Antigravity CLI driver: disposable home, plugin install, headless JSON turns and conservative list-price spend.

Antigravity CLI (``agy``) runs headlessly with ``-p`` and ``--output-format json``. Each run gets a disposable
``HOME`` that symlinks only ``~/Library/Keychains`` (for macOS Keychain OAuth access), writes harness-owned
headless permissions under ``.gemini/antigravity-cli/settings.json``, and installs the exported Pinboard plugin into
``<home>/.gemini/config/plugins/pinboard`` via ``agy plugin install``.
"""

import json
import re
import shutil
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import msgspec

from evals.behavioral import claude_driver, export, processes
from evals.behavioral.claude_driver import now
from evals.behavioral.records import (
    AgyEffort,
    AntigravityRunDetails,
    ExportRecord,
    Hook,
    InventoryEntry,
    NonEmpty,
    PermissionDenial,
    TurnEvidence,
)


class AgyUnavailableError(ValueError):
    """A preflight requirement for Antigravity runs is not met; no agent turn started."""


class AgyStreamError(Exception):
    """An ``agy -p --output-format json`` turn failed or returned an incomplete/invalid result."""


@dataclass(frozen=True)
class AgyPrice:
    """List prices in USD per 1,000,000 tokens for <=200K and >200K input contexts."""

    input_usd: float
    cached_input_usd: float
    output_usd: float
    long_input_usd: float
    long_cached_input_usd: float
    long_output_usd: float


SONNET_PRICE = AgyPrice(3.00, 0.30, 15.00, 6.00, 0.60, 22.50)
OPUS_PRICE = AgyPrice(5.00, 0.50, 25.00, 10.00, 1.00, 37.50)
PRO_PRICE = AgyPrice(2.00, 0.20, 12.00, 4.00, 0.40, 18.00)
FLASH_PRICE = AgyPrice(0.50, 0.05, 3.00, 0.50, 0.05, 3.00)
OSS_PRICE = AgyPrice(0.15, 0.075, 0.60, 0.15, 0.075, 0.60)

PRICES_PER_MILLION: dict[str, AgyPrice] = {
    "claude-sonnet-5-5": SONNET_PRICE,
    "claude-sonnet-5-5-low": SONNET_PRICE,
    "claude-sonnet-5-5-medium": SONNET_PRICE,
    "claude-sonnet-5-5-high": SONNET_PRICE,
    "claude-sonnet-5-5-max": SONNET_PRICE,
    "claude-sonnet-4-6": SONNET_PRICE,
    "claude-opus-5-5": OPUS_PRICE,
    "claude-opus-5-5-low": OPUS_PRICE,
    "claude-opus-5-5-medium": OPUS_PRICE,
    "claude-opus-5-5-high": OPUS_PRICE,
    "claude-opus-5-5-max": OPUS_PRICE,
    "claude-opus-4-6": OPUS_PRICE,
    "gemini-4-argon": PRO_PRICE,
    "gemini-4-argon-low": PRO_PRICE,
    "gemini-4-argon-medium": PRO_PRICE,
    "gemini-4-argon-high": PRO_PRICE,
    "gemini-3.1-pro": PRO_PRICE,
    "gemini-3.1-pro-low": PRO_PRICE,
    "gemini-3.1-pro-high": PRO_PRICE,
    "gemini-3.8-flash": FLASH_PRICE,
    "gemini-3.8-flash-low": FLASH_PRICE,
    "gemini-3.8-flash-medium": FLASH_PRICE,
    "gemini-3.8-flash-high": FLASH_PRICE,
    "gemini-3-flash": FLASH_PRICE,
    "gpt-oss-120b": OSS_PRICE,
    "gpt-oss-120b-medium": OSS_PRICE,
}
PRICE_SOURCE = (
    "Anthropic and Google public API standard list prices per 1M tokens "
    "(https://docs.anthropic.com/en/docs/about-claude/models/overview and https://ai.google.dev/gemini-api/docs/pricing, "
    "checked 2026-10-27): claude-sonnet-5-5 / claude-sonnet-4-6 short input 3.00, cached 0.30, output 15.00 USD "
    "(>200K input 6.00, cached 0.60, output 22.50); claude-opus-5-5 / claude-opus-4-6 short input 5.00, cached 0.50, "
    "output 25.00 (>200K input 10.00, cached 1.00, output 37.50); gemini-4-argon / gemini-3.1-pro short input 2.00, "
    "cached 0.20, output 12.00 (>200K input 4.00, cached 0.40, output 18.00); gemini-3.8-flash / gemini-3-flash "
    "input 0.50, cached 0.05, output 3.00; gpt-oss-120b-medium input 0.15, cached 0.075, output 0.60. "
    "Thinking tokens are billed at the output rate; cache_read_tokens are added at the cached-input rate without "
    "subtracting from input_tokens."
)

PERMISSIONS_ALLOW: tuple[NonEmpty, ...] = (
    "mcp(pinboard_pinboard/*)",
    "command(shasum)",
    "command(git)",
    "command(git status)",
    "command(git log)",
    "command(git diff)",
    "command(git show)",
    "command(git branch)",
    "command(git rev-parse)",
    "command(git worktree)",
    "command(git checkout)",
    "command(git switch)",
    "command(git add)",
    "command(git commit)",
    "command(git merge)",
    "command(git push)",
)

LONG_CONTEXT_TOKENS = 200_000


class AgyUsage(msgspec.Struct, frozen=True):
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cache_read_tokens: int
    total_tokens: int


class AgyDeniedAction(msgspec.Struct, frozen=True):
    action: NonEmpty
    display_name: NonEmpty


class AgyTurnResult(msgspec.Struct, frozen=True):
    conversation_id: str
    status: str
    response: str
    duration_seconds: float
    num_turns: int
    usage: AgyUsage
    denied_actions: tuple[AgyDeniedAction, ...] = ()


class AgyPluginEntry(msgspec.Struct, frozen=True):
    name: str
    enabled: bool
    version: str
    source: str
    path: str
    skills: list[str]
    mcp_servers: list[str] = msgspec.field(name="mcpServers")
    hooks: list[str]


def _counts(usage: AgyUsage) -> tuple[tuple[str, int], ...]:
    return (
        ("input_tokens", usage.input_tokens),
        ("output_tokens", usage.output_tokens),
        ("thinking_tokens", usage.thinking_tokens),
        ("cache_read_tokens", usage.cache_read_tokens),
        ("total_tokens", usage.total_tokens),
    )


def price(model: str) -> AgyPrice:
    if model not in PRICES_PER_MILLION:
        known = ", ".join(sorted(PRICES_PER_MILLION))
        raise AgyUnavailableError(f"no list price is declared for Antigravity model {model!r} (known: {known})")
    return PRICES_PER_MILLION[model]


def usage_since(current: AgyUsage, previous: AgyUsage | None) -> AgyUsage:
    for field, count in _counts(current):
        if count < 0:
            raise AgyStreamError(f"negative {field} in agy usage: {current}")
    if previous is None:
        return current
    delta = AgyUsage(
        input_tokens=current.input_tokens - previous.input_tokens,
        output_tokens=current.output_tokens - previous.output_tokens,
        thinking_tokens=current.thinking_tokens - previous.thinking_tokens,
        cache_read_tokens=current.cache_read_tokens - previous.cache_read_tokens,
        total_tokens=current.total_tokens - previous.total_tokens,
    )
    for field, count in _counts(delta):
        if count < 0:
            raise AgyStreamError(f"cumulative agy usage decreased in {field}: {previous} -> {current}")
    return delta


def turn_cost(model: str, delta: AgyUsage) -> float:
    """Conservative list-price cost for one turn's token delta.

    Because Antigravity's normalized usage stream does not document whether ``input_tokens`` includes
    ``cache_read_tokens`` across model families, ``input_tokens`` is billed in full at the uncached input rate and
    ``cache_read_tokens`` is added at the cached input rate. ``thinking_tokens`` are billed at the output rate.
    """
    rates = price(model)
    long_context = delta.input_tokens + delta.cache_read_tokens > LONG_CONTEXT_TOKENS
    input_rate = rates.long_input_usd if long_context else rates.input_usd
    cached_rate = rates.long_cached_input_usd if long_context else rates.cached_input_usd
    output_rate = rates.long_output_usd if long_context else rates.output_usd
    return (
        delta.input_tokens * input_rate
        + delta.cache_read_tokens * cached_rate
        + (delta.output_tokens + delta.thinking_tokens) * output_rate
    ) / 1_000_000


def agy_version(home: Path, window: processes.Window) -> str:
    completed = processes.run_tool(
        processes.Tool.AGY,
        ["--version"],
        cwd=home,
        environment=processes.agy_environment(home),
        stdin=None,
        timeout_seconds=30,
        window=window,
    )
    if completed.timed_out or completed.returncode != 0:
        raise AgyUnavailableError(f"agy --version failed: {completed.stderr.strip()}")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return " / ".join(lines)


@contextmanager
def disposable_home(home: Path, permissions_allow: Sequence[str]) -> Generator[Path]:
    """Create a disposable HOME with only the macOS Keychains link and explicit headless permissions."""
    home.mkdir(parents=True, exist_ok=False)
    try:
        keychains = Path.home() / "Library" / "Keychains"
        if keychains.exists():
            library = home / "Library"
            library.mkdir(parents=True, exist_ok=True)
            (library / "Keychains").symlink_to(keychains)
        settings = home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"permissions": {"allow": list(permissions_allow)}}, indent=2) + "\n")
        yield home
    except processes.CleanupUnconfirmed:
        raise
    except BaseException:
        shutil.rmtree(home, ignore_errors=True)
        raise
    else:
        shutil.rmtree(home, ignore_errors=True)


@contextmanager
def isolated_home(home: Path) -> Generator[Path]:
    """Create a disposable HOME with only the macOS Keychains link and headless settings."""
    with disposable_home(home, PERMISSIONS_ALLOW) as created:
        yield created


def install_plugin(home: Path, plugin_root: Path, window: processes.Window) -> None:
    completed = processes.run_tool(
        processes.Tool.AGY,
        ["plugin", "install", str(plugin_root)],
        cwd=home,
        environment=processes.agy_environment(home),
        stdin=None,
        timeout_seconds=60,
        window=window,
    )
    if completed.timed_out or completed.returncode != 0:
        raise AgyUnavailableError(
            f"agy plugin install failed ({completed.returncode}): {completed.stdout.strip()} {completed.stderr.strip()}"
        )


def installed_plugins(home: Path, window: processes.Window) -> list[AgyPluginEntry]:
    completed = processes.run_tool(
        processes.Tool.AGY,
        ["plugin", "list", "--json"],
        cwd=home,
        environment=processes.agy_environment(home),
        stdin=None,
        timeout_seconds=30,
        window=window,
    )
    if completed.timed_out or completed.returncode != 0:
        raise AgyUnavailableError(f"agy plugin list --json failed: {completed.stderr.strip()}")
    raw = completed.stdout.strip()
    if not raw or raw == "null":
        return []
    try:
        return msgspec.json.decode(raw.encode(), type=list[AgyPluginEntry])
    except msgspec.DecodeError as error:
        raise AgyUnavailableError(f"agy plugin list --json returned invalid JSON: {error}") from error


def loaded_context(project: Path, plugins: list[AgyPluginEntry], environment: dict[str, str]) -> list[InventoryEntry]:
    entries: list[InventoryEntry] = []
    for plugin in plugins:
        entries.append(InventoryEntry(kind="plugin", name=plugin.name, source=f"{plugin.source}:{plugin.path}"))
        entries.extend(InventoryEntry(kind="skill", name=skill, source=plugin.path) for skill in sorted(plugin.skills))
        entries.extend(
            InventoryEntry(kind="mcp-server", name=server, source=plugin.path) for server in sorted(plugin.mcp_servers)
        )
        entries.extend(InventoryEntry(kind="hook", name=hook, source=plugin.path) for hook in sorted(plugin.hooks))
    agents = project / "AGENTS.md"
    if agents.is_file():
        entries.append(InventoryEntry(kind="instruction-file", name="tally/AGENTS.md", source=str(agents)))
    entries.append(claude_driver.host_environment(environment))
    return entries


def _plugin_findings(plugin: AgyPluginEntry, expected_install: Path, evaluated: ExportRecord) -> list[str]:
    findings: list[str] = []
    if not plugin.enabled:
        findings.append(f"installed plugin {plugin.name} is disabled")
    if plugin.name != "pinboard":
        findings.append(f"unexpected plugin name {plugin.name!r}")
    if plugin.source != "antigravity":
        findings.append(f"unexpected plugin source {plugin.source!r}")
    installed_path = Path(plugin.path).resolve()
    if installed_path != expected_install:
        findings.append(f"plugin path {installed_path} is outside {expected_install}")
    skills_dir = installed_path / "skills"
    if not skills_dir.is_dir() or not plugin.skills:
        findings.append(f"installed plugin has no skills at {skills_dir}")
    else:
        digest = export.skills_sha256(installed_path)
        if digest != evaluated.skills_sha256:
            findings.append(f"installed skills sha256 {digest} != exported {evaluated.skills_sha256}")
    return findings


def isolation_findings(
    home: Path,
    project: Path,
    evaluated: ExportRecord,
    version: str,
    plugins: list[AgyPluginEntry],
    entries: list[InventoryEntry],
) -> list[str]:
    findings: list[str] = []
    if not version:
        findings.append("agy --version returned empty output")
    expected_install = (home / ".gemini" / "config" / "plugins" / "pinboard").resolve()
    if len(plugins) != 1:
        findings.append(f"expected 1 installed plugin, found {len(plugins)}: {[p.name for p in plugins]}")
        return findings
    findings.extend(_plugin_findings(plugins[0], expected_install, evaluated))
    mcps = [entry.name for entry in entries if entry.kind == "mcp-server"]
    if mcps != ["pinboard"]:
        findings.append(f"expected only pinboard mcp-server, found {mcps}")
    hooks = [entry.name for entry in entries if entry.kind == "hook"]
    if hooks != ["pinboard"]:
        findings.append(f"expected only pinboard hook, found {hooks}")
    instructions = [Path(entry.source).resolve() for entry in entries if entry.kind == "instruction-file"]
    expected_agents = (project / "AGENTS.md").resolve()
    if instructions != [expected_agents]:
        findings.append(f"expected instruction file {expected_agents}, found {instructions}")
    return findings


def details(effort: AgyEffort, observed_model: str | None) -> AntigravityRunDetails:
    price_source: NonEmpty = PRICE_SOURCE
    return AntigravityRunDetails(
        reasoning_effort=effort,
        observed_model=observed_model,
        permissions_allow=list(PERMISSIONS_ALLOW),
        price_source=price_source,
    )


def _decode_turn_result(completed: processes.Completed, expected_conversation_id: str | None) -> AgyTurnResult:
    if completed.timed_out or "print timeout after" in completed.stderr:
        raise AgyStreamError("agy turn timed out; partial output is not scored")
    try:
        result = msgspec.json.decode(completed.stdout.encode(), type=AgyTurnResult)
    except msgspec.DecodeError as error:
        if completed.returncode != 0:
            raise AgyStreamError(
                f"agy exited {completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"[:2000]
            ) from error
        raise AgyStreamError(f"agy output is not valid AgyTurnResult JSON: {error}") from error
    if completed.returncode != 0 or result.status != "SUCCESS":
        detail = completed.stderr.strip() or f"status={result.status}"
        raise AgyStreamError(f"agy turn did not succeed: {detail}"[:2000])
    if not result.conversation_id.strip():
        raise AgyStreamError("agy returned an empty conversation_id")
    if expected_conversation_id is not None and result.conversation_id != expected_conversation_id:
        raise AgyStreamError(
            f"agy conversation_id changed across resumed turn: {expected_conversation_id} -> {result.conversation_id}"
        )
    if not result.response.strip():
        raise AgyStreamError("agy returned an empty response")
    return result


@dataclass
class AgySession:
    home: Path
    project: Path
    model: str
    effort: AgyEffort
    window: processes.Window
    conversation_id: str | None
    totals: AgyUsage | None
    observed_model: str | None
    turn_evidence: TurnEvidence | None

    @classmethod
    def start(
        cls,
        home: Path,
        project: Path,
        model: str,
        effort: AgyEffort,
        window: processes.Window,
    ) -> AgySession:
        return cls(
            home=home,
            project=project,
            model=model,
            effort=effort,
            window=window,
            conversation_id=None,
            totals=None,
            observed_model=None,
            turn_evidence=None,
        )

    def send_turn(self, index: int, prompt: str, hook: Hook | None, raw_path: Path) -> TurnEvidence:
        price(self.model)
        arguments = [
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.model,
            "--effort",
            self.effort.value,
            "--print-timeout",
            "5m",
            *([] if self.conversation_id is None else ["--conversation", self.conversation_id]),
        ]
        started_at = now()
        self.turn_evidence = None
        unreported = TurnEvidence(
            index=index,
            human=prompt,
            hook_ran=hook,
            session_id=self.conversation_id or "unreported",
            final_reply="",
            commentary=[],
            started_at=started_at,
            finished_at=started_at,
            cost_usd=None,
            uncached_input_tokens=None,
            cached_input_tokens=None,
            cache_write_input_tokens=None,
            output_tokens=None,
            reasoning_output_tokens=None,
            permission_denials=[],
        )
        try:
            completed = processes.run_tool(
                processes.Tool.AGY,
                arguments,
                cwd=self.project,
                environment=processes.agy_environment(self.home),
                stdin=None,
                timeout_seconds=330,
                window=self.window,
            )
        except processes.ProcessIncomplete, processes.CleanupUnconfirmed:
            self.turn_evidence = msgspec.structs.replace(unreported, finished_at=now())
            raise
        finished_at = now()
        self.turn_evidence = msgspec.structs.replace(unreported, finished_at=finished_at)
        raw_path.write_text(completed.stdout)
        if completed.stderr:
            raw_path.with_suffix(".stderr").write_text(completed.stderr)
        result = _decode_turn_result(completed, self.conversation_id)
        self.conversation_id = result.conversation_id
        delta = usage_since(result.usage, self.totals)
        self.totals = result.usage
        denials = [
            PermissionDenial(tool=denied.display_name, detail=denied.action) for denied in result.denied_actions
        ] + [
            PermissionDenial(tool="agy-permissions", detail=match.group(1).strip())
            for match in re.finditer(
                r"([^\n]+auto-denied\. Add an allow-rule under permissions\.allow[^\n]*)",
                completed.stderr,
            )
        ]
        evidence = TurnEvidence(
            index=index,
            human=prompt,
            hook_ran=hook,
            session_id=result.conversation_id,
            final_reply=result.response,
            commentary=[],
            started_at=started_at,
            finished_at=finished_at,
            cost_usd=turn_cost(self.model, delta),
            uncached_input_tokens=delta.input_tokens,
            cached_input_tokens=delta.cache_read_tokens,
            cache_write_input_tokens=0,
            output_tokens=delta.output_tokens + delta.thinking_tokens,
            reasoning_output_tokens=delta.thinking_tokens,
            permission_denials=denials,
        )
        self.turn_evidence = evidence
        return evidence
