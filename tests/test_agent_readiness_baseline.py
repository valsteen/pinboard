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
        self.assertEqual(
            (
                "dc5bc7a815459ba7a38f408ceec4cb8933a44395",
                "c92ee09348448beec3407078b81813fe28315988",
                "747856dcc135b5e46cbc7236bbe03b8ed6696bdb",
            ),
            tuple(case.base_identity for case in baseline.cases),
        )
        self.assertEqual((44, 40, 8), tuple(case.meaningful_edit_site_count for case in baseline.cases))
        self.assertEqual((1, 2, 0), tuple(case.correction_rounds for case in baseline.cases))
        self.assertEqual(
            "400be989ec480fd83a11ec87a1faa12ced62c8a3b1a78db452b728af12d233e2",
            baseline.methodology.evidence_plan_digest,
        )
        self.assertEqual(68_248, sum(source.selected_bytes for source in baseline.evidence_sources))
        self.assertEqual(
            ("persistence-import-correction", "persistence-evidence-correction"),
            baseline.cases[1].context_evidence_ids,
        )
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

        incomplete = source.replace(b',"correction_rounds":1', b"", 1)
        with self.assertRaises(msgspec.ValidationError):
            decode_baseline(incomplete)

        invalid = source.replace(b'"meaningful_edit_site_count":44', b'"meaningful_edit_site_count":0', 1)
        with self.assertRaises(msgspec.ValidationError):
            decode_baseline(invalid)

    def test_invalid_candidate_provenance_is_rejected(self) -> None:
        source = CORPUS_PATH.read_bytes()
        invalid_candidates = {
            "base": source.replace(
                b'"base_identity":"dc5bc7a815459ba7a38f408ceec4cb8933a44395"',
                b'"base_identity":"zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"',
                1,
            ),
            "commit": source.replace(
                b'"candidate_identity":"4ad54ddc4d97bcf01ea10e229893de7cabb1e80a"',
                b'"candidate_identity":"zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"',
                1,
            ),
            "working-tree": source.replace(
                b"working-tree-sha256:d7105e63b870990f7994a716b1c9428ad16fe572557013a526ebb389f6c5d669",
                b"working-tree-sha256:zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
                1,
            ),
            "kind-mismatch": source.replace(b'"candidate_kind":"commit"', b'"candidate_kind":"working-tree-sha256"', 1),
        }
        for label, candidate in invalid_candidates.items():
            with self.subTest(label=label), self.assertRaises(msgspec.ValidationError):
                decode_baseline(candidate)

    def test_evidence_cannot_be_assigned_to_multiple_cases(self) -> None:
        source = CORPUS_PATH.read_bytes()
        duplicate_evidence = source.replace(
            b'"context_evidence_ids":["persistence-import-correction","persistence-evidence-correction"]',
            b'"context_evidence_ids":["persistence-import-correction","persistence-evidence-correction","large-refactor-result"]',
            1,
        ).replace(b'"selected_source_bytes":15591', b'"selected_source_bytes":46329', 1)
        with self.assertRaises(msgspec.ValidationError):
            decode_baseline(duplicate_evidence)

    def test_projection_is_deterministic_and_matches_the_committed_report(self) -> None:
        source = CORPUS_PATH.read_bytes()
        baseline = decode_baseline(source)

        first = render_baseline(baseline)
        second = render_baseline(decode_baseline(source))

        self.assertEqual(first, second)
        self.assertEqual(REPORT_PATH.read_text(encoding="utf-8"), first)


if __name__ == "__main__":
    unittest.main()
