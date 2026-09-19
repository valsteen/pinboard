import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import msgspec

from pinboard.application import proposal_models
from pinboard.application.proposal_models import Proposal
from pinboard.mcp import contracts
from pinboard.mcp import execution as mcp_execution
from pinboard.mcp import mutation_operations as mcp_mutations


def proposal() -> dict[str, proposal_models.ProposalJsonValue]:
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
        common: dict[str, proposal_models.ProposalJsonValue] = {
            "project_root": "/project",
            "work_root": "/work",
            "actor_task_id": "task",
            "actor_host_id": "host",
        }

        def decoded(proposal: dict[str, proposal_models.ProposalJsonValue]) -> Proposal:
            return msgspec.convert(common | {"proposal": proposal}, type=contracts.ProposalCreateRequest).proposal

        current = decoded(value)
        positioned = decoded(value | {"position": 2})
        date_only = decoded(value | {"created_at": "2026-08-25"})
        timezone_aware = decoded(value | {"created_at": "2026-08-25T12:00:00+02:00"})
        with self.assertRaises(msgspec.ValidationError):
            decoded(value | {"unexpected": True})

        self.assertEqual("proposal-1", current.proposal_id)
        self.assertEqual(2, positioned.position)
        self.assertEqual(datetime(2026, 8, 25, tzinfo=UTC), date_only.created_at_utc())
        self.assertEqual(datetime(2026, 8, 25, 10, tzinfo=UTC), timezone_aware.created_at_utc())

    def test_decoder_rejects_invalid_shapes_and_reports_paths(self) -> None:
        valid = proposal()
        cases: tuple[tuple[dict[str, proposal_models.ProposalJsonValue], str], ...] = (
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
            with self.subTest(field=field), patch.object(mcp_mutations.common, "_resolve_durable") as durable:
                failure = mcp_mutations._proposal_created(
                    "/project",
                    "/work",
                    value,
                    "task",
                    "host",
                    mcp_execution.CancellationToken(),
                )
                self.assertEqual("PROPOSAL_INVALID", failure.content["code"])
                message = failure.content["message"]
                assert isinstance(message, str)
                self.assertIn(field, message)
                durable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
