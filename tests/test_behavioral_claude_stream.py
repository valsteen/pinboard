"""Claude Code driver: per-turn cost from resumed session totals, failed turns and skill provenance, from synthetic
stream text returned by a controlled fake in place of the claude CLI."""

import json
import os
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import override
from unittest import mock

from evals.behavioral import claude_driver, oneshot, processes

type Json = str | int | float | bool | list[Json] | dict[str, Json] | None

PROVENANCE = (
    "[DEBUG] Loaded 0 unique skills (0 unconditional, 0 conditional, managed: 0, user: {user}, project: 0, "
    "additional: 0, legacy commands: 0)\n"
    "[DEBUG] getSkills returning: 0 skill dir commands, 1 plugin skills, 2 bundled skills, 0 builtin plugin skills\n"
)


def init_event(plugin_root: Path) -> dict[str, Json]:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": "s-1",
        "model": "model",
        "permissionMode": "bypassPermissions",
        "claude_code_version": "test",
        "plugins": [{"name": "pinboard", "path": str(plugin_root), "source": "pinboard@inline"}],
        "skills": ["pinboard:pinboard", "simplify"],
        "mcp_servers": [{"name": "plugin:pinboard:pinboard", "status": "connected"}],
        "added_later": True,
    }


def result_event(total_cost: float, *, subtype: str, is_error: bool) -> dict[str, Json]:
    return {
        "type": "result",
        "subtype": subtype,
        "session_id": "s-1",
        "is_error": is_error,
        "total_cost_usd": total_cost,
        "result": "Answer.",
        "usage": {
            "input_tokens": 3,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 10,
            "output_tokens": 20,
        },
        "permission_denials": [],
    }


@dataclass(frozen=True)
class FakeCall:
    tool: processes.Tool
    arguments: list[str]
    cwd: Path
    environment: dict[str, str]
    stdin: str | None
    timeout_seconds: float


@dataclass
class FakeClaude:
    """Stands in for ``processes.run_tool``: answers each turn with the next scripted stream."""

    streams: list[list[dict[str, Json]]]
    exit_codes: list[int]
    debug_log: str
    calls: list[FakeCall]

    def __call__(
        self,
        tool: processes.Tool,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdin: str | None,
        timeout_seconds: float,
        window: processes.Window,
    ) -> processes.Completed:
        window.timeout(timeout_seconds)
        self.calls.append(FakeCall(tool, list(arguments), cwd, dict(environment), stdin, timeout_seconds))
        if "--debug-file" in arguments:
            Path(arguments[arguments.index("--debug-file") + 1]).write_text(self.debug_log)
        stream = "\n".join(json.dumps(event) for event in self.streams.pop(0))
        return processes.Completed(self.exit_codes.pop(0), stream + "\n", "", False)


class ClaudeTurnTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plugin_root = self.root / "plugin"
        self.session = claude_driver.ClaudeSession.start(self.plugin_root, "model", self.root, ("default",))

    def run_turns(self, fake: FakeClaude, count: int) -> list[claude_driver.ClaudeTurn]:
        with mock.patch.object(claude_driver.processes, "run_tool", fake):
            return [
                self.session.turn(index, "question", None, self.root / f"turn-{index}.jsonl")
                for index in range(1, count + 1)
            ]

    def test_resumed_turns_record_their_own_share_of_the_session_cost(self) -> None:
        fake = FakeClaude(
            streams=[
                [init_event(self.plugin_root), result_event(0.25, subtype="success", is_error=False)],
                [result_event(0.75, subtype="success", is_error=False)],
                [result_event(1.0, subtype="success", is_error=False)],
            ],
            exit_codes=[0, 0, 0],
            debug_log=PROVENANCE.format(user=0),
            calls=[],
        )
        turns = self.run_turns(fake, 3)
        self.assertEqual([0.25, 0.5, 0.25], [turn.evidence.cost_usd for turn in turns])
        self.assertEqual([None, None, None], [turn.problem for turn in turns])
        self.assertIn("--session-id", fake.calls[0].arguments)
        self.assertIn("--resume", fake.calls[1].arguments)
        self.assertNotIn("--debug-file", fake.calls[1].arguments)
        self.assertEqual({processes.Tool.CLAUDE}, {call.tool for call in fake.calls})
        self.assertEqual("false", fake.calls[0].environment["ENABLE_CLAUDEAI_MCP_SERVERS"])
        self.assertEqual(self.root, fake.calls[0].cwd)

    def test_a_session_total_that_falls_is_refused(self) -> None:
        fake = FakeClaude(
            streams=[
                [init_event(self.plugin_root), result_event(0.5, subtype="success", is_error=False)],
                [result_event(0.25, subtype="success", is_error=False)],
            ],
            exit_codes=[0, 0],
            debug_log=PROVENANCE.format(user=0),
            calls=[],
        )
        with self.assertRaises(claude_driver.StreamError):
            self.run_turns(fake, 2)

    def test_a_turn_claude_reports_as_an_error_is_a_failed_turn_with_its_cost(self) -> None:
        fake = FakeClaude(
            streams=[
                [init_event(self.plugin_root), result_event(0.125, subtype="error_during_execution", is_error=True)]
            ],
            exit_codes=[1],
            debug_log=PROVENANCE.format(user=0),
            calls=[],
        )
        (turn,) = self.run_turns(fake, 1)
        self.assertIsNotNone(turn.problem)
        self.assertEqual("", turn.evidence.final_reply)
        self.assertEqual(0.125, turn.evidence.cost_usd)
        self.assertNotIn("--tools", fake.calls[0].arguments)

    def test_an_error_flag_on_a_success_result_is_still_a_failed_turn(self) -> None:
        fake = FakeClaude(
            streams=[[init_event(self.plugin_root), result_event(0.125, subtype="success", is_error=True)]],
            exit_codes=[0],
            debug_log=PROVENANCE.format(user=0),
            calls=[],
        )
        (turn,) = self.run_turns(fake, 1)
        self.assertIsNotNone(turn.problem)


class SkillProvenanceTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plugin_root = self.root / "plugin"

    def session_after_first_turn(self, debug_log: str) -> claude_driver.ClaudeSession:
        session = claude_driver.ClaudeSession.start(self.plugin_root, "model", self.root, ("default",))
        fake = FakeClaude(
            streams=[[init_event(self.plugin_root), result_event(0.1, subtype="success", is_error=False)]],
            exit_codes=[0],
            debug_log=debug_log,
            calls=[],
        )
        with mock.patch.object(claude_driver.processes, "run_tool", fake):
            session.turn(1, "question", None, self.root / "turn-1.jsonl")
        return session

    def test_exported_and_bundled_skills_only_pass_and_are_labelled_from_the_debug_log(self) -> None:
        session = self.session_after_first_turn(PROVENANCE.format(user=0))
        self.assertEqual([], session.isolation_findings())
        bundled = next(entry for entry in session.loaded_context() if entry.name == "simplify")
        self.assertEqual("runtime-bundled", bundled.kind)
        self.assertIn("user 0", bundled.source)

    def test_a_user_skill_is_an_isolation_finding(self) -> None:
        session = self.session_after_first_turn(PROVENANCE.format(user=1))
        self.assertNotEqual([], session.isolation_findings())

    def test_a_claude_builtin_plugin_skill_is_not_foreign(self) -> None:
        session = self.session_after_first_turn(
            PROVENANCE.format(user=0).replace("0 builtin plugin skills", "1 builtin plugin skills")
        )
        self.assertEqual([], session.isolation_findings())

    def test_a_missing_provenance_summary_is_an_isolation_finding(self) -> None:
        session = self.session_after_first_turn("[DEBUG] nothing about skills\n")
        findings = session.isolation_findings()
        self.assertEqual(1, len(findings))
        self.assertIn("simplify", findings[0])


class CleanEnvironmentTest(unittest.TestCase):
    def test_agent_version_and_scorer_preserve_login_without_host_context_or_secret_inventory(self) -> None:
        host = {
            "HOME": "/synthetic/home",
            "PATH": "/synthetic/bin",
            "TMPDIR": "/synthetic/tmp",
            "USER": "synthetic-user",
            "LOGNAME": "synthetic-user",
            "SHELL": "/bin/sh",
            "LANG": "en_US.UTF-8",
            "ANTHROPIC_API_KEY": "synthetic-auth-secret",
            "CLAUDE_CODE_SESSION_ID": "foreign-session",
            "CLAUDE_CODE_MESSAGING_SOCKET": "foreign-socket",
            "CLAUDE_CODE_MESSAGING_TOKEN": "synthetic-bridge-secret",
            "ANTHROPIC_BASE_URL": "foreign-provider",
            "OTHER_HOST_CONTEXT": "foreign-context",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "true",
        }
        expected_names = {
            "HOME",
            "PATH",
            "TMPDIR",
            "USER",
            "LOGNAME",
            "SHELL",
            "LANG",
            "ANTHROPIC_API_KEY",
            "ENABLE_CLAUDEAI_MCP_SERVERS",
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, host, clear=True):
            root = Path(temporary)
            session = claude_driver.ClaudeSession.start(root / "plugin", "model", root, ("default",))
            fake = FakeClaude(
                streams=[
                    [init_event(root / "plugin"), result_event(0.1, subtype="success", is_error=False)],
                    [result_event(0.1, subtype="success", is_error=False)],
                    [result_event(0.1, subtype="success", is_error=False)],
                ],
                exit_codes=[0, 0, 0],
                debug_log=PROVENANCE.format(user=0),
                calls=[],
            )
            with mock.patch.object(processes, "run_tool", fake):
                session.turn(1, "question", None, root / "turn.jsonl")
                claude_driver.claude_version()
                oneshot.ask("score this", "model", processes.Window(None))
            for call in fake.calls:
                self.assertEqual(expected_names, set(call.environment))
                for name in expected_names - {"ENABLE_CLAUDEAI_MCP_SERVERS"}:
                    self.assertEqual(host[name], call.environment[name])
                self.assertEqual("false", call.environment["ENABLE_CLAUDEAI_MCP_SERVERS"])
            inventory = next(entry for entry in session.loaded_context() if entry.name == "host-environment")
            self.assertEqual("passed variables: " + ", ".join(sorted(fake.calls[0].environment)), inventory.source)
            self.assertNotIn("synthetic-auth-secret", inventory.source)
            self.assertNotIn("synthetic-bridge-secret", inventory.source)
            self.assertNotIn("synthetic-auth-secret", (root / "turn.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
