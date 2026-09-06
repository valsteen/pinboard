import hashlib
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import msgspec

from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import available_actions, decide
from pinboard.domain.definition_decisions import decide_definition_revision
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import work_item_definition_bytes, work_item_definition_digest
from pinboard.domain.identifiers import AttemptId, ItemId, LeaseId, TaskId
from pinboard.domain.ledger import LedgerSnapshot
from tests.domain_support import expect_success

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def definition() -> work_models.WorkItemDefinition:
    return work_models.WorkItemDefinition(
        title="Build the map",
        objective="Add navigable routes",
        hypothesis="The party cannot travel without a reliable route.",
        evidence=("artifacts/requirements/routes.md",),
        scope=("Map the western route.", "Map the eastern route."),
        non_scope=("Do not redesign combat.",),
        acceptance_criteria=("The next area is reachable.",),
        dependencies=(ItemId("survey-west"), ItemId("survey-east")),
        effect="Navigable routes are available.",
        unlock="The party can reach the next area.",
    )


class WorkItemDefinitionContractTest(unittest.TestCase):
    def test_definition_has_one_frozen_canonical_identity(self) -> None:
        expected = (
            b'{"acceptance_criteria":["The next area is reachable."],'
            b'"dependencies":["survey-west","survey-east"],'
            b'"effect":"Navigable routes are available.",'
            b'"evidence":["artifacts/requirements/routes.md"],'
            b'"hypothesis":"The party cannot travel without a reliable route.",'
            b'"non_scope":["Do not redesign combat."],'
            b'"objective":"Add navigable routes",'
            b'"schema":"pinboard-work-item-definition/v1",'
            b'"scope":["Map the western route.","Map the eastern route."],'
            b'"title":"Build the map",'
            b'"unlock":"The party can reach the next area."}\n'
        )

        self.assertEqual(expected, expect_success(work_item_definition_bytes(definition())))
        self.assertEqual(
            hashlib.sha256(expected).hexdigest(),
            expect_success(work_item_definition_digest(definition())),
        )

    def test_outbound_conversion_failure_maps_to_definition_invalid(self) -> None:
        with patch("pinboard.domain.history.msgspec.convert", side_effect=msgspec.ValidationError("invalid")):
            result = work_item_definition_bytes(definition())

        self.assertIsInstance(result, DecisionFailure)
        assert isinstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_DEFINITION_INVALID, result.code)

    def test_empty_evidence_non_scope_and_dependencies_are_explicitly_valid(self) -> None:
        sparse = replace(definition(), evidence=(), non_scope=(), dependencies=())

        self.assertIsInstance(expect_success(work_item_definition_bytes(sparse)), bytes)


