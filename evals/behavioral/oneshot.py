"""One fresh-context Claude session that sees only its prompt: the blind scorer and the substance assessor.

The session runs from an empty temporary directory with only project settings, no tools, no skills, no MCP server
and no claude.ai account connectors, so no plugin, user setting or instruction file reaches it. Its output is the
``claude -p --output-format json`` result record, decoded at that accepted external boundary.
"""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import msgspec

from evals.behavioral import processes

SESSION_TIMEOUT_SECONDS = 1800
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


def ask(prompt: str, model: str, window: processes.Window) -> Answer:
    with tempfile.TemporaryDirectory(prefix="pinboard-eval-oneshot-") as empty:
        completed = processes.run_tool(
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
            cwd=Path(empty),
            environment=os.environ | {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
            stdin=prompt,
            timeout_seconds=SESSION_TIMEOUT_SECONDS,
            window=window,
        )
    if completed.timed_out:
        return Answer(None, None, completed.stdout, "session timed out; partial output retained and cost unknown")
    try:
        result = msgspec.json.decode(completed.stdout.encode(), type=OneshotResult)
    except msgspec.DecodeError as error:
        return Answer(
            None, None, completed.stdout, f"no decodable claude result: {error}; {completed.stderr.strip()}"[:2000]
        )
    if result.is_error:
        return Answer(result.total_cost_usd, None, completed.stdout, "the claude session reported an error")
    try:
        text = msgspec.json.decode(completed.stdout.encode(), type=OneshotText).result
    except msgspec.DecodeError as error:
        return Answer(result.total_cost_usd, None, completed.stdout, f"the claude result has no text: {error}")
    return Answer(result.total_cost_usd, text, completed.stdout, None)


def last_json_block(text: str) -> str | None:
    blocks = FENCED_JSON.findall(text)
    return blocks[-1] if blocks else None
