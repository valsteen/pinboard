"""Antigravity CLI driver and runner: CLI decoding, disposable HOME isolation, preflight provenance, multi-turn
resumption, conservative list-price accounting, failed turns, world instruction file and Codex-only substance."""

import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never
from unittest import mock

import msgspec

from evals.behavioral import agy_driver, cli, decision, export, processes, runner, spend, substance, world
from evals.behavioral.layout import SCORER_INPUT, Layout
from evals.behavioral.processes import Completed as ProcessResult
from evals.behavioral.processes import Window
from evals.behavioral.records import (
    AgyEffort,
    AntigravityRunDetails,
    ClaudeRunDetails,
    Completed,
    ExportRecord,
    Failed,
    PermissionDenial,
    RunRecord,
    Runtime,
    Scenario,
    ScorerInput,
    SeededItem,
    Stopped,
)
from evals.behavioral.scenarios import RegisteredSet, load_set
from evals.behavioral.spend import Budget, Category

type Json = str | int | float | bool | list[Json] | dict[str, Json] | None


def create_exported_plugin(root: Path) -> ExportRecord:
    plugin = root / "exported-plugin"
    skill_dir = plugin / "skills" / "pinboard"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Pinboard Skill\n")
    return ExportRecord(
        schema="pinboard-behavioral-export/v1",
        commit="a" * 40,
        skills_sha256=export.skills_sha256(plugin),
        plugin_root=str(plugin),
    )


def turn_json(
    *,
    status: str = "SUCCESS",
    response: str = "Final answer.",
    conversation_id: str = "conv-123",
    input_tokens: int = 1_000,
    output_tokens: int = 200,
    thinking_tokens: int = 50,
    cache_read_tokens: int = 400,
    denied_actions: Sequence[Mapping[str, str]] | None = None,
) -> str:
    total_tokens = input_tokens + output_tokens + thinking_tokens + cache_read_tokens
    payload: dict[str, Json] = {
        "conversation_id": conversation_id,
        "status": status,
        "response": response,
        "duration_seconds": 1.2,
        "num_turns": 1,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "cache_read_tokens": cache_read_tokens,
            "total_tokens": total_tokens,
        },
        "extra_additive_field": "ignored",
    }
    if denied_actions is not None:
        payload["denied_actions"] = [dict(entry) for entry in denied_actions]
    return json.dumps(payload)


type PreflightCase = Literal[
    "empty-version",
    "no-plugins",
    "disabled-plugin",
    "wrong-name",
    "wrong-source",
    "outside-home",
    "skills-sha-mismatch",
    "missing-skills-dir",
    "wrong-mcp",
    "missing-hook",
    "missing-agents-md",
]


@dataclass(frozen=True)
class RecordedCall:
    tool: processes.Tool
    arguments: list[str]
    cwd: Path
    environment: dict[str, str]


class FakeAgy:
    """Fake ``processes.run_tool`` boundary for ``Tool.AGY``."""

    def __init__(self, turn_results: list[ProcessResult]) -> None:
        self.turn_results = list(turn_results)
        self.calls: list[RecordedCall] = []
        self.seen_homes: list[tuple[Path, bool, list[str]]] = []

    def __call__(
        self,
        tool: processes.Tool,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdin: str | None,
        timeout_seconds: float,
        window: Window,
    ) -> ProcessResult:
        del stdin
        window.timeout(timeout_seconds)
        args = list(arguments)
        env = dict(environment)
        self.calls.append(RecordedCall(tool, args, cwd, env))
        if args == ["--version"]:
            return ProcessResult(0, "Antigravity 2.04.8\nAntigravity CLI 1.4.8\n", "", False)
        home = Path(env["HOME"])
        if args[:2] == ["plugin", "install"]:
            source = Path(args[2])
            dest = home / ".gemini" / "config" / "plugins" / "pinboard"
            dest.parent.mkdir(parents=True, exist_ok=True)
            for item in source.rglob("*"):
                rel = item.relative_to(source)
                target = dest / rel
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(item.read_bytes())
            return ProcessResult(0, "Installed plugin pinboard\n", "", False)
        if args == ["plugin", "list", "--json"]:
            dest = home / ".gemini" / "config" / "plugins" / "pinboard"
            entries = [
                {
                    "name": "pinboard",
                    "enabled": True,
                    "version": "0.1.0+antigravity",
                    "source": "antigravity",
                    "path": str(dest),
                    "skills": ["pinboard"],
                    "mcpServers": ["pinboard"],
                    "hooks": ["pinboard"],
                }
            ]
            return ProcessResult(0, json.dumps(entries), "", False)
        if args and args[0] == "-p":
            settings_file = home / ".gemini" / "antigravity-cli" / "settings.json"
            allow = json.loads(settings_file.read_text())["permissions"]["allow"] if settings_file.is_file() else []
            self.seen_homes.append((home, (home / "Library" / "Keychains").is_symlink(), allow))
            return self.turn_results.pop(0)
        raise AssertionError(f"unexpected agy arguments: {args}")