class WorkItemDefinitionRevisionDecisionTest(unittest.TestCase):
    def test_revise_uses_exact_compare_and_swap_and_preserves_lifecycle(self) -> None:
        current = definition()
        current_digest = expect_success(work_item_definition_digest(current))
        item = work_models.WorkItem(
            ItemId("build-map"),
            work_models.WorkState.ACTIVE,
            None,
            current.dependencies,
            None,
            "proposal:build-map",
            "Continue",
            "",
            1,
        )
        revised = replace(current, objective="Add safe navigable routes")
        snapshot = LedgerSnapshot(
            "ledger-revision",
            (item,),
            definitions=(work_models.DefinitionAnchor(item.item, 3, current_digest, current),),
            history_items=(ItemId("survey-west"), ItemId("survey-east")),
        )

        decision = expect_success(
            decide_definition_revision(
                snapshot,
                item.item,
                work_models.ReviseItemDefinitionInput(
                    item.item,
                    3,
                    current_digest,
                    TaskId("owner-task"),
                    "Clarify the safety requirement.",
                    revised,
                ),
                NOW,
            )
        )

        self.assertEqual(4, decision.revision)
        self.assertEqual(current_digest, decision.before_digest)
        self.assertEqual(expect_success(work_item_definition_digest(revised)), decision.after_digest)
        self.assertEqual(work_models.WorkState.ACTIVE, item.state)

    def test_revise_rejects_stale_identity_missing_dependency_and_cycle(self) -> None:
        current = definition()
        current_digest = expect_success(work_item_definition_digest(current))
        build = work_models.WorkItem(
            ItemId("build-map"), work_models.WorkState.PAUSED, None, (), None, "source", "continue", "", 1
        )
        survey = work_models.WorkItem(
            ItemId("survey-west"), work_models.WorkState.READY, None, (build.item,), None, "source", "continue", "", 2
        )
        snapshot = LedgerSnapshot(
            "ledger-revision",
            (build, survey),
            definitions=(
                work_models.DefinitionAnchor(build.item, 3, current_digest, current),
                work_models.DefinitionAnchor(
                    survey.item,
                    1,
                    "a" * 64,
                    replace(current, dependencies=(build.item,)),
                ),
            ),
            history_items=(ItemId("survey-east"),),
        )

        cases = (
            (
                work_models.ReviseItemDefinitionInput(
                    build.item, 2, current_digest, TaskId("owner-task"), "Reason", current
                ),
                DecisionFailureCode.ITEM_DEFINITION_STALE,
            ),
            (
                work_models.ReviseItemDefinitionInput(
                    build.item,
                    3,
                    current_digest,
                    TaskId("owner-task"),
                    "Reason",
                    replace(current, dependencies=(ItemId("absent"),)),
                ),
                DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            ),
            (
                work_models.ReviseItemDefinitionInput(
                    build.item,
                    3,
                    current_digest,
                    TaskId("owner-task"),
                    "Reason",
                    replace(current, dependencies=(survey.item,)),
                ),
                DecisionFailureCode.ITEM_DEPENDENCY_CYCLE,
            ),
        )
        for value, code in cases:
            with self.subTest(code=code):
                rejected = decide_definition_revision(snapshot, build.item, value, NOW)
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(code, rejected.code)

    def test_terminal_item_definition_cannot_be_revised(self) -> None:
        current = definition()
        digest = expect_success(work_item_definition_digest(current))
        snapshot = LedgerSnapshot(
            "ledger-revision",
            (),
            definitions=(work_models.DefinitionAnchor(ItemId("done"), 1, digest, current),),
            history_items=(ItemId("done"),),
        )

        rejected = decide_definition_revision(
            snapshot,
            ItemId("done"),
            work_models.ReviseItemDefinitionInput(ItemId("done"), 1, digest, TaskId("owner"), "Reason", current),
            NOW,
        )

        self.assertEqual(
            DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID,
                "A terminal work item cannot be revised.",
            ),
            rejected,
        )

    def test_revised_active_attempt_loses_progress_and_acceptance_actions_but_keeps_recovery_paths(
        self,
    ) -> None:
        accepted = definition()
        accepted_digest = expect_success(work_item_definition_digest(accepted))
        revised = replace(accepted, objective="Add safe navigable routes")
        revised_digest = expect_success(work_item_definition_digest(revised))
        attempt_id = AttemptId("build-map-1")
        item = work_models.WorkItem(
            ItemId("build-map"),
            work_models.WorkState.ACTIVE,
            None,
            (),
            attempt_id,
            "source",
            "continue",
            "",
            1,
        )
        attempt = work_models.AttemptRecord(
            attempt_id,
            item.item,
            work_models.AttemptState.ACTIVE,
            1,
            accepted_digest,
        )
        authority = work_models.AttemptAuthority(attempt_id, item.item, LeaseId("worker-lease"), 1)
        snapshot = LedgerSnapshot(
            "revision",
            (item,),
            attempts=(attempt,),
            attempt_authorities=(authority,),
            definitions=(work_models.DefinitionAnchor(item.item, 2, revised_digest, revised),),
        )
        project = expect_success(
            available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.PROJECT,
                    decision_models.AuthorizationKind.PROJECT,
                    1,
                ),
            )
        )
        worker = expect_success(
            available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.WORKER,
                    decision_models.AuthorizationKind.ATTEMPT,
                    1,
                    LeaseId("worker-lease"),
                    (attempt_id,),
                ),
            )
        )

        project_ids = {decision_models.action_id(value) for value in project}
        worker_ids = {decision_models.action_id(value) for value in worker}
        self.assertTrue(
            {
                "rebind-attempt:build-map-1",
                "pause:build-map-1",
                "block:build-map-1",
                "revise-item:build-map",
            }
            <= project_ids
        )
        self.assertTrue(
            {"continue:build-map-1", "dispatch:build-map-1", "complete:build-map-1"}.isdisjoint(project_ids)
        )
        self.assertEqual({"report-blocker:build-map-1"}, worker_ids)

        capability = decision_models.MutationActionCapability(attempt_id, "complete", "revision")
        rejected = decide(
            snapshot,
            decision_models.CompleteCommand(
                decision_models.CompleteAction(capability),
                work_models.EvidenceInput("done"),
            ),
            NOW,
        )
        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_DEFINITION_STALE, rejected.code)


if __name__ == "__main__":
    unittest.main()
