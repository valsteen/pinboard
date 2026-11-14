"""Pinboard tool use read from recorded raw turn streams, the tool-use verdict beside the checklist verdict, and the
changed-tool coverage check before paid runs.

The stream fixtures are lines cut from recorded Claude Code and Codex runs, with local paths and long result bodies
trimmed; their call identities, origins, error flags and result statuses are the recorded ones.
"""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import msgspec

from evals.behavioral import cli, decision, tool_use
from evals.behavioral.layout import Layout
from evals.behavioral.records import (
    AgyEffort,
    AntigravityRunDetails,
    ChecklistItem,
    CodexRunDetails,
    RunKey,
    Runtime,
    ScenarioSet,
    encode,
)
from evals.behavioral.scenarios import DataIntegrityError, declared_exercise, exercise_path
from evals.behavioral.tool_use import CallOutcome, Observed, Origin, ToolCall, Unobservable
from tests.test_behavioral_decision import fixture_set, record_score

FIXTURES = Path(__file__).parent / "fixtures" / "behavioral-tool-use"
CLAUDE_TURN = (FIXTURES / "claude-turn.jsonl").read_text()
CODEX_TURN = (FIXTURES / "codex-turn.jsonl").read_text()
CODEX_SUBAGENT_TURN = (FIXTURES / "codex-subagent-turn.jsonl").read_text()
IDS = [f"s{number}" for number in range(1, 6)]


def claude_lines(*tools: str) -> str:
    """The recorded call/result pairs for the named tools, in fixture order."""
    lines = CLAUDE_TURN.splitlines()
    pairs = [lines[index : index + 2] for index in range(0, len(lines), 2)]
    selected = [
        pair
        for pair in pairs
        if json.loads(pair[0])["message"]["content"][0]["name"].removeprefix(tool_use.CLAUDE_TOOL_PREFIX) in tools
    ]
    return "\n".join(line for pair in selected for line in pair) + "\n"


class ClaudeStreamTest(unittest.TestCase):
    def test_recorded_calls_keep_origin_and_server_outcome(self) -> None:
        self.assertEqual(
            [
                ToolCall(1, "pinboard_overview", Origin.MAIN, CallOutcome.ACCEPTED),
                ToolCall(1, "pinboard_item_status", Origin.MAIN, CallOutcome.REJECTED),
                ToolCall(1, "pinboard_item_status", Origin.MAIN, CallOutcome.ERRORED),
                ToolCall(1, "pinboard_attempt_authority", Origin.SUBAGENT, CallOutcome.REJECTED),
                ToolCall(1, "pinboard_attempt_authority", Origin.SUBAGENT, CallOutcome.ACCEPTED),
                ToolCall(1, "pinboard_actions", Origin.MAIN, CallOutcome.ACCEPTED),
            ],
            tool_use.claude_calls(1, CLAUDE_TURN),
        )

    def test_an_oversized_result_needs_its_structured_status_and_a_missing_result_is_unobserved(self) -> None:
        lines = CLAUDE_TURN.splitlines()
        oversized = json.loads(lines[-1])
        self.assertTrue(oversized["message"]["content"][0]["content"].startswith("Error: result"))
        del oversized["tool_use_result"]["structuredContent"]
        without_structure = "\n".join([*lines[:-1], json.dumps(oversized)])
        self.assertIs(CallOutcome.UNOBSERVED, tool_use.claude_calls(1, without_structure)[-1].outcome)
        self.assertIs(CallOutcome.UNOBSERVED, tool_use.claude_calls(1, "\n".join(lines[:-1]))[-1].outcome)


