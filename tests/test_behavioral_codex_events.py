"""Codex output reading: replies, commentary, refusals and token pricing from synthetic event text."""

import json
import unittest

from evals.behavioral import codex_driver

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


class RolloutRefusalTest(unittest.TestCase):
    def test_refusals_that_never_reached_the_event_stream_are_found_in_the_rollout(self) -> None:
        rollout = (
            '{"type":"response_item","payload":{"output":"error: cannot lock ref \'ORIG_HEAD\': Unable to create '
            "'/w/tally/.git/ORIG_HEAD.lock': Operation not permitted\"}}\n"
            '{"type":"response_item","payload":{"output":"exec requires approval, but approval policy is never"}}\n'
        )
        refusals = codex_driver.rollout_refusals(rollout)
        self.assertEqual(1, len(refusals.git_writes))
        self.assertEqual(1, len(refusals.approvals))

    def test_guidance_that_mentions_git_and_approval_is_not_a_refusal(self) -> None:
        text = (
            "Sibling .codex, .git, and the installed plugin cache remain read-only. "
            "Approval policy is currently never. Do not provide the sandbox_permissions for any reason."
        )
        refusals = codex_driver.rollout_refusals(text)
        self.assertEqual([], refusals.git_writes)
        self.assertEqual([], refusals.approvals)


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
        self.assertAlmostEqual(codex_driver.turn_cost("gpt-6-sol", second.usage), sum(t.cost_usd for t in turns))

    def test_cumulative_usage_that_falls_is_refused(self) -> None:
        first = codex_driver.read_events(completed_turn(500, 100, 50, 10)).usage
        second = codex_driver.read_events(completed_turn(400, 100, 60, 10)).usage
        assert first is not None and second is not None
        with self.assertRaises(codex_driver.CodexStreamError):
            codex_driver.usage_since(first, second)

    def test_a_turn_without_reported_usage_costs_nothing(self) -> None:
        reading = codex_driver.read_events(event({"type": "thread.started", "thread_id": "t-1"}))
        previous = codex_driver.read_events(completed_turn(500, 100, 50, 10)).usage
        evidence = codex_driver.turn_evidence(2, "question", None, "t-1", reading, previous, "gpt-6-sol", "t0")
        self.assertEqual(0.0, evidence.cost_usd)


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
