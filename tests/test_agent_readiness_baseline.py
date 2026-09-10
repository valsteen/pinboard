import unittest
from pathlib import Path

import msgspec

from tests.prototypes.agent_readiness_baseline import decode_baseline, render_baseline

PROTOTYPE_ROOT = Path(__file__).parent / "prototypes"
CORPUS_PATH = PROTOTYPE_ROOT / "agent_readiness_baseline.v1.json"
REPORT_PATH = PROTOTYPE_ROOT / "agent_readiness_baseline.v1.md"


class AgentReadinessBaselineTest(unittest.TestCase):
    def test_corpus_is_canonical_and_measurement_complete(self) -> None:
        source = CORPUS_PATH.read_bytes()

        baseline = decode_baseline(source)

        self.assertEqual(
            (
                "large-cross-boundary-delivery",
                "persistence-fixed-point-cleanup",
                "local-dto-simplification",
            ),
            tuple(case.case_id for case in baseline.cases),
        )
        self.assertEqual(63_335, sum(source.selected_bytes for source in baseline.evidence_sources))
        for case in baseline.cases:
            self.assertTrue(case.owner_localization.correct_owners)
            self.assertGreater(case.selected_source_bytes, 0)
            self.assertGreater(case.meaningful_edit_site_count, 0)
            self.assertTrue(case.verification)
            self.assertGreaterEqual(case.correction_rounds, 0)

    def test_unknown_incomplete_and_invalid_measurements_are_rejected(self) -> None:
        source = CORPUS_PATH.read_bytes()
        unknown = source.replace(b'{"bottlenecks":', b'{"aggregate_readiness_score":100,"bottlenecks":', 1)
        with self.assertRaises(msgspec.ValidationError):
            decode_baseline(unknown)

        incomplete = source.replace(b',"correction_rounds":4', b"", 1)
        with self.assertRaises(msgspec.ValidationError):
            decode_baseline(incomplete)

        invalid = source.replace(b'"meaningful_edit_site_count":44', b'"meaningful_edit_site_count":0', 1)
        with self.assertRaises(msgspec.ValidationError):
            decode_baseline(invalid)

    def test_projection_is_deterministic_and_matches_the_committed_report(self) -> None:
        source = CORPUS_PATH.read_bytes()
        baseline = decode_baseline(source)

        first = render_baseline(baseline)
        second = render_baseline(decode_baseline(source))

        self.assertEqual(first, second)
        self.assertEqual(REPORT_PATH.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
