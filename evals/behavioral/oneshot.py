"""One fresh-context session that sees only its prompt: the blind scorer and the substance assessor.

Claude models run from an empty temporary directory with only project settings, no tools, no skills, no MCP server
and no claude.ai account connectors, decoding the ``claude -p --output-format json`` result record. Non-Flash
Antigravity Gemini models run ``agy -p --output-format json`` from an empty working directory inside a disposable
Keychain-linked ``HOME`` with ``permissions.allow: []`` and no installed plugins, decoding ``AgyTurnResult`` and
pricing token usage via ``agy_driver.turn_cost``.
"""

import re
import tempfile
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

import msgspec

from evals.behavioral import agy_driver, processes

SESSION_TIMEOUT_SECONDS = 1800
AGY_TIMEOUT_SECONDS = 330
FENCED_JSON = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


class OneshotResult(msgspec.Struct, frozen=True):
    is_error: bool
    total_cost_usd: float


class OneshotText(msgspec.Struct, frozen=True):
    result: str


@dataclass(frozen=True)
class Answer:
    cost_usd: float | None
    text: str | None
    stdout: str
    problem: str | None


def is_agy_model(model: str) -> bool:
    """Return True for supported Antigravity Gemini models, False for Claude models, and reject Flash models."""
    if "flash" in model.lower():
        raise agy_driver.AgyUnavailableError(f"Flash models are excluded from blind scoring: {model!r}")
    if model.startswith("gemini-"):
        if agy_driver.price(model) is agy_driver.FLASH_PRICE:
            raise agy_driver.AgyUnavailableError(f"Flash models are excluded from blind scoring: {model!r}")
        return True
    return False


def _run_claude(prompt: str, model: str, empty: Path, window: processes.Window) -> processes.Completed:
    return processes.run_tool(
        processes.Tool.CLAUDE,
        [
            "-p",
            "--model",
            model,
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--tools",
            "",
            "--output-format",
            "json",
        ],
        cwd=empty,
        environment=processes.claude_environment(),
        stdin=prompt,
        timeout_seconds=SESSION_TIMEOUT_SECONDS,
        window=window,
    )


def _run_agy(prompt: str, model: str, empty: Path, window: processes.Window) -> processes.Completed:
    cwd = empty / "cwd"
    cwd.mkdir()
    with agy_driver.disposable_home(empty / "home", ()) as home:
        return processes.run_tool(
            processes.Tool.AGY,
            [
                "-p",
                prompt,
                "--output-format",
                "json",
                "--model",
                model,
                "--disable-slash-commands",
                "--print-timeout",
                "5m",
            ],
            cwd=cwd,
            environment=processes.agy_environment(home),
            stdin=None,
            timeout_seconds=AGY_TIMEOUT_SECONDS,
            window=window,
        )


def _decode_claude_answer(completed: processes.Completed) -> Answer:
    if completed.timed_out:
        return Answer(None, None, completed.stdout, "session timed out; partial output retained and cost unknown")
    try:
        result = msgspec.json.decode(completed.stdout.encode(), type=OneshotResult)
    except msgspec.DecodeError as error:
        return Answer(
            None, None, completed.stdout, f"no decodable claude result: {error}; {completed.stderr.strip()}"[:2000]
        )
    if not isfinite(result.total_cost_usd) or result.total_cost_usd < 0:
        return Answer(None, None, completed.stdout, "the claude result has invalid cost")
    if result.is_error:
        return Answer(result.total_cost_usd, None, completed.stdout, "the claude session reported an error")
    try:
        text = msgspec.json.decode(completed.stdout.encode(), type=OneshotText).result
    except msgspec.DecodeError as error:
        return Answer(result.total_cost_usd, None, completed.stdout, f"the claude result has no text: {error}")
    return Answer(result.total_cost_usd, text, completed.stdout, None)


def _decode_agy_answer(completed: processes.Completed, model: str) -> Answer:
    if completed.timed_out or "print timeout after" in completed.stderr:
        return Answer(None, None, completed.stdout, "session timed out; partial output retained and cost unknown")
    try:
        result = msgspec.json.decode(completed.stdout.encode(), type=agy_driver.AgyTurnResult)
    except msgspec.DecodeError as error:
        return Answer(
            None, None, completed.stdout, f"no decodable agy result: {error}; {completed.stderr.strip()}"[:2000]
        )
    try:
        usage = agy_driver.usage_since(result.usage, None)
        cost_usd = agy_driver.turn_cost(model, usage)
    except (agy_driver.AgyStreamError, agy_driver.AgyUnavailableError) as error:
        return Answer(None, None, completed.stdout, f"the agy result has invalid usage: {error}")
    if not isfinite(cost_usd) or cost_usd < 0:
        return Answer(None, None, completed.stdout, "the agy result has invalid cost")
    if completed.returncode != 0 or result.status != "SUCCESS":
        return Answer(cost_usd, None, completed.stdout, "the agy session reported an error")
    if not result.response.strip():
        return Answer(cost_usd, None, completed.stdout, "the agy result has no text")
    return Answer(cost_usd, result.response, completed.stdout, None)


def ask(prompt: str, model: str, window: processes.Window) -> Answer:
    use_agy = is_agy_model(model)
    completed: processes.Completed | None = None
    incomplete: processes.ProcessIncomplete | processes.CleanupUnconfirmed | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="pinboard-eval-oneshot-") as empty:
            try:
                root = Path(empty)
                completed = (
                    _run_agy(prompt, model, root, window) if use_agy else _run_claude(prompt, model, root, window)
                )
            except (processes.ProcessIncomplete, processes.CleanupUnconfirmed) as failure:
                incomplete = failure
                raise
    except BaseException as failure:
        if incomplete is not None:
            if failure is not incomplete:
                incomplete.args = (*incomplete.args, f"session-directory cleanup failed: {failure!r}")
                failure.__cause__ = incomplete.__cause__
                raise incomplete from failure
            raise
        if completed is not None:
            raise processes.ProcessIncomplete(
                completed.stdout.encode(), completed.stderr.encode(), failure
            ) from failure
        raise
    return _decode_agy_answer(completed, model) if use_agy else _decode_claude_answer(completed)


def last_json_block(text: str) -> str | None:
    blocks = FENCED_JSON.findall(text)
    return blocks[-1] if blocks else None