class CodexStreamTest(unittest.TestCase):
    def test_recorded_calls_classify_completed_rejections_and_failures(self) -> None:
        reading = tool_use.codex_calls(2, CODEX_TURN)
        self.assertFalse(reading.started_subagents)
        self.assertEqual(
            [
                ToolCall(2, "pinboard_attempt_inspect", Origin.MAIN, CallOutcome.ACCEPTED),
                ToolCall(2, "pinboard_actions", Origin.MAIN, CallOutcome.REJECTED),
                ToolCall(2, "pinboard_item_definition", Origin.MAIN, CallOutcome.ERRORED),
                ToolCall(2, "pinboard_overview", Origin.MAIN, CallOutcome.ERRORED),
            ],
            reading.calls,
        )

    def test_a_completed_rejection_without_structured_content_reads_the_text_status(self) -> None:
        lines = CODEX_TURN.splitlines()
        rejected = json.loads(lines[3])
        del rejected["item"]["result"]["structured_content"]
        changed = "\n".join([*lines[:3], json.dumps(rejected)])
        self.assertIs(CallOutcome.REJECTED, tool_use.codex_calls(1, changed).calls[-1].outcome)
        self.assertIs(CallOutcome.UNOBSERVED, tool_use.codex_calls(1, lines[0]).calls[0].outcome)

    def test_started_subagents_are_reported(self) -> None:
        self.assertTrue(tool_use.codex_calls(1, CODEX_SUBAGENT_TURN).started_subagents)


def prepared(layout: Layout) -> ScenarioSet:
    """Thirty scored single-turn Claude runs per side, without raw streams yet."""
    for name in IDS:
        for index in range(1, decision.required_runs(decision.CRITERIA, len(IDS)) + 1):
            for variant in ("base", "cand"):
                record_score(layout, f"{variant}-{name}-{index}", RunKey(name, variant, index), [set()], True)
    return fixture_set(IDS, 1, (ChecklistItem.P9,)).registration


def write_streams(layout: Layout, variant: str, stream: str) -> None:
    for record in layout.run_records():
        if record.run.variant == variant:
            (layout.run_directory(record.run) / "turn-1.jsonl").write_text(stream)


DECLARED = {"s1": frozenset({"pinboard_overview", "pinboard_item_status"})}
SURFACE = frozenset({"pinboard_overview", "pinboard_item_status"})


class ReadRunTest(unittest.TestCase):
    def test_runs_without_observable_calls_are_never_zero_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            key = RunKey("s1", "base", 1)
            record_score(layout, "T1", key, [set()], True)
            record = layout.run_record(key)
            assert record is not None
            missing = tool_use.read_run(layout, record)
            assert isinstance(missing, Unobservable)
            self.assertIn("turn-1.jsonl", missing.reason)
            (layout.run_directory(key) / "turn-1.jsonl").write_text(claude_lines("pinboard_overview"))
            self.assertEqual(
                Observed((ToolCall(1, "pinboard_overview", Origin.MAIN, CallOutcome.ACCEPTED),)),
                tool_use.read_run(layout, record),
            )
            agy = msgspec.structs.replace(
                record,
                runtime=Runtime.ANTIGRAVITY,
                details=AntigravityRunDetails(AgyEffort.HIGH, None, [], "controlled-price"),
            )
            self.assertIsInstance(tool_use.read_run(layout, agy), Unobservable)
            codex = msgspec.structs.replace(
                record,
                runtime=Runtime.CODEX,
                details=CodexRunDetails("high", "profile", "workspace-write", "on-request", [], "price", False),
            )
            (layout.run_directory(key) / "turn-1.jsonl").write_text(CODEX_TURN)
            observed = tool_use.read_run(layout, codex)
            assert isinstance(observed, Observed)
            self.assertEqual(4, len(observed.calls))
            (layout.run_directory(key) / "turn-1.jsonl").write_text(CODEX_TURN + CODEX_SUBAGENT_TURN)
            self.assertIsInstance(tool_use.read_run(layout, codex), Unobservable)
            (layout.run_directory(key) / "run.json").write_bytes(encode(agy))
            report = tool_use.report(Layout(layout.root))
            self.assertIn("unobservable: Antigravity", report)
            self.assertIn("unobservable runs=1", report)


