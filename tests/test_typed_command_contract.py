import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import available_actions as available_actions_outcome
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.decisions import validate_supplied_action
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HostId,
    ItemId,
    LeaseId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.interfaces.transition_input import parse_transition_command
from tests.decision_support import project_decision_snapshot
from tests.domain_support import action, expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state, test_definition


def _worker_actor() -> decision_models.ActorAuthority:
    return decision_models.ActorAuthority(
        decision_models.Role.WORKER,
        decision_models.AuthorizationKind.ATTEMPT,
        3,
        LeaseId("attempt-lease-a"),
        (AttemptId("work-a-1"),),
    )


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority
) -> tuple[decision_models.Action, ...]:
    return expect_success(available_actions_outcome(snapshot, actor))


def decide(
    snapshot: LedgerSnapshot, command: decision_models.TransitionCommand, now: datetime
) -> decision_models.Decision:
    return expect_success(decision_outcome(snapshot, command, now))


def _stored_action(snapshot: LedgerSnapshot) -> decision_models.SubmitReviewAction:
    selected = next(
        candidate
        for candidate in available_actions(snapshot, _worker_actor())
        if candidate.kind == decision_models.ActionKind.SUBMIT_REVIEW
    )
    assert isinstance(selected, decision_models.SubmitReviewAction)
    return selected


class TypedTransitionContractTest(unittest.TestCase):
    def test_submit_review_candidate_is_required_typed_and_preserved(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        submit = _stored_action(snapshot)

        candidate = CandidateId("candidate-that-is-not-a-subject-revision")
        parsed = parse_transition_command(
            submit,
            b'{"candidate":"candidate-that-is-not-a-subject-revision"}',
        )
        self.assertIsInstance(parsed, decision_models.SubmitReviewCommand)
        assert isinstance(parsed, decision_models.SubmitReviewCommand)
        self.assertEqual(work_models.SubmitReviewInput(candidate), parsed.value)
        decision = decide(snapshot, parsed, SQLITE_NOW)
        self.assertIsInstance(decision.change, decision_models.ReviewSubmissionChange)
        assert isinstance(decision.change, decision_models.ReviewSubmissionChange)
        self.assertEqual(candidate, decision.change.protected_candidate_after)
        self.assertEqual(SQLITE_NOW, decision.change.candidate_observed_at)

    def test_activation_requires_one_existing_brief_artifact_reference(self) -> None:
        ready = work_models.WorkItem(
            ItemId("ready-item"), work_models.WorkState.READY, None, (), None, "test", "activate", "", 1
        )
        snapshot = LedgerSnapshot(
            "project-revision",
            (ready,),
            artifacts=(
                work_models.ArtifactRecord(ArtifactRefId(1), work_models.ArtifactKind.BRIEF),
                work_models.ArtifactRecord(ArtifactRefId(2), work_models.ArtifactKind.RESULT),
            ),
            definitions=(
                work_models.DefinitionAnchor(
                    ItemId("ready-item"), 1, "d" * 64, test_definition(ItemId("ready-item"))[0]
                ),
            ),
        )
        preparation = work_models.PreparationCommandAuthority(
            1,
            ItemId("ready-item"),
            1,
            "d" * 64,
            TaskId("preparer"),
            HostId("host"),
            LeaseId("preparation"),
            1,
            SQLITE_NOW + timedelta(minutes=5),
        )
        snapshot = replace(
            snapshot,
            definitions=(
                replace(
                    snapshot.definitions[0], definition=replace(snapshot.definitions[0].definition, dependencies=())
                ),
            ),
        )
        activation = decision_models.ActivateAction(
            decision_models.MutationActionCapability(
                ItemId("ready-item"),
                "activate",
                "1",
                authorization=decision_models.AuthorizationKind.PREPARATION,
                lease_id=preparation.lease_id,
                preparation_authority=preparation,
            )
        )

        variants = (
            work_models.ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(99)),
            work_models.ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(2)),
        )
        for value in variants:
            with self.subTest(value=value):
                selected_command = decision_models.ActivateCommand(activation, value)
                rejected = decision_outcome(snapshot, selected_command, SQLITE_NOW)
                self.assertIsInstance(rejected, DecisionFailure)
            self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        accepted = decide(
            snapshot,
            decision_models.ActivateCommand(
                activation,
                work_models.ActivateInput(AttemptId("ready-item-1"), "branch", "base", "task", ArtifactRefId(1)),
            ),
            SQLITE_NOW,
        )
        self.assertIsInstance(accepted.change, decision_models.ActivationChange)
        assert isinstance(accepted.change, decision_models.ActivationChange)
        self.assertEqual(ArtifactRefId(1), accepted.change.brief_artifact_ref_id)

    def test_resume_may_replace_the_attempt_brief_with_one_existing_brief_reference(self) -> None:
        without_attempt = LedgerSnapshot(
            "project-revision",
            (
                work_models.WorkItem(
                    ItemId("ready-item"),
                    work_models.WorkState.PAUSED,
                    None,
                    (),
                    None,
                    "test",
                    "resume",
                    "",
                    1,
                ),
            ),
            artifacts=(work_models.ArtifactRecord(ArtifactRefId(1), work_models.ArtifactKind.BRIEF),),
        )
        rejected_without_attempt = decision_outcome(
            without_attempt,
            decision_models.ResumeCommand(
                action(decision_models.ResumeAction, ItemId("ready-item")), work_models.ResumeInput(ArtifactRefId(1))
            ),
            SQLITE_NOW,
        )
        self.assertIsInstance(rejected_without_attempt, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected_without_attempt.code)

        paused = work_models.WorkItem(
            ItemId("ready-item"),
            work_models.WorkState.PAUSED,
            None,
            (),
            AttemptId("ready-item-1"),
            "test",
            "resume",
            "",
            1,
        )
        snapshot = LedgerSnapshot(
            "project-revision",
            (paused,),
            attempts=(
                work_models.AttemptRecord(
                    AttemptId("ready-item-1"),
                    ItemId("ready-item"),
                    work_models.AttemptState.PAUSED,
                    brief_artifact_ref_id=ArtifactRefId(1),
                ),
            ),
            artifacts=(
                work_models.ArtifactRecord(ArtifactRefId(1), work_models.ArtifactKind.BRIEF),
                work_models.ArtifactRecord(ArtifactRefId(2), work_models.ArtifactKind.BRIEF),
                work_models.ArtifactRecord(ArtifactRefId(3), work_models.ArtifactKind.RESULT),
            ),
            definitions=(
                work_models.DefinitionAnchor(
                    ItemId("ready-item"),
                    2,
                    "d" * 64,
                    work_models.WorkItemDefinition(
                        "Ready item", "effect", "why", (), ("effect",), (), ("unlock",), (), "effect", "unlock"
                    ),
                ),
            ),
        )
        resume = action(decision_models.ResumeAction, ItemId("ready-item"))

        for value in (work_models.ResumeInput(ArtifactRefId(99)), work_models.ResumeInput(ArtifactRefId(3))):
            with self.subTest(value=value):
                rejected = decision_outcome(snapshot, decision_models.ResumeCommand(resume, value), SQLITE_NOW)
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)

        accepted = decide(
            snapshot,
            decision_models.ResumeCommand(resume, work_models.ResumeInput(ArtifactRefId(2))),
            SQLITE_NOW,
        )
        self.assertIsInstance(accepted.change, decision_models.ResumeAttemptChange)
        assert isinstance(accepted.change, decision_models.ResumeAttemptChange)
        revised_brief = accepted.change.revised_brief
        self.assertIsNotNone(revised_brief)
        assert revised_brief is not None
        self.assertEqual(
            (ArtifactRefId(2), 2, "d" * 64),
            (
                revised_brief.artifact_ref_id,
                revised_brief.accepted_scope_revision,
                revised_brief.accepted_scope_digest,
            ),
        )