class AgyCliTest(unittest.TestCase):
    BASE_ARGS = (
        "--export",
        "exp",
        "--scenario-set",
        "set.json",
        "--variant",
        "cand",
        "--runs",
        "2",
        "--out",
        "out",
        "--worlds",
        "worlds",
        "--cap-usd",
        "5.0",
    )

    def test_run_agy_requires_model_and_reasoning_effort_and_leaves_siblings_unchanged(self) -> None:
        agy_cmd = cli.decode(
            [
                "run",
                "agy",
                *self.BASE_ARGS,
                "--model",
                "claude-sonnet-5-5",
                "--reasoning-effort",
                "high",
                "--jobs",
                "2",
            ]
        )
        assert isinstance(agy_cmd, cli.RunAgy)
        self.assertEqual("claude-sonnet-5-5", agy_cmd.model)
        self.assertIs(AgyEffort.HIGH, agy_cmd.reasoning_effort)
        self.assertEqual(2, agy_cmd.jobs)
        self.assertEqual(1, agy_cmd.first_index)

        claude_cmd = cli.decode(["run", "claude", *self.BASE_ARGS, "--model", "claude-sonnet-4-6"])
        assert isinstance(claude_cmd, cli.RunClaude)
        self.assertIsNone(claude_cmd.reasoning_effort)

        codex_cmd = cli.decode(["run", "codex", *self.BASE_ARGS, "--model", "gpt-5.4", "--reasoning-effort", "high"])
        assert isinstance(codex_cmd, cli.RunCodex)
        self.assertEqual("high", codex_cmd.reasoning_effort)

        probe_cmd = cli.decode(
            [
                "probe",
                "codex",
                "--export",
                "exp",
                "--name",
                "p1",
                "--model",
                "gpt-5.4",
                "--reasoning-effort",
                "high",
                "--out",
                "out",
                "--worlds",
                "worlds",
                "--cap-usd",
                "1.0",
            ]
        )
        assert isinstance(probe_cmd, cli.ProbeCodex)
        self.assertEqual("p1", probe_cmd.name)

        for bad_args in (
            ["run", "agy", *self.BASE_ARGS, "--model", "claude-sonnet-5-5"],
            ["run", "agy", *self.BASE_ARGS, "--reasoning-effort", "high"],
            ["run", "agy", *self.BASE_ARGS, "--model", "claude-sonnet-5-5", "--reasoning-effort", "minimal"],
        ):
            with self.subTest(bad_args=bad_args), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                cli.decode(bad_args)


