"""The local trial collector preserves attribution without exporting private note pointers."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

TRIAL_PATH = Path(__file__).resolve().parent.parent / "skills/investigation-focus/scripts/trial.py"
spec = importlib.util.spec_from_file_location("investigation_trial", TRIAL_PATH)
assert spec is not None and spec.loader is not None
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class TrialCollectorTests(unittest.TestCase):
    def test_interleaved_inquiries_and_candidate_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)

            def add(
                inquiry: str,
                session_id: str,
                context: str,
                candidate: str,
                revision: str,
                output: str,
                intervention: str,
            ) -> None:
                failures: list[dict[str, str]] = (
                    [{"kind": "missing source", "summary": "source unavailable"}] if context == "resumed" else []
                )
                entry = {
                    "schema": "investigation-trial-session/v1",
                    "local_note": f"private/{inquiry}/{session_id}.md",
                    "session": {
                        "inquiry_id": inquiry,
                        "session_id": session_id,
                        "context": context,
                        "candidate_commit": candidate,
                        "model": "gpt-6-luna",
                        "reasoning": "high",
                        "sources": [{"source_id": "dashboard", "revision": revision, "window": revision}],
                        "outputs": [
                            {
                                "output_id": output,
                                "revision": revision,
                                "sources": [{"source_id": "dashboard", "revision": revision, "window": revision}],
                            }
                        ],
                        "interventions": [{"kind": intervention, "summary": f"{intervention} recorded"}],
                        "failures": failures,
                        "coverage": [{"source_id": "queue", "status": "withheld", "summary": "not supplied"}],
                        "cost_usd": 0.25 if context == "resumed" else None,
                        "cost_basis": "reported" if context == "resumed" else "unavailable",
                    },
                }
                source = home / "session.json"
                source.write_text(json.dumps(entry), encoding="utf-8")
                trial.record(home, source)

            add("harbor", "s1", "fresh", "a" * 40, "r1", "technical", "direction")
            add("billing", "s1", "fresh", "a" * 40, "r1", "policy", "direction")
            add("harbor", "s2", "resumed", "b" * 40, "r2", "leadership", "correction")
            add("billing", "s2", "resumed", "b" * 40, "r2", "policy", "dismissal")

            destination = trial.export(home)
            draft = json.loads(destination.read_text(encoding="utf-8"))
            rows = draft["sessions"]
            self.assertEqual([row["sequence"] for row in rows], [1, 2, 3, 4])
            self.assertEqual(
                [(row["session"]["inquiry_id"], row["session"]["session_id"]) for row in rows],
                [("harbor", "s1"), ("billing", "s1"), ("harbor", "s2"), ("billing", "s2")],
            )
            self.assertEqual(rows[2]["session"]["candidate_commit"], "b" * 40)
            self.assertEqual(rows[2]["session"]["sources"][0]["revision"], "r2")
            self.assertEqual(rows[2]["session"]["outputs"][0]["output_id"], "leadership")
            self.assertEqual(rows[2]["session"]["outputs"][0]["sources"][0]["revision"], "r2")
            self.assertEqual(rows[2]["session"]["interventions"][0]["kind"], "correction")
            self.assertEqual(rows[2]["session"]["failures"][0]["kind"], "missing source")
            self.assertEqual(rows[2]["session"]["coverage"][0]["status"], "withheld")
            self.assertEqual(rows[2]["session"]["cost_usd"], 0.25)
            self.assertNotIn("local_note", destination.read_text(encoding="utf-8"))
            self.assertNotIn("private/", destination.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(ValueError, "already exists"):
                add("billing", "s2", "resumed", "b" * 40, "r3", "policy", "correction")


if __name__ == "__main__":
    unittest.main()
