"""Observable fixture, registration, recovery and assessment contracts for fictional inquiries."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import msgspec

from evals.behavioral import claude_driver, codex_driver, investigation, processes


class InvestigationWorldTests(unittest.TestCase):
    def test_registered_worlds_have_independent_sources_and_private_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("tuning", "heldout"):
                _, cases = investigation.load_set(investigation.DATA / "sets" / f"{name}.json")
                self.assertEqual({case.kind for case in cases}, {"urgent", "cross-service"})
                for case in cases:
                    root = Path(temporary) / case.id
                    home = investigation.build_world(root, case, processes.Window(None))
                    self.assertEqual(home, root / "inquiry")
                    self.assertFalse(any(path.name.startswith("key") for path in root.rglob("*.json")))
                    for repo in case.repositories:
                        location = root / "sources" / repo.name
                        history = processes.git_checked(
                            ["log", "--format=%s"], cwd=location, window=processes.Window(None)
                        )
                        self.assertGreaterEqual(len(history.splitlines()), 2)
                    self.assertTrue((root / "sources" / "dashboards").is_dir())
                    self.assertTrue((root / "sources" / "transcripts").is_dir())
                    self.assertTrue((root / "sources" / "traces").is_dir())

    def test_changed_registered_bytes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "investigation"
            shutil.copytree(investigation.DATA, copied)
            source = copied / "scenarios" / "urgent-heldout.json"
            source.write_bytes(source.read_bytes() + b" ")
            with patch.object(investigation, "DATA", copied), self.assertRaisesRegex(ValueError, "digest changed"):
                investigation.load_set(copied / "sets" / "heldout.json")

    def test_private_key_digest_is_required(self) -> None:
        case = investigation.load_set(investigation.DATA / "sets" / "heldout.json")[1][0]
        key = investigation.InvestigationKey(
            schema="pinboard-investigation-key/v1",
            scenario_id=case.id,
            facts=[],
            consequential_fact="impact",
            red_herring="lead",
            human_choice="choose",
            unavailable_fact="gap",
            valid_alternatives=[],
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "key.json"
            content = msgspec.json.encode(key)
            path.write_bytes(content)
            self.assertEqual(investigation.load_key(path, case, hashlib.sha256(content).hexdigest()), key)
            with self.assertRaisesRegex(ValueError, "digest changed"):
                investigation.load_key(path, case, "0" * 64)

    def test_fresh_recovery_requires_new_identity_and_saved_evidence(self) -> None:
        case = investigation.load_set(investigation.DATA / "sets" / "heldout.json")[1][0]
        requested: list[str | None] = []
        with tempfile.TemporaryDirectory() as temporary:
            home = investigation.build_world(Path(temporary) / "world", case, processes.Window(None))
            finding = home / "finding.md"

            def controlled_send(_human: str, previous: str | None) -> tuple[str, bool, str | None]:
                requested.append(previous)
                if len(requested) == 2:
                    finding.write_text("Worker w9 changed tags only; Nia dismissed the flush lead.\n")
                if len(requested) == 3:
                    self.assertIsNone(previous)
                    saved = finding.read_text()
                    self.assertIn("Nia dismissed the flush lead", saved)
                    return "thread-b", bool(saved), None
                return "thread-a", False, None

            exercised, coverage = investigation.exercise_sessions(case, controlled_send)
        self.assertEqual(requested, [None, "thread-a", None])
        self.assertEqual([turn.runtime_identity for turn in exercised], ["thread-a", "thread-a", "thread-b"])
        self.assertEqual(coverage, "unobserved")
        observations = [
            investigation.SessionObservation(1, investigation.SessionMode.START, "thread-a", False, None),
            investigation.SessionObservation(2, investigation.SessionMode.CONTINUE, "thread-a", False, None),
            investigation.SessionObservation(3, investigation.SessionMode.FRESH, "thread-b", True, None),
        ]
        self.assertEqual(investigation.record_sessions(case, observations), "unobserved")
        with self.assertRaisesRegex(ValueError, "reused a runtime identity"):
            investigation.record_sessions(
                case,
                [
                    *observations[:2],
                    investigation.SessionObservation(3, investigation.SessionMode.FRESH, "thread-a", True, None),
                ],
            )
        with self.assertRaisesRegex(ValueError, "did not read saved"):
            investigation.record_sessions(
                case,
                [
                    *observations[:2],
                    investigation.SessionObservation(3, investigation.SessionMode.FRESH, "thread-b", False, None),
                ],
            )

    def test_controlled_runtime_commands_start_fresh_sessions(self) -> None:
        case = investigation.load_set(investigation.DATA / "sets" / "heldout.json")[1][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claude_calls: list[list[str]] = []
            session: claude_driver.ClaudeSession | None = None

            def fake_claude(_tool: processes.Tool, arguments: list[str], **_kwargs: object) -> processes.Completed:
                claude_calls.append(arguments)
                identity = (
                    arguments[arguments.index("--session-id") + 1]
                    if "--session-id" in arguments
                    else arguments[arguments.index("--resume") + 1]
                )
                denials: list[dict[str, str]] = []
                event = {
                    "type": "result",
                    "subtype": "success",
                    "session_id": identity,
                    "is_error": False,
                    "total_cost_usd": 0.0,
                    "usage": {
                        "input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 0,
                    },
                    "permission_denials": denials,
                    "result": "saved",
                }
                return processes.Completed(0, json.dumps(event) + "\n", "", False)

            def send_claude(human: str, previous: str | None) -> tuple[str, bool, str | None]:
                nonlocal session
                if previous is None:
                    session = claude_driver.ClaudeSession.start(root, "test-model", root)
                assert session is not None
                session.turn(1 if previous is None else 2, human, None, root / "claude.jsonl")
                return session.session_id, previous is None and len(claude_calls) > 1, None

            with patch.object(claude_driver.processes, "run_tool", side_effect=fake_claude):
                observations, coverage = investigation.exercise_sessions(case, send_claude)
            self.assertEqual(coverage, "unobserved")
            self.assertEqual(["--session-id" in args for args in claude_calls], [True, False, True])
            self.assertNotEqual(observations[0].runtime_identity, observations[2].runtime_identity)

            codex_calls: list[list[str]] = []

            def fake_codex(arguments: list[str], **_kwargs: object) -> processes.Completed:
                codex_calls.append(arguments)
                identity = f"codex-{len([a for a in codex_calls if a[1] != 'resume'])}"
                return processes.Completed(
                    0, json.dumps({"type": "thread.started", "thread_id": identity}) + "\n", "", False
                )

            def send_codex(human: str, previous: str | None) -> tuple[str, bool, str | None]:
                _, reading = codex_driver.run_turn(
                    root, root, previous, human, root / "codex.jsonl", processes.Window(None)
                )
                assert reading.thread_id is not None
                return reading.thread_id, previous is None and len(codex_calls) > 1, None

            with patch.object(codex_driver, "codex", side_effect=fake_codex):
                observations, coverage = investigation.exercise_sessions(case, send_codex)
            self.assertEqual(coverage, "unobserved")
            self.assertEqual([args[1] == "resume" for args in codex_calls], [False, True, False])
            self.assertNotEqual(observations[0].runtime_identity, observations[2].runtime_identity)

    def test_assessment_keeps_decision_measures_separate(self) -> None:
        case = investigation.load_set(investigation.DATA / "sets" / "heldout.json")[1][0]
        for expected, failing in (
            (True, set()),
            (False, {investigation.Measure.SALIENCE, investigation.Measure.CHECKABILITY}),
        ):
            assessment = investigation.InvestigationAssessment(
                schema="pinboard-investigation-assessment/v1",
                scenario_id=case.id,
                results=[
                    investigation.MeasureResult(measure, measure not in failing, "synthetic reviewer observation")
                    for measure in investigation.Measure
                ],
            )
            decoded = investigation.decode_assessment(msgspec.json.encode(assessment), case)
            self.assertEqual(all(result.passed for result in decoded.results), expected)
            self.assertEqual(len(decoded.results), len(investigation.Measure))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            investigation.InvestigationAssessment(
                schema="pinboard-investigation-assessment/v1", scenario_id=case.id, results=[]
            )


if __name__ == "__main__":
    unittest.main()
