import hashlib
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import msgspec
from msgspec.structs import replace as replace_struct

from pinboard.application import proposal_models, proposals, query_models, work_brief_models
from pinboard.application.work_briefs import validate_definition_brief_agreement
from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import available_actions, decide
from pinboard.domain.definition_decisions import decide_definition_revision
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import decode_work_item_definition, work_item_definition_bytes, work_item_definition_digest
from pinboard.domain.identifiers import AttemptId, CheckpointId, LeaseId, TaskId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.domain.proposal_decisions import decide_proposal_creation
from tests.domain_support import expect_success
from tests.support import SQLITE_DEFINITION
from tests.test_proposals import proposal as proposal_payload
from tests.work_brief_support import example_work_brief

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
        dependencies=(WorkItemId("survey-west"), WorkItemId("survey-east")),
        effect="Navigable routes are available.",
        unlock="The party can reach the next area.",
        checkout_policy=work_models.CheckoutPolicy.ISOLATED,
        obligations=(
            work_models.WorkObligation(
                work_models.ObligationId("reachable-area"),
                "The next area is reachable.",
                work_models.ObligationDeferralPolicy.FORBIDDEN,
            ),
        ),
        checkpoints=(),
    )


class WorkItemDefinitionContractTest(unittest.TestCase):
    def test_historical_v1_cannot_become_a_current_definition(self) -> None:
        payload = (
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

        decoded = decode_work_item_definition(payload)

        self.assertIsInstance(decoded, DecisionFailure)
        assert isinstance(decoded, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_DEFINITION_INVALID, decoded.code)

    def test_definition_has_one_frozen_canonical_identity(self) -> None:
        expected = (
            b'{"acceptance_criteria":["The next area is reachable."],'
            b'"checkout_policy":"isolated",'
            b'"dependencies":["survey-west","survey-east"],'
            b'"effect":"Navigable routes are available.",'
            b'"evidence":["artifacts/requirements/routes.md"],'
            b'"hypothesis":"The party cannot travel without a reliable route.",'
            b'"non_scope":["Do not redesign combat."],'
            b'"objective":"Add navigable routes",'
            b'"obligations":[{"deferral_policy":"forbidden","obligation_id":"reachable-area",'
            b'"statement":"The next area is reachable."}],'
            b'"schema":"pinboard-work-item-definition/v2",'
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

    def test_declared_checkpoints_roundtrip_validate_uniqueness_and_project_views(self) -> None:
        declared = replace(
            definition(),
            checkpoints=(CheckpointId("survey-routes"), CheckpointId("build-bridges")),
        )
        encoded = expect_success(work_item_definition_bytes(declared))
        self.assertIn(b'"checkpoints":["survey-routes","build-bridges"]', encoded)
        self.assertEqual(declared, expect_success(decode_work_item_definition(encoded)))

        carried = replace(
            declared,
            obligations=(
                work_models.CarriedWorkObligation(
                    work_models.ObligationId("reachable-area"),
                    "The next area is reachable.",
                    work_models.ObligationDeferralPolicy.FORBIDDEN,
                    work_models.ObligationOrigin(
                        WorkItemId("survey-west"),
                        1,
                        work_models.ObligationId("reachable-area"),
                    ),
                ),
            ),
        )
        carried_encoded = expect_success(work_item_definition_bytes(carried))
        self.assertIn(b'"schema":"pinboard-work-item-definition/v3"', carried_encoded)
        self.assertIn(b'"checkpoints":["survey-routes","build-bridges"]', carried_encoded)
        self.assertEqual(carried, expect_success(decode_work_item_definition(carried_encoded)))

        duplicate = replace(
            definition(),
            checkpoints=(CheckpointId("survey-routes"), CheckpointId("survey-routes")),
        )
        rejected = work_item_definition_bytes(duplicate)
        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_DEFINITION_INVALID, rejected.code)

        empty_view = query_models.project_definition(definition())
        assert isinstance(empty_view, query_models.WorkItemDefinitionView)
        self.assertEqual((), empty_view.checkpoints)
        self.assertNotIn(b'"checkpoints"', msgspec.json.encode(empty_view))

        declared_view = query_models.project_definition(declared)
        assert isinstance(declared_view, query_models.WorkItemDefinitionView)
        self.assertEqual(("survey-routes", "build-bridges"), declared_view.checkpoints)
        self.assertIn(b'"checkpoints":["survey-routes","build-bridges"]', msgspec.json.encode(declared_view))

        carried_view = query_models.project_definition(carried)
        assert isinstance(carried_view, query_models.OriginWorkItemDefinitionView)
        self.assertEqual(("survey-routes", "build-bridges"), carried_view.checkpoints)
        self.assertIn(b'"checkpoints":["survey-routes","build-bridges"]', msgspec.json.encode(carried_view))

    def test_proposal_checkpoints_convert_into_ready_definition_and_preserve_prerequisite_checkpoints(self) -> None:
        raw = proposal_payload()
        raw["checkpoints"] = ["phase-1", "phase-2"]
        converted = proposals.convert_proposal(msgspec.convert(raw, type=proposal_models.Proposal))
        self.assertEqual((CheckpointId("phase-1"), CheckpointId("phase-2")), converted.intake.checkpoints)

        duplicate_raw = {**raw, "checkpoints": ["phase-1", "phase-1"]}
        with self.assertRaises(msgspec.ValidationError):
            msgspec.convert(duplicate_raw, type=proposal_models.Proposal)

        anchor_def = replace(definition(), checkpoints=(CheckpointId("existing-cp"),))
        anchor_digest = expect_success(work_item_definition_digest(anchor_def))
        target_item = work_models.WorkItem(
            WorkItemId("work-c"),
            work_models.WorkState.READY,
            None,
            anchor_def.dependencies,
            None,
            "source",
            "continue",
            "",
            1,
        )
        snapshot = LedgerSnapshot(
            "1",
            (target_item,),
            definitions=(work_models.DefinitionAnchor(target_item.work_item_id, 1, anchor_digest, anchor_def),),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
        )
        prereq_raw = {
            **raw,
            "relation": {"kind": "prerequisite", "item": "work-c"},
        }
        prereq_op = proposals.convert_proposal(msgspec.convert(prereq_raw, type=proposal_models.Proposal))
        created = expect_success(decide_proposal_creation(snapshot, prereq_op, 1))
        self.assertEqual((CheckpointId("phase-1"), CheckpointId("phase-2")), created.ready_item.definition.checkpoints)
        assert created.prerequisite_change is not None
        self.assertEqual((CheckpointId("existing-cp"),), created.prerequisite_change.definition_after.checkpoints)

    def test_validate_definition_brief_agreement_enforces_declared_checkpoints_and_remaining_work(self) -> None:
        brief = example_work_brief()
        declared_def = replace(
            SQLITE_DEFINITION,
            checkpoints=(CheckpointId("phase-1"), CheckpointId("phase-2"), CheckpointId("phase-3")),
        )
        undeclared = validate_definition_brief_agreement(declared_def, brief)
        self.assertIsNotNone(undeclared)
        assert undeclared is not None
        self.assertEqual(work_brief_models.WorkBriefErrorCode.BRIEF_INVALID, undeclared.code)

        terminal_early = replace_struct(
            brief,
            checkpoint=replace_struct(
                brief.checkpoint,
                checkpoint_id="phase-1",
                disposition=work_brief_models.TerminalCheckpointDisposition(),
            ),
        )
        rejected_terminal = validate_definition_brief_agreement(declared_def, terminal_early)
        self.assertIsNotNone(rejected_terminal)
        assert rejected_terminal is not None
        self.assertEqual(work_brief_models.WorkBriefErrorCode.BRIEF_INVALID, rejected_terminal.code)

        missing_remaining = replace_struct(
            brief,
            checkpoint=replace_struct(
                brief.checkpoint,
                checkpoint_id="phase-1",
                disposition=work_brief_models.ContinueCheckpointDisposition("Remaining checkpoint: phase-2."),
            ),
        )
        rejected_missing = validate_definition_brief_agreement(declared_def, missing_remaining)
        self.assertIsNotNone(rejected_missing)
        assert rejected_missing is not None
        self.assertEqual(work_brief_models.WorkBriefErrorCode.BRIEF_INVALID, rejected_missing.code)
        self.assertIn("phase-3", rejected_missing.message)

        valid_continue = replace_struct(
            brief,
            checkpoint=replace_struct(
                brief.checkpoint,
                checkpoint_id="phase-1",
                disposition=work_brief_models.ContinueCheckpointDisposition("Remaining checkpoints: phase-2, phase-3."),
            ),
        )
        self.assertIsNone(validate_definition_brief_agreement(declared_def, valid_continue))

        valid_final = replace_struct(
            brief,
            checkpoint=replace_struct(
                brief.checkpoint,
                checkpoint_id="phase-3",
                disposition=work_brief_models.TerminalCheckpointDisposition(),
            ),
        )
        self.assertIsNone(validate_definition_brief_agreement(declared_def, valid_final))


class WorkItemDefinitionRevisionDecisionTest(unittest.TestCase):
    def test_revise_uses_exact_compare_and_swap_and_preserves_lifecycle(self) -> None:
        current = definition()
        current_digest = expect_success(work_item_definition_digest(current))
        item = work_models.WorkItem(
            WorkItemId("build-map"),
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
            definitions=(work_models.DefinitionAnchor(item.work_item_id, 3, current_digest, current),),
            history_items=(WorkItemId("survey-west"), WorkItemId("survey-east")),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
        )

        decision = expect_success(
            decide_definition_revision(
                snapshot,
                item.work_item_id,
                work_models.ReviseWorkItemDefinitionInput(
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
            WorkItemId("build-map"), work_models.WorkState.PAUSED, None, (), None, "source", "continue", "", 1
        )
        survey = work_models.WorkItem(
            WorkItemId("survey-west"),
            work_models.WorkState.READY,
            None,
            (build.work_item_id,),
            None,
            "source",
            "continue",
            "",
            2,
        )
        snapshot = LedgerSnapshot(
            "ledger-revision",
            (build, survey),
            definitions=(
                work_models.DefinitionAnchor(build.work_item_id, 3, current_digest, current),
                work_models.DefinitionAnchor(
                    survey.work_item_id,
                    1,
                    "a" * 64,
                    replace(current, dependencies=(build.work_item_id,)),
                ),
            ),
            history_items=(WorkItemId("survey-east"),),
            accepted_checkpoints=frozenset(),
            dependency_facts=(work_models.DefinitionDependencies(survey.work_item_id, (build.work_item_id,)),),
            dropped_item_ids=frozenset(),
        )

        cases = (
            (
                work_models.ReviseWorkItemDefinitionInput(2, current_digest, TaskId("owner-task"), "Reason", current),
                DecisionFailureCode.ITEM_DEFINITION_STALE,
            ),
            (
                work_models.ReviseWorkItemDefinitionInput(
                    3,
                    current_digest,
                    TaskId("owner-task"),
                    "Reason",
                    replace(current, dependencies=(WorkItemId("absent"),)),
                ),
                DecisionFailureCode.DEPENDENCY_NOT_SATISFIED,
            ),
            (
                work_models.ReviseWorkItemDefinitionInput(
                    3,
                    current_digest,
                    TaskId("owner-task"),
                    "Reason",
                    replace(current, dependencies=(survey.work_item_id,)),
                ),
                DecisionFailureCode.ITEM_DEPENDENCY_CYCLE,
            ),
        )
        for value, code in cases:
            with self.subTest(code=code):
                rejected = decide_definition_revision(snapshot, build.work_item_id, value, NOW)
                assert isinstance(rejected, DecisionFailure)
                self.assertEqual(code, rejected.code)

    def test_terminal_item_definition_cannot_be_revised(self) -> None:
        current = definition()
        digest = expect_success(work_item_definition_digest(current))
        snapshot = LedgerSnapshot(
            "ledger-revision",
            (),
            definitions=(),
            history_items=(WorkItemId("done"),),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
        )

        rejected = decide_definition_revision(
            snapshot,
            WorkItemId("done"),
            work_models.ReviseWorkItemDefinitionInput(1, digest, TaskId("owner"), "Reason", current),
            NOW,
        )

        self.assertEqual(
            DecisionFailure(
                DecisionFailureCode.ITEM_DEFINITION_LIFECYCLE_INVALID,
                "A terminal work item cannot be revised.",
                None,
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
            WorkItemId("build-map"),
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
            item.work_item_id,
            work_models.AttemptState.ACTIVE,
            1,
            accepted_digest,
            pause_reason=None,
            pending_decision_batch=None,
        )
        authority = work_models.AttemptAuthority(attempt_id, item.work_item_id, LeaseId("worker-lease"), 1)
        snapshot = LedgerSnapshot(
            "revision",
            (item,),
            attempts=(attempt,),
            attempt_authorities=(authority,),
            definitions=(work_models.DefinitionAnchor(item.work_item_id, 2, revised_digest, revised),),
            subject_revisions=(
                work_models.SubjectRevision(item.work_item_id, "2"),
                work_models.SubjectRevision(attempt_id, "1"),
            ),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
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
        self.assertEqual({"report-blocker:build-map-1", "pause:build-map-1"}, worker_ids)

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

    def test_revise_allows_appending_checkpoints_and_rejects_removing_declared_checkpoints(self) -> None:
        current = replace(
            definition(),
            checkpoints=(CheckpointId("phase-1"), CheckpointId("phase-2")),
        )
        current_digest = expect_success(work_item_definition_digest(current))
        item = work_models.WorkItem(
            WorkItemId("build-map"),
            work_models.WorkState.READY,
            None,
            current.dependencies,
            None,
            "proposal:build-map",
            "Continue",
            "",
            1,
        )
        snapshot = LedgerSnapshot(
            "ledger-revision",
            (item,),
            definitions=(work_models.DefinitionAnchor(item.work_item_id, 1, current_digest, current),),
            history_items=(WorkItemId("survey-west"), WorkItemId("survey-east")),
            accepted_checkpoints=frozenset(),
            dependency_facts=(),
            dropped_item_ids=frozenset(),
        )
        appended = replace(
            current,
            checkpoints=(CheckpointId("phase-1"), CheckpointId("phase-2"), CheckpointId("phase-3")),
        )
        decision = expect_success(
            decide_definition_revision(
                snapshot,
                item.work_item_id,
                work_models.ReviseWorkItemDefinitionInput(
                    1,
                    current_digest,
                    TaskId("owner-task"),
                    "Append phase-3 checkpoint.",
                    appended,
                ),
                NOW,
            )
        )
        self.assertEqual(appended.checkpoints, decision.definition.checkpoints)

        removed = replace(current, checkpoints=(CheckpointId("phase-1"),))
        rejected = decide_definition_revision(
            snapshot,
            item.work_item_id,
            work_models.ReviseWorkItemDefinitionInput(
                1,
                current_digest,
                TaskId("owner-task"),
                "Attempt to remove phase-2.",
                removed,
            ),
            NOW,
        )
        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_DEFINITION_INVALID, rejected.code)
        self.assertIn("phase-2", rejected.message)


if __name__ == "__main__":
    unittest.main()