class AgyDriverAndRunnerTest(unittest.TestCase):
    def make_plan(
        self,
        root: Path,
        exported: ExportRecord,
        *,
        model: str = "claude-sonnet-5-5",
        effort: AgyEffort = AgyEffort.MEDIUM,
    ) -> runner.AgyPlan:
        worlds = root / "worlds"
        worlds.mkdir(exist_ok=True)
        registered = load_set(Path("evals/behavioral/data/scenario-sets/s13-s17.json"))
        # s13-opportunity-separate-script is a two-turn scenario in s13-s17.json.
        two_turn = next(s for s in registered.scenarios if len(s.turns) == 2)
        run_plan = runner.RunPlan(
            layout=Layout(root / "out"),
            worlds=worlds,
            export=exported,
            variant="cand",
            scenarios=RegisteredSet((two_turn,), registered.registration, registered.scenario_sources),
            first_index=1,
            runs=1,
            model=model,
            window=Window(None),
        )
        return runner.AgyPlan(run=run_plan, reasoning_effort=effort)

    def stub_world(self, stack: ExitStack) -> None:
        def fake_build_world(
            world_root: Path,
            scenario: Scenario,
            runtime: Runtime,
            plugin_root: Path,
            owner: str,
            window: Window,
        ) -> tuple[world.World, list[SeededItem]]:
            del scenario, owner
            world_root.mkdir(parents=True, exist_ok=False)
            project = world_root / "tally"
            project.mkdir(parents=True, exist_ok=True)
            (project / "AGENTS.md").write_text(world.agents_file(runtime))
            built = world.World(
                root=world_root,
                project=project,
                origin=world_root / "origin.git",
                scratch_board=None,
                launcher=plugin_root / "scripts" / "pinboard",
                window=window,
            )
            return built, []

        def fake_snapshot(built: world.World, name: str) -> world.ObservedState:
            return world.ObservedState(name=name, text=f"snapshot {built.project}\n")

        stack.enter_context(mock.patch.object(runner.world, "build_world", side_effect=fake_build_world))
        stack.enter_context(mock.patch.object(runner.world, "run_hook", return_value="hook-ran\n"))
        stack.enter_context(mock.patch.object(runner.world, "snapshot", side_effect=fake_snapshot))

    def test_isolated_home_symlinks_only_keychains_writes_settings_and_honors_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_real_home = root / "real-home"
            (fake_real_home / "Library" / "Keychains").mkdir(parents=True)
            (fake_real_home / ".gemini" / "extensions").mkdir(parents=True)
            disposable = root / "disposable-home"
            with mock.patch.object(Path, "home", return_value=fake_real_home):
                with agy_driver.isolated_home(disposable) as home:
                    self.assertEqual(disposable, home)
                    self.assertTrue((home / "Library" / "Keychains").is_symlink())
                    self.assertEqual(
                        (fake_real_home / "Library" / "Keychains").resolve(),
                        (home / "Library" / "Keychains").resolve(),
                    )
                    self.assertFalse((home / ".gemini" / "extensions").exists())
                    settings = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text())
                    self.assertEqual({"permissions": {"allow": list(agy_driver.PERMISSIONS_ALLOW)}}, settings)
                self.assertFalse(disposable.exists())

                retained = root / "retained-home"
                with self.assertRaises(processes.CleanupUnconfirmed), agy_driver.isolated_home(retained):
                    raise processes.CleanupUnconfirmed(999, b"out", b"err")
                self.assertTrue(retained.exists())

    def test_multi_turn_run_resumes_conversation_differences_usage_and_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            exported = create_exported_plugin(root)
            plan = self.make_plan(root, exported, model="claude-sonnet-5-5", effort=AgyEffort.HIGH)
            self.stub_world(stack)
            fake = FakeAgy(
                [
                    ProcessResult(
                        0,
                        turn_json(
                            response="Turn 1 reply.",
                            conversation_id="conv-abc",
                            input_tokens=1_000,
                            output_tokens=200,
                            thinking_tokens=100,
                            cache_read_tokens=500,
                            denied_actions=[{"action": "command", "display_name": "RunCommand"}],
                        ),
                        "[agy] MCP tool alpha auto-denied. Add an allow-rule under permissions.allow\n",
                        False,
                    ),
                    ProcessResult(
                        0,
                        turn_json(
                            response="Turn 2 reply.",
                            conversation_id="conv-abc",
                            input_tokens=3_000,
                            output_tokens=500,
                            thinking_tokens=300,
                            cache_read_tokens=1_500,
                        ),
                        "",
                        False,
                    ),
                ]
            )
            stack.enter_context(mock.patch.object(processes, "run_tool", side_effect=fake))
            stack.enter_context(
                mock.patch.dict(
                    os.environ, {"SECRET_TOKEN": "do-not-leak", "PATH": "/usr/bin", "USER": "tester"}, clear=True
                )
            )
            budget = Budget(plan.run.layout, 5.0, Window(None), False)
            skipped = runner.run_agy(plan, budget, jobs=1)
            self.assertEqual([], skipped)

            (record,) = plan.run.layout.run_records()
            self.assertIs(Runtime.ANTIGRAVITY, record.runtime)
            self.assertEqual("Antigravity 2.04.8 / Antigravity CLI 1.4.8", record.cli_version)
            self.assertEqual("claude-sonnet-5-5", record.model)
            self.assertIsInstance(record.outcome, Completed)
            assert isinstance(record.details, AntigravityRunDetails)
            self.assertIs(AgyEffort.HIGH, record.details.reasoning_effort)
            self.assertIsNone(record.details.observed_model)
            self.assertEqual(list(agy_driver.PERMISSIONS_ALLOW), record.details.permissions_allow)
            self.assertEqual(agy_driver.PRICE_SOURCE, record.details.price_source)

            # Check environment isolation and inventory
            env_entries = [e for e in record.inventory if e.name == "host-environment"]
            self.assertEqual(1, len(env_entries))
            self.assertNotIn("SECRET_TOKEN", env_entries[0].source)
            self.assertNotIn("do-not-leak", env_entries[0].source)
            self.assertIn("HOME", env_entries[0].source)
            kinds = {e.kind for e in record.inventory}
            self.assertEqual({"plugin", "skill", "mcp-server", "hook", "instruction-file", "runtime-bundled"}, kinds)

            # Check conversation resumption and CLI flags; every Tool.AGY call runs with agy_environment(home)
            disposable_home = fake.seen_homes[0][0]
            for call in fake.calls:
                self.assertIs(processes.Tool.AGY, call.tool)
                self.assertEqual(processes.agy_environment(disposable_home), call.environment)
                self.assertNotIn("SECRET_TOKEN", call.environment)
            chat_calls = [c for c in fake.calls if c.arguments and c.arguments[0] == "-p"]
            self.assertEqual(2, len(chat_calls))
            self.assertNotIn("--conversation", chat_calls[0].arguments)
            self.assertIn("--conversation", chat_calls[1].arguments)
            conv_idx = chat_calls[1].arguments.index("--conversation")
            self.assertEqual("conv-abc", chat_calls[1].arguments[conv_idx + 1])
            for call in chat_calls:
                self.assertIn("--effort", call.arguments)
                self.assertEqual("high", call.arguments[call.arguments.index("--effort") + 1])

            # Disposable home existed during turn execution and was removed after run
            self.assertEqual(2, len(fake.seen_homes))
            self.assertEqual(list(agy_driver.PERMISSIONS_ALLOW), fake.seen_homes[0][2])
            self.assertFalse(disposable_home.exists())
            self._assert_multi_turn_accounting_and_outputs(plan, budget, record)

    def _assert_multi_turn_accounting_and_outputs(
        self, plan: runner.AgyPlan, budget: Budget, record: RunRecord
    ) -> None:
        # Check per-turn token deltas and conservative list-price costs for claude-sonnet-5-5 ($3/$0.30/$15 per 1M)
        self.assertEqual(2, len(record.turns))
        t1, t2 = record.turns
        self.assertEqual("conv-abc", t1.session_id)
        self.assertEqual(1_000, t1.uncached_input_tokens)
        self.assertEqual(500, t1.cached_input_tokens)
        self.assertEqual(300, t1.output_tokens)
        self.assertEqual(100, t1.reasoning_output_tokens)
        expected_t1 = (1_000 * 3.0 + 500 * 0.30 + 300 * 15.0) / 1_000_000
        self.assertAlmostEqual(expected_t1, t1.cost_usd or 0.0, places=9)
        self.assertEqual(
            [
                PermissionDenial(tool="RunCommand", detail="command"),
                PermissionDenial(
                    tool="agy-permissions",
                    detail="[agy] MCP tool alpha auto-denied. Add an allow-rule under permissions.allow",
                ),
            ],
            t1.permission_denials,
        )
        self.assertEqual([], t2.permission_denials)

        self.assertEqual(2_000, t2.uncached_input_tokens)
        self.assertEqual(1_000, t2.cached_input_tokens)
        self.assertEqual(500, t2.output_tokens)
        self.assertEqual(200, t2.reasoning_output_tokens)
        expected_t2 = (2_000 * 3.0 + 1_000 * 0.30 + 500 * 15.0) / 1_000_000
        self.assertAlmostEqual(expected_t2, t2.cost_usd or 0.0, places=9)

        # Scorer input was written and spend report attributes to Antigravity category
        scorer_path = plan.run.layout.run_directory(record.run) / SCORER_INPUT
        self.assertTrue(scorer_path.is_file())
        decoded_input = msgspec.json.decode(scorer_path.read_bytes(), type=ScorerInput)
        self.assertEqual(["Turn 1 reply.", "Turn 2 reply."], decoded_input.replies)

        recorded_items = spend.items(plan.run.layout)
        self.assertEqual(1, len(recorded_items))
        self.assertIs(Category.ANTIGRAVITY_AGENT_RUN, recorded_items[0].category)
        self.assertAlmostEqual(expected_t1 + expected_t2, recorded_items[0].usd, places=9)
        self.assertFalse(recorded_items[0].unknown)
        self.assertIn("agent runs (antigravity)\t1\t", spend.report(plan.run.layout))

        # Substance assessment skips Antigravity runs
        with mock.patch.object(substance.oneshot, "ask") as assess_mock:
            skipped_assessments = substance.assess(plan.run.layout, budget, substance.ASSESSOR_MODEL)
        self.assertEqual([], skipped_assessments)
        assess_mock.assert_not_called()

        # decision.conditions distinguishes AntigravityRunDetails fields and runtime
        assert isinstance(record.details, AntigravityRunDetails)
        cond = decision.conditions(record)
        self.assertEqual(decision.conditions(msgspec.structs.replace(record)), cond)
        self.assertNotEqual(
            decision.conditions(msgspec.structs.replace(record, model="claude-opus-5-5")),
            cond,
        )
        self.assertNotEqual(
            decision.conditions(
                msgspec.structs.replace(
                    record, details=msgspec.structs.replace(record.details, reasoning_effort=AgyEffort.LOW)
                )
            ),
            cond,
        )
        self.assertNotEqual(
            decision.conditions(
                msgspec.structs.replace(
                    record,
                    details=msgspec.structs.replace(record.details, permissions_allow=["mcp(pinboard_pinboard/*)"]),
                )
            ),
            cond,
        )
        self.assertNotEqual(
            decision.conditions(
                msgspec.structs.replace(
                    record,
                    runtime=Runtime.CLAUDE_CODE,
                    details=ClaudeRunDetails(
                        permission_mode="bypassPermissions",
                        cost_basis="claude total_cost_usd",
                        reasoning_effort=None,
                    ),
                )
            ),
            cond,
        )

    def test_unpriced_model_and_budget_cap_prevent_turns_from_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exported = create_exported_plugin(root)
            unpriced_plan = self.make_plan(root, exported, model="unknown-model-99")
            with mock.patch.object(processes, "run_tool") as run_mock:
                with self.assertRaises(agy_driver.AgyUnavailableError):
                    runner.run_agy(unpriced_plan, Budget(unpriced_plan.run.layout, 5.0, Window(None), False), jobs=1)
                run_mock.assert_not_called()

            priced_plan = self.make_plan(root, exported, model="gemini-3.8-flash")
            with mock.patch.object(processes, "run_tool") as run_mock:
                skipped = runner.run_agy(priced_plan, Budget(priced_plan.run.layout, 0.10, Window(None), False), jobs=1)
                self.assertEqual(1, len(skipped))
                run_mock.assert_not_called()

        # Also test long-context pricing (>200K input + cache_read)
        long_usage = agy_driver.AgyUsage(
            input_tokens=150_000,
            output_tokens=1_000,
            thinking_tokens=500,
            cache_read_tokens=60_000,
            total_tokens=211_500,
        )
        # gemini-4-argon-high long rates: $4 input, $0.40 cached, $18 output
        expected_long = (150_000 * 4.0 + 60_000 * 0.40 + 1_500 * 18.0) / 1_000_000
        self.assertAlmostEqual(expected_long, agy_driver.turn_cost("gemini-4-argon-high", long_usage), places=9)

    @staticmethod
    def _tamper_preflight(
        selected_case: PreflightCase, result: ProcessResult, home: Path, root: Path, scenario_id: str
    ) -> ProcessResult:
        dest = home / ".gemini" / "config" / "plugins" / "pinboard"
        data = json.loads(result.stdout)
        match selected_case:
            case "empty-version":
                pass
            case "no-plugins":
                return ProcessResult(0, "[]", "", False)
            case "disabled-plugin":
                data[0]["enabled"] = False
            case "wrong-name":
                data[0]["name"] = "other"
            case "wrong-source":
                data[0]["source"] = "foreign"
            case "outside-home":
                data[0]["path"] = str(root / "outside")
            case "skills-sha-mismatch":
                (dest / "skills" / "pinboard" / "SKILL.md").write_text("tampered\n")
            case "missing-skills-dir":
                shutil.rmtree(dest / "skills")
            case "wrong-mcp":
                data[0]["mcpServers"] = ["foreign"]
            case "missing-hook":
                data[0]["hooks"] = []
            case "missing-agents-md":
                (root / "worlds" / f"{scenario_id}-cand-1" / "tally" / "AGENTS.md").unlink()
            case _ as unreachable:
                assert_never(unreachable)
        return ProcessResult(0, json.dumps(data), "", False)

    def test_preflight_provenance_mismatches_stop_before_any_agent_turn(self) -> None:
        cases: tuple[PreflightCase, ...] = (
            "empty-version",
            "no-plugins",
            "disabled-plugin",
            "wrong-name",
            "wrong-source",
            "outside-home",
            "skills-sha-mismatch",
            "missing-skills-dir",
            "wrong-mcp",
            "missing-hook",
            "missing-agents-md",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                exported = create_exported_plugin(root)
                plan = self.make_plan(root, exported)
                scenario_id = plan.run.scenarios.scenarios[0].id
                self.stub_world(stack)
                fake = FakeAgy([ProcessResult(0, turn_json(), "", False)])

                def custom_run_tool(
                    tool: processes.Tool,
                    arguments: Sequence[str],
                    *,
                    cwd: Path,
                    environment: Mapping[str, str],
                    stdin: str | None,
                    timeout_seconds: float,
                    window: Window,
                    selected_case: PreflightCase = case,
                    current_root: Path = root,
                    current_scenario_id: str = scenario_id,
                    delegate: FakeAgy = fake,
                ) -> ProcessResult:
                    result = delegate(
                        tool,
                        arguments,
                        cwd=cwd,
                        environment=environment,
                        stdin=stdin,
                        timeout_seconds=timeout_seconds,
                        window=window,
                    )
                    if selected_case == "empty-version" and list(arguments) == ["--version"]:
                        return ProcessResult(0, "   \n", "", False)
                    home = Path(environment["HOME"])
                    if list(arguments) == ["plugin", "list", "--json"]:
                        return self._tamper_preflight(selected_case, result, home, current_root, current_scenario_id)
                    return result

                stack.enter_context(mock.patch.object(processes, "run_tool", side_effect=custom_run_tool))
                with self.assertRaises(runner.IsolationBreachError):
                    runner.run_agy(plan, Budget(plan.run.layout, 5.0, Window(None), False), jobs=1)
                chat_calls = [c for c in fake.calls if c.arguments and c.arguments[0] == "-p"]
                self.assertEqual([], chat_calls)
                (record,) = plan.run.layout.run_records()
                self.assertIsInstance(record.outcome, Stopped)
                self.assertEqual([], record.turns)
                self.assertFalse((plan.run.layout.run_directory(record.run) / SCORER_INPUT).exists())

    def test_failed_malformed_and_timed_out_turns_fail_run_without_scorer_input(self) -> None:
        bad_turn_cases: tuple[tuple[str, ProcessResult], ...] = (
            ("non-success-status", ProcessResult(0, turn_json(status="ERROR"), "", False)),
            ("nonzero-exit", ProcessResult(1, turn_json(status="SUCCESS"), "fatal error", False)),
            ("malformed-json-zero", ProcessResult(0, "{not-json", "", False)),
            ("malformed-json-nonzero", ProcessResult(1, "{not-json", "crash", False)),
            ("empty-conversation-id", ProcessResult(0, turn_json(conversation_id="   "), "", False)),
            ("empty-response", ProcessResult(0, turn_json(response="   "), "", False)),
            (
                "negative-tokens",
                ProcessResult(0, turn_json(input_tokens=-1), "", False),
            ),
            (
                "print-timeout-stderr",
                ProcessResult(
                    0,
                    turn_json(response="partial"),
                    "[agy] print timeout after 5m0s with turn in progress; returning partial output\n",
                    False,
                ),
            ),
            ("subprocess-timed-out", ProcessResult(0, turn_json(), "", True)),
        )
        for label, bad_result in bad_turn_cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                exported = create_exported_plugin(root)
                plan = self.make_plan(root, exported)
                self.stub_world(stack)
                fake = FakeAgy([bad_result])
                stack.enter_context(mock.patch.object(processes, "run_tool", side_effect=fake))
                runner.run_agy(plan, Budget(plan.run.layout, 5.0, Window(None), False), jobs=1)
                (record,) = plan.run.layout.run_records()
                self.assertIsInstance(record.outcome, Failed)
                self.assertEqual(1, len(record.turns))
                self.assertIsNone(record.turns[0].cost_usd)
                self.assertTrue(spend.main_usage_unknown(plan.run.layout))
                self.assertFalse((plan.run.layout.run_directory(record.run) / SCORER_INPUT).exists())

        # Also test conversation_id drift and decreasing cumulative usage on turn 2
        for second_turn in (
            turn_json(conversation_id="conv-different", input_tokens=2_000),
            turn_json(conversation_id="conv-123", input_tokens=500),
        ):
            with self.subTest(second_turn=second_turn), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                exported = create_exported_plugin(root)
                plan = self.make_plan(root, exported)
                self.stub_world(stack)
                fake = FakeAgy(
                    [
                        ProcessResult(0, turn_json(input_tokens=1_000), "", False),
                        ProcessResult(0, second_turn, "", False),
                    ]
                )
                stack.enter_context(mock.patch.object(processes, "run_tool", side_effect=fake))
                runner.run_agy(plan, Budget(plan.run.layout, 5.0, Window(None), False), jobs=1)
                (record,) = plan.run.layout.run_records()
                assert isinstance(record.outcome, Failed)
                self.assertEqual("turn 2", record.outcome.stage)
                self.assertIsNotNone(record.turns[0].cost_usd)
                self.assertIsNone(record.turns[1].cost_usd)

    def test_world_instruction_file_and_cli_dispatch_surface(self) -> None:
        claude_agents = world.agents_file(Runtime.CLAUDE_CODE)
        codex_agents = world.agents_file(Runtime.CODEX)
        agy_agents = world.agents_file(Runtime.ANTIGRAVITY)
        self.assertEqual(codex_agents, agy_agents)
        self.assertGreater(len(agy_agents), len(claude_agents))
        diff = world.fixture_difference(Runtime.ANTIGRAVITY)
        assert diff is not None
        self.assertIn("Antigravity loads directory-based workspace rules from AGENTS.md", diff)

        # Verify cli.dispatch for RunAgy both on normal completion and on AgyUnavailableError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exported = create_exported_plugin(root)
            cmd = cli.RunAgy(
                export=root / "exp",
                scenario_set=Path("evals/behavioral/data/scenario-sets/s13-s17.json"),
                variant="cand",
                runs=1,
                first_index=1,
                model="claude-sonnet-5-5",
                reasoning_effort=AgyEffort.HIGH,
                out=root / "out",
                worlds=root / "worlds",
                cap_usd=5.0,
                jobs=1,
            )
            with (
                mock.patch.object(cli.export, "load_export", return_value=exported),
                mock.patch.object(cli.runner, "run_agy", return_value=[]),
            ):
                self.assertEqual(0, cli.dispatch(cmd))
            with (
                mock.patch.object(cli.export, "load_export", return_value=exported),
                mock.patch.object(cli.runner, "run_agy", side_effect=agy_driver.AgyUnavailableError("no agy binary")),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(2, cli.dispatch(cmd))

    def test_driver_helper_failures_and_runner_process_interruptions(self) -> None:
        window = Window(None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            retained_home = root / "retained-home"
            retained_home.mkdir()

            # isolated_home removes home on ordinary exception, keeps it on CleanupUnconfirmed
            err_home = root / "err-home"
            with self.assertRaises(RuntimeError), agy_driver.isolated_home(err_home):
                self.assertTrue(err_home.is_dir())
                raise RuntimeError("boom")
            self.assertFalse(err_home.exists())

            unconfirmed_home = root / "unconfirmed-home"
            with (
                self.assertRaises(processes.CleanupUnconfirmed),
                agy_driver.isolated_home(unconfirmed_home),
            ):
                raise processes.CleanupUnconfirmed(123, b"out", b"err")
            self.assertTrue(unconfirmed_home.is_dir())

            # agy_version, install_plugin, and installed_plugins failure branches
            for bad in (
                ProcessResult(1, "", "err", False),
                ProcessResult(0, "0.2.3", "", True),
            ):
                with (
                    mock.patch.object(processes, "run_tool", return_value=bad),
                    self.assertRaises(agy_driver.AgyUnavailableError),
                ):
                    agy_driver.agy_version(retained_home, window)
            with mock.patch.object(processes, "run_tool", return_value=ProcessResult(0, "   \n", "", False)):
                self.assertEqual("", agy_driver.agy_version(retained_home, window))

            with (
                mock.patch.object(processes, "run_tool", return_value=ProcessResult(1, "", "install failed", False)),
                self.assertRaises(agy_driver.AgyUnavailableError),
            ):
                agy_driver.install_plugin(retained_home, root, window)

            for bad_list in (
                ProcessResult(1, "", "list failed", False),
                ProcessResult(0, "{bad-json", "", False),
            ):
                with (
                    mock.patch.object(processes, "run_tool", return_value=bad_list),
                    self.assertRaises(agy_driver.AgyUnavailableError),
                ):
                    agy_driver.installed_plugins(retained_home, window)

            with mock.patch.object(processes, "run_tool", return_value=ProcessResult(0, "null\n", "", False)):
                self.assertEqual([], agy_driver.installed_plugins(retained_home, window))

            # ProcessIncomplete and CleanupUnconfirmed during a turn record Failed and re-raise
            for exc, fail_raw_write in (
                (processes.ProcessIncomplete(b"partial-out", b"partial-err", RuntimeError("incomplete")), False),
                (processes.CleanupUnconfirmed(999, b"unconfirmed-out", b"unconfirmed-err"), False),
                (processes.CleanupUnconfirmed(999, b"unconfirmed-out", b"unconfirmed-err"), True),
            ):
                with ExitStack() as stack:
                    sub_root = root / f"{type(exc).__name__}-{fail_raw_write}"
                    sub_root.mkdir()
                    exported = create_exported_plugin(sub_root)
                    plan = self.make_plan(sub_root, exported)
                    self.stub_world(stack)
                    fake = FakeAgy([])

                    def interrupt_on_chat(
                        tool: processes.Tool,
                        arguments: Sequence[str],
                        *,
                        cwd: Path,
                        environment: Mapping[str, str],
                        stdin: str | None,
                        timeout_seconds: float,
                        window: Window,
                        raised: Exception = exc,
                        make_raw_dir: bool = fail_raw_write,
                        current_plan: runner.AgyPlan = plan,
                        delegate: FakeAgy = fake,
                    ) -> ProcessResult:
                        if list(arguments)[:1] == ["-p"]:
                            if make_raw_dir:
                                _, key = current_plan.run.planned()[0]
                                (current_plan.run.layout.run_directory(key) / "turn-1.json").mkdir(parents=True)
                            raise raised
                        return delegate(
                            tool,
                            arguments,
                            cwd=cwd,
                            environment=environment,
                            stdin=stdin,
                            timeout_seconds=timeout_seconds,
                            window=window,
                        )

                    stack.enter_context(mock.patch.object(processes, "run_tool", side_effect=interrupt_on_chat))
                    with self.assertRaises(type(exc)) as caught:
                        runner.run_agy(plan, Budget(plan.run.layout, 5.0, Window(None), False), jobs=1)
                    if fail_raw_write:
                        self.assertIsInstance(caught.exception.__cause__, OSError)
                        self.assertTrue(
                            any("raw turn output write failed" in note for note in caught.exception.__notes__)
                        )
                        disposable_home = Path(fake.calls[0].environment["HOME"])
                        self.assertTrue(disposable_home.is_dir())
                    (rec,) = plan.run.layout.run_records()
                    self.assertIsInstance(rec.outcome, Failed)


if __name__ == "__main__":
    unittest.main()
