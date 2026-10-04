"""Observable fixture, registration, recovery and assessment contracts for fictional inquiries."""

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import msgspec

from evals.behavioral import investigation, processes


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

    def test_source_histories_copy_without_detached_git_maintenance(self) -> None:
        case = investigation.load_set(investigation.DATA / "sets" / "heldout.json")[1][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace = root / "git.trace"
            with patch.dict(
                os.environ,
                {
                    "GIT_TRACE": str(trace),
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "maintenance.auto",
                    "GIT_CONFIG_VALUE_0": "true",
                },
            ):
                investigation.build_world(root / "seed", case, processes.Window(None))
                shutil.copytree(root / "seed", root / "copy")
            self.assertNotIn("maintenance run --auto", trace.read_text())
            for repo in case.repositories:
                histories = [
                    processes.git_checked(
                        ["log", "--format=%H %s"],
                        cwd=root / name / "sources" / repo.name,
                        window=processes.Window(None),
                    )
                    for name in ("seed", "copy")
                ]
                self.assertEqual(histories[0], histories[1])

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