class ToolUseCompareTest(unittest.TestCase):
    def compare(self, layout: Layout, registration: ScenarioSet, surface: frozenset[str]) -> str:
        return decision.tool_use_compare(Layout(layout.root), registration, DECLARED, "base", "cand", surface)

    def test_equal_failed_calls_are_no_worse_and_an_added_rejection_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            registration = prepared(layout)
            write_streams(layout, "base", claude_lines("pinboard_overview"))
            write_streams(layout, "cand", claude_lines("pinboard_overview"))
            output = self.compare(layout, registration, SURFACE)
            self.assertIn("calls\t1\t1", output)
            self.assertIn("tool-use decision: no worse", output)
            write_streams(layout, "cand", claude_lines("pinboard_overview", "pinboard_item_status"))
            output = self.compare(layout, registration, SURFACE)
            self.assertIn("rejected\t0\t1", output)
            self.assertIn("errored\t0\t1", output)
            self.assertIn("tool-use decision: inconclusive (interval upper bound above", output)
            write_streams(layout, "base", claude_lines("pinboard_overview", "pinboard_item_status"))
            write_streams(layout, "cand", claude_lines("pinboard_overview"))
            self.assertIn("tool-use decision: improved", self.compare(layout, registration, SURFACE))

    def test_unexercised_undeclared_and_unobservable_comparisons_do_not_decide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = Layout(Path(directory))
            registration = prepared(layout)
            self.assertIn(
                f"tool-use decision: refused ({decision.CHECKLIST_ONLY})",
                self.compare(layout, registration, frozenset()),
            )
            refused = self.compare(layout, registration, SURFACE)
            self.assertIn("tool-use decision: refused (", refused)
            self.assertIn("has no raw stream turn-1.jsonl", refused)
            write_streams(layout, "base", claude_lines("pinboard_overview"))
            write_streams(layout, "cand", claude_lines("pinboard_overview"))
            self.assertIn(
                "no call to the changed tools was observed on the baseline and candidate side",
                decision.tool_use_compare(
                    Layout(layout.root),
                    registration,
                    {"s1": frozenset({"pinboard_dispatch"})},
                    "base",
                    "cand",
                    frozenset({"pinboard_dispatch"}),
                ),
            )
            undeclared = decision.tool_use_compare(Layout(layout.root), registration, {}, "base", "cand", SURFACE)
            self.assertIn("no scenario in the set declares exercising them", undeclared)
            self.assertIn("tool-use decision: inconclusive (no scenario in the set declares", undeclared)
            key = RunKey("s2", "cand", 3)
            record = layout.run_record(key)
            assert record is not None
            agy = msgspec.structs.replace(
                record,
                runtime=Runtime.ANTIGRAVITY,
                details=AntigravityRunDetails(AgyEffort.HIGH, None, [], "controlled-price"),
            )
            (layout.run_directory(key) / "run.json").write_bytes(encode(agy))
            self.assertIn(
                "tool-use decision: refused (Antigravity output exposes no individual tool calls)",
                self.compare(layout, registration, SURFACE),
            )
            checklist = decision.compare(Layout(layout.root), registration, "base", "cand")
            self.assertNotIn("tool-use", checklist)


def write_set(root: Path, declaration: dict[str, object] | None) -> Path:
    """A registered set on disk, with its scenarios and an optional tool-exercise declaration."""
    registered = fixture_set(IDS, 1, ())
    (root / "scenarios").mkdir()
    for scenario, content in zip(registered.scenarios, registered.scenario_sources, strict=True):
        (root / "scenarios" / f"{scenario.id}.json").write_bytes(content)
    path = root / "scenario-sets" / "controlled.json"
    path.parent.mkdir()
    path.write_bytes(encode(registered.registration))
    if declaration is not None:
        exercise_path(path).parent.mkdir()
        exercise_path(path).write_text(json.dumps(declaration))
    return path


def declaration(scenario_set_sha256: str, scenario: str) -> dict[str, object]:
    return {
        "schema": "pinboard-behavioral-tool-exercise/v1",
        "scenario_set_sha256": scenario_set_sha256,
        "scenarios": [{"id": scenario, "tools": ["pinboard_dispatch"]}],
    }


class ExerciseDeclarationTest(unittest.TestCase):
    def test_a_declaration_binds_to_exact_set_bytes_and_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_set(root, None)
            registration = fixture_set(IDS, 1, ()).registration
            self.assertEqual({}, declared_exercise(path, registration))
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            exercise_path(path).parent.mkdir()
            exercise_path(path).write_text(json.dumps(declaration(digest, "s2")))
            self.assertEqual({"s2": frozenset({"pinboard_dispatch"})}, declared_exercise(path, registration))
            for changed in (declaration("0" * 64, "s2"), declaration(digest, "s9")):
                exercise_path(path).write_text(json.dumps(changed))
                with self.subTest(changed=changed), self.assertRaises(DataIntegrityError):
                    declared_exercise(path, registration)


