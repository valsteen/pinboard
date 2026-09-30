"""Strict decoding of blind scorer answers: only a complete checklist answer for the right label counts."""

import json
import unittest

from evals.behavioral import scoring
from evals.behavioral.records import Redaction, ScoreRecord, ScoringFailure


def item(verdict: str) -> dict[str, str]:
    return {"verdict": verdict, "evidence": "quote"}


def answer(label: str, turns: list[int]) -> str:
    items = {name: item("pass") for name in [f"P{n}" for n in range(1, 13)] + ["N1"]}
    items["P3"] = item("fail")
    replies = json.dumps([{"turn": turn, "items": items} for turn in turns])
    body = (
        f'{{"label": {json.dumps(label)}, "replies": {replies}, "scenario_pass": false, '
        '"failed_items": ["turn 1 P3: missing"], "permission_denial_suspected": false}'
    )
    return f'Reasoning first.\n\n```json\n{{"label": "draft"}}\n```\n\nFinal:\n```json\n{body}\n```\n'


class DecodeScoreTest(unittest.TestCase):
    def test_a_complete_answer_for_the_label_decodes_from_the_last_json_block(self) -> None:
        decoded = scoring.decode_score(answer("T1", [1, 2]), "T1", 2)
        assert isinstance(decoded, ScoreRecord)
        self.assertEqual([1, 2], [reply.turn for reply in decoded.replies])

    def test_an_unknown_field_is_a_scoring_failure(self) -> None:
        text = answer("T1", [1]).replace('"scenario_pass"', '"confidence": "high", "scenario_pass"')
        decoded = scoring.decode_score(text, "T1", 1)
        self.assertIsInstance(decoded, ScoringFailure)

    def test_a_missing_checklist_item_is_a_scoring_failure(self) -> None:
        text = answer("T1", [1]).replace('"N1": {"verdict": "pass", "evidence": "quote"}', '"N2": {}')
        self.assertIsInstance(scoring.decode_score(text, "T1", 1), ScoringFailure)

    def test_an_unknown_verdict_is_a_scoring_failure(self) -> None:
        text = answer("T1", [1]).replace('"verdict": "fail"', '"verdict": "maybe"')
        self.assertIsInstance(scoring.decode_score(text, "T1", 1), ScoringFailure)

    def test_another_label_is_a_scoring_failure(self) -> None:
        self.assertIsInstance(scoring.decode_score(answer("T2", [1]), "T1", 1), ScoringFailure)

    def test_a_skipped_or_repeated_reply_is_a_scoring_failure(self) -> None:
        self.assertIsInstance(scoring.decode_score(answer("T1", [1, 1]), "T1", 2), ScoringFailure)
        self.assertIsInstance(scoring.decode_score(answer("T1", [1]), "T1", 2), ScoringFailure)

    def test_no_answer_or_no_json_block_is_a_scoring_failure(self) -> None:
        self.assertIsInstance(scoring.decode_score(None, "T1", 1), ScoringFailure)
        self.assertIsInstance(scoring.decode_score("no block here", "T1", 1), ScoringFailure)


class RedactTest(unittest.TestCase):
    def test_known_roots_become_placeholders_and_remaining_local_paths_are_removed(self) -> None:
        text = "at /work/world-1/tally and /work/world-1b, plugin /opt/export/plugin, also /Users/someone/x y"
        redacted = scoring.redact(
            text,
            [
                Redaction(value="/work/world-1", placeholder="<world>"),
                Redaction(value="/work/world-1b", placeholder="<other>"),
                Redaction(value="/opt/export/plugin", placeholder="<plugin>"),
            ],
        )
        self.assertEqual("at <world>/tally and <other>, plugin <plugin>, also <path> y", redacted)


if __name__ == "__main__":
    unittest.main()