class ExactMutationAuthorityTest(unittest.TestCase):
    def test_action_preserves_every_attempt_authority_fact(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        selected = _stored_action(snapshot)
        authority = selected.capability.command_authority
        self.assertIsNotNone(authority)
        assert authority is not None
        self.assertEqual(
            (2, "work-a", "7", "work-a-1", "8", "worker", "host-a", "attempt-lease-a", 3),
            (
                authority.host_epoch,
                authority.item,
                authority.item_subject_revision,
                authority.attempt,
                authority.attempt_subject_revision,
                authority.task_id,
                authority.host_id,
                authority.lease_id,
                authority.generation,
            ),
        )

    def test_exact_validation_rejects_single_fact_substitutions_not_global_revision(self) -> None:
        state = complete_sqlite_state()
        snapshot = project_decision_snapshot(state, SQLITE_NOW)
        selected = _stored_action(snapshot)
        authority = selected.capability.command_authority
        assert authority is not None
        substitutions = (
            replace(
                selected,
                capability=replace(
                    selected.capability,
                    command_authority=replace(authority, generation=authority.generation + 1),
                ),
            ),
        )
        for supplied in substitutions:
            with self.subTest(supplied=supplied):
                rejected = validate_supplied_action(snapshot, _worker_actor(), supplied)
                self.assertIsInstance(rejected, DecisionFailure)
                assert isinstance(rejected, DecisionFailure)
            self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, rejected.code)

        advanced_state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                project=replace(state.lifecycle.project, revision=state.lifecycle.project.revision + 1),
            ),
        )
        self.assertIsNone(
            validate_supplied_action(project_decision_snapshot(advanced_state, SQLITE_NOW), _worker_actor(), selected)
        )


if __name__ == "__main__":
    unittest.main()