class PaidRunPreflightTest(unittest.TestCase):
    def arguments(self, runtime: str, scenario_set: Path, root: Path) -> list[str]:
        effort = ["--reasoning-effort", "high"] if runtime == "codex" else []
        return [
            "run",
            runtime,
            "--export",
            str(root / "export"),
            "--scenario-set",
            str(scenario_set),
            "--variant",
            "cand",
            "--runs",
            "1",
            "--model",
            "controlled-model",
            "--out",
            str(root / "out"),
            "--worlds",
            str(root / "worlds"),
            "--cap-usd",
            "5",
            *effort,
        ]

    def test_a_declared_surface_without_declared_exercise_refuses_before_any_reservation(self) -> None:
        for runtime, entry in (("claude", "run_claude"), ("codex", "run_codex")):
            with tempfile.TemporaryDirectory() as directory, self.subTest(runtime=runtime):
                root = Path(directory)
                scenario_set = write_set(root, None)
                command = cli.decode(
                    [*self.arguments(runtime, scenario_set, root), "--changed-tool", "pinboard_dispatch"]
                )
                output = io.StringIO()
                with (
                    patch.object(cli.runner, entry) as paid,
                    patch.object(cli.spend, "Budget") as budget,
                    patch.object(cli.export, "load_export") as loaded,
                    redirect_stdout(output),
                ):
                    self.assertEqual(2, cli.dispatch(command))
                paid.assert_not_called()
                budget.assert_not_called()
                loaded.assert_not_called()
                self.assertIn("no scenario in the set declares exercising them", output.getvalue())
                self.assertIn("No paid run started", output.getvalue())

    def test_declared_exercise_or_no_surface_lets_the_run_start(self) -> None:
        for runtime, entry in (("claude", "run_claude"), ("codex", "run_codex")):
            with tempfile.TemporaryDirectory() as directory, self.subTest(runtime=runtime):
                root = Path(directory)
                scenario_set = write_set(root, None)
                digest = hashlib.sha256(scenario_set.read_bytes()).hexdigest()
                exercise_path(scenario_set).parent.mkdir()
                exercise_path(scenario_set).write_text(json.dumps(declaration(digest, "s1")))
                surfaces: tuple[list[str], ...] = (["--changed-tool", "pinboard_dispatch"], [])
                for extra in surfaces:
                    command = cli.decode([*self.arguments(runtime, scenario_set, root), *extra])
                    output = io.StringIO()
                    with (
                        patch.object(cli.runner, entry, Mock(return_value=[])) as paid,
                        patch.object(cli.export, "load_export"),
                        patch.object(cli, "default_source"),
                        redirect_stdout(output),
                    ):
                        self.assertEqual(0, cli.dispatch(command))
                    paid.assert_called_once()
                    printed = output.getvalue()
                    if extra:
                        self.assertIn("declared exercised by s1 (pinboard_dispatch)", printed)
                        self.assertIn("observed calls to pinboard_dispatch:", printed)
                    else:
                        self.assertNotIn("tool use", printed)

    def test_the_surface_is_a_validated_option_only_on_run_claude_run_codex_and_compare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario_set = root / "set.json"
            agy = [*self.arguments("agy", scenario_set, root), "--reasoning-effort", "high"]
            for rejected in (
                [*agy, "--changed-tool", "pinboard_dispatch"],
                [*self.arguments("claude", scenario_set, root), "--changed-tool", "Bash"],
            ):
                with self.subTest(arguments=rejected), patch("sys.stderr"), self.assertRaises(SystemExit):
                    cli.decode(rejected)
            compared = cli.decode(
                [
                    "compare",
                    "--out",
                    "out",
                    "--scenario-set",
                    "set.json",
                    "--baseline",
                    "base",
                    "--candidate",
                    "cand",
                    "--changed-tool",
                    "pinboard_startup_text",
                    "--changed-tool",
                    "pinboard_evidence_page",
                ]
            )
            assert isinstance(compared, cli.Compare)
            self.assertEqual(frozenset({"pinboard_startup_text", "pinboard_evidence_page"}), compared.changed_tools)
            self.assertIsInstance(cli.decode(["tool-use", "--out", "out"]), cli.ToolUse)


if __name__ == "__main__":
    unittest.main()
