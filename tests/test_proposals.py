import json
import unittest
from datetime import UTC, datetime

from pinboard.application.proposal_models import Proposal, ProposalFailure
from pinboard.application.proposals import parse_proposal
from tests.support import JsonObject


def proposal() -> JsonObject:
    return {
        "schema": "pinboard-proposal/v2",
        "proposal_id": "proposal-1",
        "created_at": "2026-08-25T00:00:00Z",
        "source_task_id": "task",
        "user_label": "Proposal",
        "trigger": "A current boundary exposed a missing behavior.",
        "evidence": ["source:test"],
        "why_it_matters": "The behavior must persist through SQLite.",
        "relation": {"kind": "independent", "item": None},
        "effect": "The proposal appears as an intake item.",
        "unlock": "Current proposal intake remains usable.",
        "urgency_evidence": "The installed command exercises this boundary.",
        "freshness_assumptions": ["SQLite remains authoritative."],
        "checkout_policy": "coordinator-selected",
        "obligations": [
            {
                "obligation_id": "sqlite-persistence",
                "statement": "The proposal persists through SQLite.",
                "deferral_policy": "forbidden",
            }
        ],
    }


class ProposalInputTest(unittest.TestCase):
    def test_current_proposal_decodes_exact_model(self) -> None:
        value = proposal()
        decoded = parse_proposal(json.dumps(value))
        positioned = parse_proposal(json.dumps({**value, "position": 2}))
        date_only = parse_proposal(json.dumps({**value, "created_at": "2026-08-25"}))
        timezone_aware = parse_proposal(json.dumps({**value, "created_at": "2026-08-25T12:00:00+02:00"}))
        unexpected = parse_proposal(json.dumps({**value, "unexpected": True}))
        self.assertIsInstance(decoded, Proposal)
        self.assertIsInstance(positioned, Proposal)
        self.assertIsInstance(date_only, Proposal)
        self.assertIsInstance(timezone_aware, Proposal)
        self.assertIsInstance(unexpected, ProposalFailure)
        assert isinstance(decoded, Proposal)
        assert isinstance(positioned, Proposal)
        assert isinstance(date_only, Proposal)
        assert isinstance(timezone_aware, Proposal)
        self.assertEqual("proposal-1", decoded.proposal_id)
        self.assertEqual(2, positioned.position)
        self.assertEqual(datetime(2026, 8, 25, tzinfo=UTC), date_only.created_at_utc())
        self.assertEqual(datetime(2026, 8, 25, 10, tzinfo=UTC), timezone_aware.created_at_utc())

    def test_decoder_rejects_invalid_shapes_and_reports_paths(self) -> None:
        valid = proposal()
        cases = (
            ({**valid, "schema": "repo" + "-work/v1"}, "schema"),
            ({**valid, "schema": "pinboard" + "-proposal/v1"}, "schema"),
            ({**valid, "proposal_id": "Not Valid"}, "proposal_id"),
            ({**valid, "proposal_id": "proposal-1\n"}, "proposal_id"),
            ({**valid, "trigger": ""}, "trigger"),
            ({**valid, "trigger": " leading whitespace"}, "trigger"),
            ({**valid, "trigger": "trailing newline\n"}, "trigger"),
            ({**valid, "position": 0}, "position"),
            ({**valid, "evidence": [""]}, "evidence[0]"),
            ({**valid, "evidence": [" leading whitespace"]}, "evidence[0]"),
            ({**valid, "evidence": ["source:test", "source:test"]}, "evidence"),
            (
                {**valid, "freshness_assumptions": ["SQLite remains authoritative.", "SQLite remains authoritative."]},
                "freshness_assumptions",
            ),
            ({**valid, "created_at": "not-a-date"}, "created_at"),
            ({**valid, "created_at": "2026-08-25T12:00:00"}, "created_at"),
            ({**valid, "relation": {"kind": "invented", "item": None}}, "relation.kind"),
            ({**valid, "relation": {"kind": "independent", "item": "work-a"}}, "relation.item"),
            ({**valid, "relation": {"kind": "prerequisite", "item": None}}, "relation.item"),
        )
        for value, field in cases:
            with self.subTest(field=field):
                failure = parse_proposal(json.dumps(value))
            self.assertIsInstance(failure, ProposalFailure)
            self.assertEqual("PROPOSAL_INVALID", failure.code.value)
            self.assertIn(field, failure.message)


if __name__ == "__main__":
    unittest.main()
