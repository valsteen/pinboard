"""The startup pilot reuses verified private cases, native observations and bounded session admission."""

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import msgspec

from evals.behavioral import codex_driver, oneshot, processes, runner, scenarios, scoring, spend, startup_pilot
from evals.behavioral.layout import Layout
from evals.behavioral.records import ChecklistItem, ExportRecord, Runtime, encode
from tests.test_behavioral_decision import fixture_set, record_score
from tests.test_behavioral_scoring import answer


class StartupPilotTest(unittest.TestCase):
    def test_both_arms_use_sonnet_and_do_not_purchase_existing_pilot_scores_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out = root / "out"
            selected = fixture_set(["s1"], 1, (ChecklistItem.P9,))
            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))

            def run(plan: runner.CodexPlan, budget: spend.Budget) -> list[str]:
                self.assertEqual(30, budget.cap_usd)
                key = plan.run.planned()[0][1]
                layout = Layout(out)
                record_score(layout, key.variant, key, [set()], True)
                record = layout.run_record(key)
                assert record is not None
                (layout.run_directory(key) / "run.json").write_bytes(
                    encode(msgspec.structs.replace(record, runtime=Runtime.CODEX))
                )
                (layout.run_directory(key) / "rollout.jsonl").write_text("")
                return []

            def respond(text: str, model: str, _window: processes.Window) -> oneshot.Answer:
                self.assertEqual("claude-sonnet-5-5", model)
                label = re.search(r"Label: (\w+)", text)
                assert label is not None
                name = label[1]
                if "blind evaluator" in text:
                    body = answer(name, [1])
                else:
                    body = (
                        "```json\n"
                        + json.dumps(
                            {
                                "label": name,
                                "turns": [{"turn": 1, "verdict": "final-reply-carries-substance", "evidence": "reply"}],
                            }
                        )
                        + "\n```"
                    )
                return oneshot.Answer(0.01, body, "{}", None)

            with (
                patch.object(startup_pilot, "load_set", return_value=selected),
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.export, "export_revision", return_value=exported),
                patch.object(startup_pilot.probe, "probe_codex", return_value=Mock(passed=True)),
                patch.object(startup_pilot.runner, "run_codex", side_effect=run),
                patch.object(
                    startup_pilot,
                    "observe",
                    return_value=startup_pilot.WorkflowObservation(
                        "pinboard-native-workflow-observation/v1",
                        [startup_pilot.TurnConfiguration("owner", "gpt-5.6-terra", "high")],
                        [],
                        "observed-calls-only",
                    ),
                ),
                patch.object(oneshot, "ask", side_effect=respond) as paid,
            ):
                self.assertEqual(0, startup_pilot.run(root, "a", "b", root / "set.json", out, root / "worlds"))
            self.assertEqual(["claude-sonnet-5-5"] * 4, [call.args[1] for call in paid.call_args_list])
            self.assertEqual(2, len(scoring.valid_score_counts(Layout(out), "claude-sonnet-5-5")))
            self.assertEqual({"claude-sonnet-5-5"}, {record.assessor_model for record in Layout(out).assessments()})

    def private_set(self, root: Path) -> Path:
        original = scenarios.DATA / "scenario-sets" / "review-after-merge.json"
        registration = msgspec.json.decode(original.read_bytes(), type=scenarios.ScenarioSet)
        (root / "scenario-sets").mkdir()
        (root / "scenarios").mkdir()
        target = root / "scenario-sets" / "private.json"
        target.write_bytes(original.read_bytes())
        for member in registration.scenarios:
            (root / "scenarios" / f"{member.id}.json").write_bytes(
                (scenarios.DATA / "scenarios" / f"{member.id}.json").read_bytes()
            )
        return target

    def test_private_registered_sources_are_verified_at_the_selected_set_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self.private_set(root)
            registered = scenarios.load_set(selected)
            scenario = registered.scenarios[0]
            self.assertEqual(
                hashlib.sha256(registered.content(scenario)).hexdigest(), registered.registration.scenarios[0].sha256
            )
            (root / "scenarios" / f"{scenario.id}.json").write_text("changed")
            with self.assertRaises(scenarios.DataIntegrityError):
                scenarios.load_set(selected)

    def test_failed_probe_preserves_deadline_cap_and_prevents_trial_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = self.private_set(root)
            out = root / "out"
            exported = ExportRecord("pinboard-behavioral-export/v1", "0" * 40, "0" * 64, str(root))
            with (
                patch.object(startup_pilot.runner, "require_codex_world_location"),
                patch.object(startup_pilot.time, "monotonic", return_value=100),
                patch.object(startup_pilot.export, "export_revision", return_value=exported) as exporting,
                patch.object(startup_pilot.probe, "probe_codex", return_value=Mock(passed=False)) as probing,
                patch.object(startup_pilot.runner, "run_codex") as running,
            ):
                self.assertEqual(2, startup_pilot.run(root, "a", "b", selected, out, root / "worlds"))
            running.assert_not_called()
            self.assertEqual(1, exporting.call_count)
            budget = probing.call_args.args[1]
            self.assertEqual(7300, budget.window.deadline)
            self.assertEqual(30, budget.cap_usd)
            self.assertFalse(budget.allow_unknown_reviewer_price)
            result = msgspec.json.decode((out / "pilot.json").read_bytes(), type=startup_pilot.PilotResult)
            self.assertEqual("incomplete", result.status)
            self.assertEqual("inconclusive", result.formal_verdict)
            self.assertEqual(0, len(list(Layout(out).run_records())))

    def test_observations_record_calls_without_inferring_delivery_and_missing_child_usage_stays_unknown(self) -> None:
        records = [
            {
                "type": "turn_context",
                "payload": {"turn_id": "owner", "model": "gpt-5.6-terra", "effort": "high", "additive": True},
            },
            {"type": "turn_context", "payload": {"turn_id": "child", "model": "gpt-5.6-terra", "effort": "high"}},
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "spawn_agent", "arguments": "opaque"},
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "thread_id": "o",
                    "turn_id": "owner",
                    "response_id": "r",
                    "thread_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 0,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 5,
                    },
                },
            },
        ]
        rollout = "\n".join(json.dumps(r) for r in records)
        observed = startup_pilot.observe(rollout)
        self.assertEqual(["spawn_agent"], observed.native_call_names)
        self.assertEqual({"owner", "child"}, {c.turn_id for c in observed.configurations})
        self.assertEqual("observed-calls-only", observed.evidence)
        self.assertFalse(codex_driver.rollout_accounting(rollout, "gpt-5.6-terra", True).main_usage_complete)
        payload = records[-1]["payload"]
        assert isinstance(payload, dict)
        usage = dict(payload)
        usage.update(thread_id="c", turn_id="child", response_id="child-r")
        complete = rollout + "\n" + json.dumps({"type": "token_usage_record", "payload": usage})
        accounting = codex_driver.rollout_accounting(complete, "gpt-5.6-terra", True)
        self.assertTrue(accounting.main_usage_complete)
        self.assertAlmostEqual(0.00064, accounting.main_known_cost_usd)
