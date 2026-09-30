"""Codex output reading: replies, commentary, refusals and token pricing from synthetic event text."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.behavioral import codex_driver, processes, runner, world
from evals.behavioral.layout import Layout
from evals.behavioral.records import Completed, ExportRecord, RunKey, Scenario, Stopped, Turn, WorldKind
from evals.behavioral.scenarios import RegisteredSet

type Json = str | int | bool | list[Json] | dict[str, Json] | None


def event(body: dict[str, Json]) -> str:
    return json.dumps(body)


class ReadEventsTest(unittest.TestCase):
    def test_the_last_agent_message_is_the_final_reply_and_earlier_ones_are_commentary(self) -> None:
        stream = "\n".join(
            [
                event({"type": "thread.started", "thread_id": "t-1"}),
                event({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Checking."}}),
                event({"type": "item.completed", "item": {"id": "b", "type": "reasoning", "text": "hidden"}}),
                event({"type": "item.completed", "item": {"id": "c", "type": "agent_message", "text": "Done."}}),
                event(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 4,
                            "cache_write_input_tokens": 1,
                            "output_tokens": 2,
                            "reasoning_output_tokens": 1,
                            "added_later": 3,
                        },
                    }
                ),
                event({"type": "some.future.event", "detail": 1}),
            ]
        )
        reading = codex_driver.read_events(stream)
        self.assertEqual("t-1", reading.thread_id)
        self.assertEqual(["Checking.", "Done."], reading.messages)
        assert reading.usage is not None
        self.assertEqual(5, reading.usage.uncached_input_tokens)

    def test_an_mcp_call_the_approval_policy_rejects_is_a_denial(self) -> None:
        stream = event(
            {
                "type": "item.completed",
                "item": {
                    "id": "m",
                    "type": "mcp_tool_call",
                    "server": "pinboard",
                    "tool": "pinboard_overview",
                    "status": "failed",
                    "error": {"message": "MCP tool call requires approval, but approval policy is never"},
                },
            }
        )
        reading = codex_driver.read_events(stream)
        self.assertTrue(reading.mcp_approval_denied)
        self.assertEqual("pinboard.pinboard_overview", reading.denials[0].tool)

    def test_a_refused_git_write_is_a_denial(self) -> None:
        stream = event(
            {
                "type": "item.completed",
                "item": {
                    "id": "g",
                    "type": "command_execution",
                    "command": "git merge pinboard/sort-flag",
                    "aggregated_output": "fatal: Unable to create '/w/tally/.git/index.lock': Operation not permitted",
                    "exit_code": 128,
                    "status": "failed",
                },
            }
        )
        self.assertTrue(codex_driver.read_events(stream).git_write_denied)


def native_rollout(outcome: str) -> str:
    """A source-supported guardian decision fixture; actual experimental evidence observed allows only."""
    records: list[dict[str, Json]] = [
        {"type": "turn_context", "payload": {"turn_id": "primary", "model": "gpt-6-sol"}},
        {"type": "turn_context", "payload": {"turn_id": "review", "model": "codex-auto-review"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "failed-git",
                "internal_chat_message_metadata_passthrough": {"turn_id": "primary"},
                "output": [
                    {
                        "type": "input_text",
                        "text": event(
                            {
                                "exit_code": 128,
                                "output": "fatal: Unable to create '/w/tally/.git/index.lock': Operation not permitted",
                            }
                        ),
                    }
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "guardian",
                "turn_id": "review",
                "item": {
                    "type": "AgentMessage",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "Text",
                            "text": event(
                                {
                                    "outcome": outcome,
                                    "risk_level": "medium",
                                    "user_authorization": "high",
                                    "rationale": "exact action decision",
                                }
                            ),
                        }
                    ],
                },
            },
        },
    ]
    return "\n".join(event(r) for r in records) + "\n"


class RolloutRefusalTest(unittest.TestCase):
    def test_recovered_sandbox_failure_remains_evidence_without_a_terminal_rejection(self) -> None:
        reading = codex_driver.rollout_refusals(native_rollout("allow"))
        self.assertEqual(1, len(reading.git_writes))
        self.assertEqual([], reading.approvals)

    def test_a_genuine_reviewer_denial_stops_but_quoted_history_does_not(self) -> None:
        reading = codex_driver.rollout_refusals(native_rollout("deny"))
        self.assertEqual(["exact action decision"], reading.approvals)
        quoted = event(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": native_rollout("deny")}],
                },
            }
        )
        self.assertEqual([], codex_driver.rollout_refusals(quoted).approvals)

    def test_guidance_and_successful_output_that_quotes_a_failure_are_not_refusals(self) -> None:
        quoted = native_rollout("allow").replace('\\"exit_code\\": 128', '\\"exit_code\\": 0')
        self.assertEqual([], codex_driver.rollout_refusals(quoted).git_writes)
        text = event(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Approval policy is never; .git remains read-only"}],
                },
            }
        )
        self.assertEqual([], codex_driver.rollout_refusals(text).approvals)


def completed_turn(input_tokens: int, cached: int, output: int, reasoning: int) -> str:
    return "\n".join(
        [
            event({"type": "thread.started", "thread_id": "t-1"}),
            event({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "Reply."}}),
            event(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached,
                        "cache_write_input_tokens": 0,
                        "output_tokens": output,
                        "reasoning_output_tokens": reasoning,
                    },
                }
            ),
        ]
    )


class ResumedUsageTest(unittest.TestCase):
    def test_a_resumed_turn_records_only_its_own_share_of_the_thread_totals(self) -> None:
        first = codex_driver.read_events(completed_turn(115_230, 96_256, 620, 111))
        second = codex_driver.read_events(completed_turn(1_421_314, 1_357_824, 7_709, 3_564))
        turns = []
        previous: codex_driver.Usage | None = None
        for index, reading in enumerate([first, second], start=1):
            turns.append(
                codex_driver.turn_evidence(index, "question", None, "t-1", reading, previous, "gpt-6-sol", "t0")
            )
            previous = reading.usage
        self.assertEqual(1_261_568, turns[1].cached_input_tokens)
        self.assertEqual(1_306_084 - 1_261_568, turns[1].uncached_input_tokens)
        self.assertEqual(7_089, turns[1].output_tokens)
        self.assertEqual(3_453, turns[1].reasoning_output_tokens)
        assert second.usage is not None
        self.assertAlmostEqual(
            codex_driver.turn_cost("gpt-6-sol", second.usage), sum(t.cost_usd for t in turns if t.cost_usd is not None)
        )

    def test_cumulative_usage_that_falls_is_refused(self) -> None:
        first = codex_driver.read_events(completed_turn(500, 100, 50, 10)).usage
        second = codex_driver.read_events(completed_turn(400, 100, 60, 10)).usage
        assert first is not None and second is not None
        with self.assertRaises(codex_driver.CodexStreamError):
            codex_driver.usage_since(first, second)

    def test_a_turn_without_reported_usage_has_unknown_cost(self) -> None:
        reading = codex_driver.read_events(event({"type": "thread.started", "thread_id": "t-1"}))
        previous = codex_driver.read_events(completed_turn(500, 100, 50, 10)).usage
        evidence = codex_driver.turn_evidence(2, "question", None, "t-1", reading, previous, "gpt-6-sol", "t0")
        self.assertIsNone(evidence.cost_usd)


class AccountingTest(unittest.TestCase):
    def test_primary_and_reviewer_thread_totals_are_separate_and_replayed_responses_are_not_added(self) -> None:
        def usage(thread: str, turn: str, response: str, tokens: int) -> dict[str, Json]:
            return {
                "type": "token_usage_record",
                "payload": {
                    "thread_id": thread,
                    "turn_id": turn,
                    "response_id": response,
                    "thread_token_usage": {
                        "input_tokens": tokens,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 1,
                        "reasoning_output_tokens": 0,
                    },
                },
            }

        records: list[dict[str, Json]] = [
            {"type": "turn_context", "payload": {"turn_id": "primary", "model": "gpt-6-sol"}},
            {"type": "turn_context", "payload": {"turn_id": "review", "model": "codex-auto-review"}},
            usage("main", "primary", "p1", 10),
            usage("reviewer", "review", "r1", 100),
            usage("main", "primary", "p2", 20),
            usage("reviewer", "review", "r2", 150),
            usage("reviewer", "review", "r1", 100),
        ]
        result = codex_driver.rollout_accounting("\n".join(event(r) for r in records), "gpt-6-sol", True)
        self.assertAlmostEqual(
            codex_driver.turn_cost(
                "gpt-6-sol",
                codex_driver.Usage(
                    input_tokens=20,
                    cached_input_tokens=0,
                    cache_write_input_tokens=0,
                    output_tokens=1,
                    reasoning_output_tokens=0,
                ),
            ),
            result.main_known_cost_usd,
        )
        self.assertTrue(result.main_usage_complete)
        self.assertEqual(1, len(result.reviewer_usage))
        self.assertEqual(150, result.reviewer_usage[0].input_tokens)
        self.assertIsNone(result.reviewer_price_usd)
        self.assertFalse(codex_driver.rollout_accounting("", "gpt-6-sol", True).main_usage_complete)


class RecoveryRunTest(unittest.TestCase):
    def test_recovery_can_complete_while_independent_reviewer_rejection_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scenario = Scenario(
                id="scenario",
                title="action",
                world=WorldKind.MINIMAL,
                world_extra=None,
                source="accepted native route",
                ground_truth="authorized action",
                turns=[Turn(human="commit", before=None)],
            )
            plan = runner.RunPlan(
                Layout(root),
                root / "worlds",
                ExportRecord(
                    schema="pinboard-behavioral-export/v1",
                    commit="0" * 40,
                    skills_sha256="0" * 64,
                    plugin_root="/plugin",
                ),
                "candidate",
                RegisteredSet("native", (scenario,), ()),
                1,
                1,
                "gpt-6-sol",
                processes.Window(None),
            )
            built = world.World(root, root / "project", root / "origin", None, root / "launcher", plan.window)
            for decision, expected in [("allow", Completed), ("deny", Stopped)]:
                with self.subTest(decision=decision):
                    state = runner.start(plan, scenario, RunKey(scenario_id="scenario", variant=decision, index=1))
                    reading = codex_driver.read_events(completed_turn(100, 10, 10, 0))
                    with (
                        patch.object(
                            codex_driver, "run_turn", return_value=(processes.Completed(0, "", "", False), reading)
                        ),
                        patch.object(codex_driver, "rollout_text", return_value=native_rollout(decision)),
                        patch.object(runner.RunState, "run_hook"),
                        patch.object(runner.RunState, "snapshot"),
                    ):
                        outcome = runner.codex_thread(state, built, root, runner.CodexPlan(plan, "high", root / "auth"))
                    self.assertIsInstance(outcome, expected)
                    self.assertTrue(state.turns[0].permission_denials)


class PriceTest(unittest.TestCase):
    def test_token_classes_are_priced_separately(self) -> None:
        usage = codex_driver.Usage(
            input_tokens=1_000_000,
            cached_input_tokens=600_000,
            cache_write_input_tokens=100_000,
            output_tokens=100_000,
            reasoning_output_tokens=40_000,
        )
        rates = codex_driver.price("gpt-6-sol")
        expected = (
            300_000 * rates.uncached_input
            + 100_000 * rates.cache_write
            + 600_000 * rates.cached_input
            + 100_000 * rates.output
        ) / 1_000_000
        self.assertAlmostEqual(expected, codex_driver.turn_cost("gpt-6-sol", usage))

    def test_a_model_without_a_recorded_price_is_refused(self) -> None:
        with self.assertRaises(codex_driver.CodexUnavailableError):
            codex_driver.price("unpriced-model")


if __name__ == "__main__":
    unittest.main()
