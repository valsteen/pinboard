import unittest
from dataclasses import replace as replace_dataclass
from datetime import UTC, datetime

from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import ActionCapabilityFactory, project_attempt_action_groups
from pinboard.domain.decisions import available_actions as available_actions_outcome
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    HistoryId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from tests.domain_support import (
    accept_proposal_input as AcceptProposalInput,
)
from tests.domain_support import (
    action,
    expect_success,
    replace,
)
from tests.domain_support import (
    attempt_record as AttemptRecord,
)
from tests.domain_support import (
    defer_input as DeferInput,
)
from tests.domain_support import (
    proposal_record as ProposalRecord,
)

NOW = datetime(2026, 8, 21, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority
) -> tuple[decision_models.Action, ...]:
    known_subjects = {value.subject for value in snapshot.subject_revisions}
    snapshot = replace_dataclass(
        snapshot,
        subject_revisions=(
            *snapshot.subject_revisions,
            *(
                work_models.SubjectRevision(value.item, "1")
                for value in snapshot.items
                if value.item not in known_subjects
            ),
            *(
                work_models.SubjectRevision(value.attempt, "1")
                for value in snapshot.attempts
                if value.attempt not in known_subjects
            ),
            *(
                work_models.SubjectRevision(value.proposal, value.revision)
                for value in snapshot.proposals
                if value.proposal not in known_subjects
            ),
        ),
    )
    return expect_success(available_actions_outcome(snapshot, actor))


def decide(
    snapshot: LedgerSnapshot, command: decision_models.TransitionCommand, now: datetime
) -> decision_models.Decision:
    return expect_success(decision_outcome(snapshot, command, now))


def item(item_id: str, state: work_models.WorkState, *, attempt: str | None = None) -> work_models.WorkItem:
    return work_models.WorkItem(
        ItemId(item_id),
        state,
        None,
        (),
        AttemptId(attempt) if attempt is not None else None,
        "design",
        "continue",
        "",
        1,
    )


def definition_anchor(
    item_id: str,
    revision: int,
    digest: str,
    dependencies: tuple[ItemId, ...] = (),
) -> work_models.DefinitionAnchor:
    return work_models.DefinitionAnchor(
        ItemId(item_id),
        revision,
        digest,
        work_models.WorkItemDefinition(
            "Build the map",
            "Add navigable routes",
            "The party cannot travel",
            (),
            ("Add navigable routes",),
            (),
            ("Reach the next area",),
            dependencies,
            "Add navigable routes",
            "Reach the next area",
        ),
    )


class LifecycleDecisionTest(unittest.TestCase):
    def test_project_attempt_action_owner_preserves_every_continuation_sequence(self) -> None:
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT,
            decision_models.AuthorizationKind.PROJECT,
            0,
        )
        factory = ActionCapabilityFactory(actor)
        current_definition = definition_anchor("target", 1, DIGEST_A)
        dependency = item("dependency", work_models.WorkState.READY)
        cases = (
            (
                "active-current",
                work_models.WorkState.ACTIVE,
                work_models.AttemptState.ACTIVE,
                DIGEST_A,
                (),
                (
                    "continue:target-1",
                    "dispatch:target-1",
                    "rebind-attempt:target-1",
                    "pause:target-1",
                    "block:target-1",
                    "complete:target-1",
                    "revise-item:target",
                ),
            ),
            (
                "active-stale",
                work_models.WorkState.ACTIVE,
                work_models.AttemptState.ACTIVE,
                DIGEST_B,
                (),
                (
                    "rebind-attempt:target-1",
                    "pause:target-1",
                    "block:target-1",
                    "revise-item:target",
                ),
            ),
            (
                "review-current",
                work_models.WorkState.REVIEW,
                work_models.AttemptState.REVIEW,
                DIGEST_A,
                (),
                (
                    "complete:target-1",
                    "return-for-correction:target-1",
                    "accept-checkpoint:target-1",
                    "accept-review-and-continue:target-1",
                    "revise-item:target",
                ),
            ),
            (
                "review-stale",
                work_models.WorkState.REVIEW,
                work_models.AttemptState.REVIEW,
                DIGEST_B,
                (),
                ("return-for-correction:target-1", "revise-item:target"),
            ),
            (
                "paused-live-dependencies",
                work_models.WorkState.PAUSED,
                work_models.AttemptState.PAUSED,
                DIGEST_A,
                (ItemId("dependency"),),
                ("revise-item:target", "rebind-attempt:target-1", "close:target"),
            ),
            (
                "paused-clear",
                work_models.WorkState.PAUSED,
                work_models.AttemptState.PAUSED,
                DIGEST_A,
                (),
                ("revise-item:target", "rebind-attempt:target-1", "resume:target", "close:target"),
            ),
            (
                "blocked-live-dependencies",
                work_models.WorkState.BLOCKED,
                work_models.AttemptState.BLOCKED,
                DIGEST_A,
                (ItemId("dependency"),),
                ("revise-item:target", "close:target"),
            ),
            (
                "blocked-clear",
                work_models.WorkState.BLOCKED,
                work_models.AttemptState.BLOCKED,
                DIGEST_A,
                (),
                ("revise-item:target", "resume:target", "close:target"),
            ),
        )
        for name, item_state, attempt_state, accepted_digest, live_dependencies, expected in cases:
            with self.subTest(name=name):
                revision_index = expected.index("revise-item:target")
                expected = (
                    *expected[:revision_index],
                    "record-replacement:target",
                    *expected[revision_index:],
                )
                target = replace_dataclass(
                    item("target", item_state, attempt="target-1"),
                    depends_on=live_dependencies,
                )
                attempt = AttemptRecord(
                    "target-1",
                    "target",
                    attempt_state,
                    accepted_scope_revision=1,
                    accepted_scope_digest=accepted_digest,
                    protected_candidate_revision="candidate-a"
                    if attempt_state == work_models.AttemptState.REVIEW
                    else None,
                )
                snapshot = LedgerSnapshot(
                    "revision",
                    (target, dependency) if live_dependencies else (target,),
                    attempts=(attempt,),
                    definitions=(current_definition,),
                    subject_revisions=(
                        work_models.SubjectRevision(ItemId("target"), "1"),
                        work_models.SubjectRevision(AttemptId("target-1"), "1"),
                        *((work_models.SubjectRevision(ItemId("dependency"), "1"),) if live_dependencies else ()),
                    ),
                )
                global_actions = available_actions(snapshot, actor)
                selected_global = tuple(
                    decision_models.action_id(action)
                    for action in global_actions
                    if action.capability.subject in {ItemId("target"), AttemptId("target-1")}
                )
                groups = project_attempt_action_groups(
                    work_models.ProjectAttemptActionContext(
                        ItemId("target"),
                        "1",
                        item_state,
                        AttemptId("target-1"),
                        "1",
                        attempt,
                        1,
                        DIGEST_A,
                        live_dependencies,
                        True,
                        None,
                        True,
                    ),
                    factory,
                )
                selected_exact = tuple(
                    decision_models.action_id(action) for action in (*groups.attempt_actions, *groups.item_actions)
                )
                self.assertEqual(expected, selected_global)
                self.assertEqual(expected, selected_exact)

    def test_every_action_kind_has_one_complete_domain_semantics_descriptor(self) -> None:
        descriptors = tuple(decision_models.action_semantics(kind) for kind in decision_models.ActionKind)

        self.assertEqual(len(decision_models.ActionKind), len(descriptors))
        for kind, descriptor in zip(decision_models.ActionKind, descriptors, strict=True):
            with self.subTest(kind=kind):
                self.assertTrue(descriptor.use_case)
                self.assertTrue(descriptor.permitted_roles)
                self.assertTrue(descriptor.practical_result)
        self.assertEqual(
            (decision_models.Role.PROJECT, decision_models.Role.WORKER),
            decision_models.action_semantics(decision_models.ActionKind.CONTINUE).permitted_roles,
        )
        advisory = decision_models.action_semantics(decision_models.ActionKind.CONTINUE)
        review_acceptance = decision_models.action_semantics(decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE)
        self.assertEqual(decision_models.LifecycleEffect.NO_LIFECYCLE_CHANGE, advisory.lifecycle_effect)
        self.assertEqual(decision_models.ActionLifecyclePrecondition.ACTIVE_ATTEMPT, advisory.lifecycle_precondition)
        self.assertEqual(decision_models.LifecycleEffect.CHANGES_LIFECYCLE, review_acceptance.lifecycle_effect)
        self.assertEqual(
            decision_models.ActionLifecyclePrecondition.REVIEW_ATTEMPT,
            review_acceptance.lifecycle_precondition,
        )

    def test_review_continuation_is_a_project_action_and_requires_the_protected_candidate(self) -> None:
        review = item("target", work_models.WorkState.REVIEW, attempt="target-1")
        attempt = AttemptRecord(
            "target-1", "target", work_models.AttemptState.REVIEW, protected_candidate_revision="candidate-a"
        )
        authority = work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("worker-lease"), 3)
        snapshot = LedgerSnapshot(
            "revision",
            (review,),
            attempts=(attempt,),
            attempt_authorities=(authority,),
        )

        project_actions = available_actions(
            snapshot,
            decision_models.ActorAuthority(decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0),
        )
        selected = next(
            value for value in project_actions if value.kind == decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE
        )
        assert isinstance(selected, decision_models.AcceptReviewAndContinueAction)

        mismatch = decision_outcome(
            snapshot,
            decision_models.AcceptReviewAndContinueCommand(
                selected, work_models.AcceptReviewAndContinueInput(CandidateId("candidate-b"), "accepted")
            ),
            NOW,
        )
        self.assertIsInstance(mismatch, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, mismatch.code)

        accepted = decide(
            snapshot,
            decision_models.AcceptReviewAndContinueCommand(
                selected, work_models.AcceptReviewAndContinueInput(CandidateId("candidate-a"), "accepted")
            ),
            NOW,
        )
        self.assertIsInstance(accepted.change, decision_models.ReviewAcceptanceChange)
        assert isinstance(accepted.change, decision_models.ReviewAcceptanceChange)
        self.assertEqual(CandidateId("candidate-a"), accepted.change.candidate)
        self.assertEqual(4, accepted.change.authority_change.after.generation)
        self.assertIsNone(accepted.change.authority_change.after.lease_id)
        self.assertEqual("accepted", accepted.receipt.evidence)

        for retained_authorities in ((), (authority, authority)):
            with self.subTest(retained_authorities=len(retained_authorities)):
                rejected = decision_outcome(
                    replace_dataclass(snapshot, attempt_authorities=retained_authorities),
                    decision_models.AcceptReviewAndContinueCommand(
                        selected,
                        work_models.AcceptReviewAndContinueInput(CandidateId("candidate-a"), "accepted"),
                    ),
                    NOW,
                )
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED, rejected.code)

        inconsistent = replace_dataclass(
            snapshot,
            attempts=(replace_dataclass(attempt, state=work_models.AttemptState.ACTIVE),),
        )
        inconsistent_kinds = {
            value.kind
            for value in available_actions(
                inconsistent,
                decision_models.ActorAuthority(
                    decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 1
                ),
            )
        }
        self.assertNotIn(decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, inconsistent_kinds)

    def test_checkpoint_acceptance_rejects_a_candidate_other_than_the_protected_one(self) -> None:
        review = item("target", work_models.WorkState.REVIEW, attempt="target-1")
        attempt = AttemptRecord(
            "target-1", "target", work_models.AttemptState.REVIEW, protected_candidate_revision="candidate-a"
        )
        authority = work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("worker-lease"), 3)
        snapshot = LedgerSnapshot(
            "revision",
            (review,),
            attempts=(attempt,),
            attempt_authorities=(authority,),
        )

        mismatch = decision_outcome(
            snapshot,
            decision_models.AcceptCheckpointCommand(
                action(decision_models.AcceptCheckpointAction, AttemptId("target-1")),
                work_models.AcceptCheckpointInput(CheckpointId("checkpoint-a"), CandidateId("candidate-b"), "accepted"),
            ),
            NOW,
        )
        self.assertIsInstance(mismatch, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, mismatch.code)

    def test_rebind_preserves_active_or_paused_attempt_and_fences_authority(self) -> None:
        authority = work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("worker-lease"), 3)
        definition = definition_anchor("target", 1, DIGEST_A)
        brief = work_models.ArtifactRecord(ArtifactRefId(7), work_models.ArtifactKind.BRIEF)

        for state, attempt_state in (
            (work_models.WorkState.ACTIVE, work_models.AttemptState.ACTIVE),
            (work_models.WorkState.PAUSED, work_models.AttemptState.PAUSED),
        ):
            with self.subTest(state=state):
                snapshot = LedgerSnapshot(
                    "revision",
                    (item("target", state, attempt="target-1"),),
                    attempts=(AttemptRecord("target-1", "target", attempt_state, 1, DIGEST_A),),
                    artifacts=(brief,),
                    definitions=(definition,),
                    attempt_authorities=(authority,),
                )
                selected = next(
                    value
                    for value in available_actions(
                        snapshot,
                        decision_models.ActorAuthority(
                            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
                        ),
                    )
                    if value.kind == decision_models.ActionKind.REBIND_ATTEMPT
                )
                assert isinstance(selected, decision_models.RebindAttemptAction)

                accepted = decide(
                    snapshot,
                    decision_models.RebindAttemptCommand(
                        selected,
                        work_models.RebindAttemptInput(
                            AttemptId("target-1"), "codex/corrected", "correct-base", ArtifactRefId(7)
                        ),
                    ),
                    NOW,
                )

                self.assertIsInstance(accepted.change, decision_models.RebindAttemptChange)
                assert isinstance(accepted.change, decision_models.RebindAttemptChange)
                self.assertEqual(attempt_state, accepted.change.attempt_state)
                self.assertEqual(
                    (1, DIGEST_A), (accepted.change.accepted_scope_revision, accepted.change.accepted_scope_digest)
                )
                self.assertEqual(4, accepted.change.authority_change.after.generation)
                self.assertIsNone(accepted.change.authority_change.after.lease_id)

    def test_definition_stale_active_or_paused_attempt_advertises_rebind_to_current_scope(self) -> None:
        authority = work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("worker-lease"), 3)
        brief = work_models.ArtifactRecord(ArtifactRefId(7), work_models.ArtifactKind.BRIEF)
        prerequisite = item("prerequisite", work_models.WorkState.READY)

        for state, attempt_state in (
            (work_models.WorkState.ACTIVE, work_models.AttemptState.ACTIVE),
            (work_models.WorkState.PAUSED, work_models.AttemptState.PAUSED),
        ):
            with self.subTest(state=state):
                target = replace_dataclass(
                    item("target", state, attempt="target-1"),
                    depends_on=(ItemId("prerequisite"),),
                )
                snapshot = LedgerSnapshot(
                    "revision",
                    (target, prerequisite),
                    attempts=(AttemptRecord("target-1", "target", attempt_state, 1, DIGEST_A),),
                    artifacts=(brief,),
                    definitions=(definition_anchor("target", 2, DIGEST_B, target.depends_on),),
                    attempt_authorities=(authority,),
                )
                advertised = available_actions(
                    snapshot,
                    decision_models.ActorAuthority(
                        decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
                    ),
                )
                selected = next(
                    value for value in advertised if value.kind == decision_models.ActionKind.REBIND_ATTEMPT
                )
                self.assertNotIn(
                    decision_models.ActionKind.RESUME,
                    {value.kind for value in advertised},
                )
                assert isinstance(selected, decision_models.RebindAttemptAction)

                accepted = decide(
                    snapshot,
                    decision_models.RebindAttemptCommand(
                        selected,
                        work_models.RebindAttemptInput(
                            AttemptId("target-1"), "codex/corrected", "correct-base", ArtifactRefId(7)
                        ),
                    ),
                    NOW,
                )

                self.assertIsInstance(accepted.change, decision_models.RebindAttemptChange)
                assert isinstance(accepted.change, decision_models.RebindAttemptChange)
                self.assertEqual(
                    (2, DIGEST_B),
                    (accepted.change.accepted_scope_revision, accepted.change.accepted_scope_digest),
                )

    def test_rebind_rejects_unsupported_attempts_and_requires_exact_authority(self) -> None:
        definition = definition_anchor("target", 1, DIGEST_A)
        brief = work_models.ArtifactRecord(ArtifactRefId(7), work_models.ArtifactKind.BRIEF)
        authority = work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("lease"), 2)
        command = decision_models.RebindAttemptCommand(
            action(decision_models.RebindAttemptAction, AttemptId("target-1")),
            work_models.RebindAttemptInput(AttemptId("target-1"), "codex/corrected", "correct-base", ArtifactRefId(7)),
        )
        cases = (
            (
                LedgerSnapshot(
                    "r",
                    (item("target", work_models.WorkState.BLOCKED, attempt="target-1"),),
                    attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.BLOCKED, 1, DIGEST_A),),
                    artifacts=(brief,),
                    definitions=(definition,),
                    attempt_authorities=(authority,),
                ),
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
            ),
            (
                LedgerSnapshot(
                    "r",
                    (item("target", work_models.WorkState.REVIEW, attempt="target-1"),),
                    attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.REVIEW, 1, DIGEST_A),),
                    artifacts=(brief,),
                    definitions=(definition,),
                    attempt_authorities=(authority,),
                ),
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
            ),
            (
                LedgerSnapshot(
                    "r",
                    (),
                    attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.DONE, 1, DIGEST_A),),
                    artifacts=(brief,),
                    definitions=(definition,),
                    attempt_authorities=(authority,),
                ),
                DecisionFailureCode.ATTEMPT_NOT_FOUND,
            ),
            (
                LedgerSnapshot(
                    "r",
                    (item("target", work_models.WorkState.ACTIVE, attempt="target-1"),),
                    attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.ACTIVE, 1, DIGEST_A),),
                    artifacts=(brief,),
                    definitions=(definition,),
                ),
                DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED,
            ),
        )
        for snapshot, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                advertised = available_actions(
                    snapshot,
                    decision_models.ActorAuthority(
                        decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
                    ),
                )
                if expected_code != DecisionFailureCode.ATTEMPT_AUTHORITY_REQUIRED:
                    self.assertNotIn(
                        decision_models.ActionKind.REBIND_ATTEMPT,
                        {candidate.kind for candidate in advertised},
                    )
                rejected = decision_outcome(snapshot, command, NOW)
                self.assertIsInstance(rejected, DecisionFailure)
                assert isinstance(rejected, DecisionFailure)
                self.assertEqual(expected_code, rejected.code)

    def test_paused_current_attempt_with_live_dependency_can_rebind_without_resume(self) -> None:
        dependency = ItemId("prerequisite")
        paused = replace(
            item("target", work_models.WorkState.PAUSED, attempt="target-1"),
            depends_on=(dependency,),
        )
        snapshot = LedgerSnapshot(
            "revision",
            (paused, item("prerequisite", work_models.WorkState.READY)),
            attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.PAUSED, 1, DIGEST_A),),
            definitions=(definition_anchor("target", 1, DIGEST_A, (dependency,)),),
        )

        actions = available_actions(
            snapshot,
            decision_models.ActorAuthority(
                decision_models.Role.PROJECT,
                decision_models.AuthorizationKind.PROJECT,
                1,
            ),
        )

        self.assertTrue(any(isinstance(value, decision_models.RebindAttemptAction) for value in actions))
        self.assertFalse(any(isinstance(value, decision_models.ResumeAction) for value in actions))

    def test_blocker_actions_expose_distinct_roles_subjects_preconditions_and_effects(self) -> None:
        active = replace(
            item("target", work_models.WorkState.ACTIVE, attempt="target-1"),
            depends_on=(ItemId("prerequisite"),),
        )
        intake = item("unstarted", work_models.WorkState.INTAKE)
        prerequisite = item("prerequisite", work_models.WorkState.READY)
        snapshot = LedgerSnapshot(
            "revision",
            (active, intake, prerequisite),
            attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.ACTIVE),),
            definitions=(definition_anchor("target", 1, DIGEST_A, (ItemId("prerequisite"),)),),
            attempt_authorities=(
                work_models.AttemptAuthority(AttemptId("target-1"), ItemId("target"), LeaseId("worker-lease"), 4),
            ),
        )
        project = available_actions(
            snapshot,
            decision_models.ActorAuthority(decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 1),
        )
        worker = available_actions(
            snapshot,
            decision_models.ActorAuthority(
                decision_models.Role.WORKER,
                decision_models.AuthorizationKind.ATTEMPT,
                4,
                LeaseId("worker-lease"),
                (AttemptId("target-1"),),
            ),
        )
        selected = {
            action.kind: action
            for action in (*project, *worker)
            if action.kind
            in {
                decision_models.ActionKind.REPORT_BLOCKER,
                decision_models.ActionKind.BLOCK,
                decision_models.ActionKind.BLOCK_ITEM,
            }
        }

        expected = {
            decision_models.ActionKind.REPORT_BLOCKER: (
                "report-blocker:target-1",
                "Prepare blocker report for target",
                (
                    "Preserve blocker evidence for the project.",
                    "advisory",
                    ("worker",),
                    "attempt",
                    "active-attempt",
                    "Prepare a blocker report without changing shared lifecycle state.",
                ),
            ),
            decision_models.ActionKind.BLOCK: (
                "block:target-1",
                "Block active attempt for target",
                (
                    "Stop an active attempt on dependencies already accepted in its definition.",
                    "mutating",
                    ("project",),
                    "attempt",
                    "active-attempt",
                    "Move the item and attempt to blocked without changing accepted dependencies.",
                ),
            ),
            decision_models.ActionKind.BLOCK_ITEM: (
                "block-item:unstarted",
                "Block unstarted work item unstarted",
                (
                    "Stop unstarted intake work on dependencies already accepted in its definition.",
                    "mutating",
                    ("project",),
                    "item",
                    "intake-item",
                    "Move the item to blocked without changing accepted dependencies or creating an attempt.",
                ),
            ),
        }
        self.assertEqual(set(expected), set(selected))
        for kind, (expected_action_id, label, semantics) in expected.items():
            with self.subTest(kind=kind):
                action = selected[kind]
                descriptor = decision_models.action_semantics(action.kind)
                self.assertEqual(expected_action_id, decision_models.action_id(action))
                self.assertEqual(label, action.capability.label)
                self.assertEqual(
                    semantics,
                    (
                        descriptor.use_case,
                        descriptor.lifecycle_effect.value,
                        tuple(role.value for role in descriptor.permitted_roles),
                        descriptor.subject_kind.value,
                        descriptor.lifecycle_precondition.value,
                        descriptor.practical_result,
                    ),
                )

        proposal_semantics = decision_models.action_semantics(decision_models.ActionKind.ACCEPT_PROPOSAL)
        self.assertEqual(
            decision_models.ActionLifecyclePrecondition.INTAKE_PROPOSAL,
            proposal_semantics.lifecycle_precondition,
        )
        self.assertEqual("intake-proposal", proposal_semantics.lifecycle_precondition.value)

        block_action = selected[decision_models.ActionKind.BLOCK]
        assert isinstance(block_action, decision_models.BlockAttemptAction)
        blocked = decide(
            snapshot,
            decision_models.BlockCommand(
                block_action,
                work_models.BlockInput("Waiting for prerequisite.", (ItemId("prerequisite"),)),
            ),
            NOW,
        )
        self.assertIsInstance(blocked.change, decision_models.BlockAttemptChange)
        assert isinstance(blocked.change, decision_models.BlockAttemptChange)
        self.assertEqual((ItemId("prerequisite"),), blocked.change.dependencies_after)
        rejected_dependency = decision_outcome(
            snapshot,
            decision_models.BlockCommand(
                block_action,
                work_models.BlockInput("Waiting for an unaccepted prerequisite.", (ItemId("unstarted"),)),
            ),
            NOW,
        )
        self.assertIsInstance(rejected_dependency, DecisionFailure)
        assert isinstance(rejected_dependency, DecisionFailure)
        self.assertEqual(DecisionFailureCode.DEPENDENCY_NOT_SATISFIED, rejected_dependency.code)

    def test_resume_and_reopen_actions_name_their_distinct_contextual_results(self) -> None:
        snapshot = LedgerSnapshot(
            "revision",
            (
                item("paused-attempt", work_models.WorkState.PAUSED, attempt="paused-attempt-1"),
                item("blocked-unstarted", work_models.WorkState.BLOCKED),
                item("deferred", work_models.WorkState.DEFERRED),
            ),
            attempts=(AttemptRecord("paused-attempt-1", "paused-attempt", work_models.AttemptState.PAUSED),),
        )

        actions = {
            decision_models.action_id(value): value
            for value in available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.PROJECT,
                    decision_models.AuthorizationKind.PROJECT,
                    1,
                ),
            )
        }

        self.assertEqual("Return paused-attempt to active", actions[ActionId("resume:paused-attempt")].capability.label)
        self.assertEqual(
            "Return blocked-unstarted to ready", actions[ActionId("resume:blocked-unstarted")].capability.label
        )
        self.assertEqual("Reopen deferred for intake", actions[ActionId("reopen:deferred")].capability.label)
        self.assertEqual(
            "Return paused or blocked work to active when an attempt exists, otherwise ready.",
            decision_models.action_semantics(decision_models.ActionKind.RESUME).practical_result,
        )
        self.assertEqual(
            "Return deferred work to intake.",
            decision_models.action_semantics(decision_models.ActionKind.REOPEN).practical_result,
        )

    def test_missing_attempt_is_a_returned_failure(self) -> None:
        snapshot = LedgerSnapshot("revision", ())
        command = decision_models.PauseCommand(
            action(decision_models.PauseAction, AttemptId("missing-attempt")), work_models.ReasonInput("pause")
        )

        outcome = decision_outcome(snapshot, command, NOW)

        self.assertEqual(
            DecisionFailure(DecisionFailureCode.ATTEMPT_NOT_FOUND, "Attempt 'missing-attempt' does not exist.", None),
            outcome,
        )

    def test_terminal_decisions_require_and_return_outcome_evidence(self) -> None:
        active = item("target", work_models.WorkState.REVIEW, attempt="target-1")
        snapshot = LedgerSnapshot(
            "revision",
            (active,),
            attempts=(AttemptRecord("target-1", "target", work_models.AttemptState.REVIEW),),
        )

        completed_action = action(decision_models.CompleteAction, AttemptId("target-1"))
        completed = decide(
            snapshot,
            decision_models.CompleteCommand(completed_action, work_models.EvidenceInput("review accepted")),
            NOW,
        )
        self.assertEqual("review accepted", completed.receipt.evidence)
        self.assertIsInstance(completed.change, decision_models.CompletionChange)
        assert isinstance(completed.change, decision_models.CompletionChange)
        self.assertEqual("review accepted", completed.change.evidence)

        intake = LedgerSnapshot("revision", (item("obsolete", work_models.WorkState.INTAKE),))
        closed = decide(
            intake,
            decision_models.CloseCommand(
                action(decision_models.CloseAction, ItemId("obsolete")),
                work_models.CloseInput(work_models.CloseOutcome.DROPPED, "no longer needed"),
            ),
            NOW,
        )
        self.assertEqual(("dropped", "no longer needed"), (closed.receipt.outcome, closed.receipt.evidence))

    def test_completion_leaf_is_selected_by_authoritative_checkpoint_history(self) -> None:
        active = item("target", work_models.WorkState.ACTIVE, attempt="target-1")
        attempt = AttemptRecord("target-1", "target", work_models.AttemptState.ACTIVE)
        complete = action(decision_models.CompleteAction, AttemptId("target-1"))
        covered_value = work_models.CoveredCompleteInput(
            CandidateId("candidate"),
            "all evidence covered",
            TaskId("reviewer"),
            "a" * 64,
            "b" * 64,
            (
                work_models.CoveredCompletionPackageInput(
                    HistoryId(7), "c" * 64, work_models.CompletionPackageDisposition.REVALIDATED, "rechecked"
                ),
            ),
        )

        direct_with_history = decision_outcome(
            LedgerSnapshot("revision", (active,), attempts=(attempt,), checkpoint_history_ids=(HistoryId(7),)),
            decision_models.CompleteCommand(complete, work_models.EvidenceInput("done")),
            NOW,
        )
        covered_without_history = decision_outcome(
            LedgerSnapshot("revision", (active,), attempts=(attempt,)),
            decision_models.CoveredCompleteCommand(complete, covered_value),
            NOW,
        )

        self.assertIsInstance(direct_with_history, DecisionFailure)
        self.assertIsInstance(covered_without_history, DecisionFailure)

    def test_changed_semantic_scope_blocks_the_next_attempt_boundary(self) -> None:
        current = definition_anchor("build-map", 2, DIGEST_B)
        active = item("build-map", work_models.WorkState.ACTIVE, attempt="build-map-1")
        snapshot = LedgerSnapshot(
            "revision",
            (active,),
            attempts=(AttemptRecord("build-map-1", "build-map", work_models.AttemptState.ACTIVE, 1, DIGEST_A),),
            definitions=(current,),
        )

        action_ids = {
            decision_models.action_id(value)
            for value in available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 1
                ),
            )
        }
        self.assertNotIn("continue:build-map-1", action_ids)
        self.assertNotIn("dispatch:build-map-1", action_ids)
        self.assertNotIn("complete:build-map-1", action_ids)
        submit = action(decision_models.SubmitReviewAction, AttemptId("build-map-1"))
        command = decision_models.SubmitReviewCommand(submit, work_models.SubmitReviewInput(CandidateId("candidate")))
        rejected = decision_outcome(snapshot, command, NOW)
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_DEFINITION_STALE, rejected.code)

    def test_transition_rejections_preserve_the_domain_boundary(self) -> None:
        ready = item("target", work_models.WorkState.READY)
        active = item("target", work_models.WorkState.ACTIVE, attempt="target-1")
        paused = item("target", work_models.WorkState.PAUSED, attempt="target-1")
        review = item("target", work_models.WorkState.REVIEW, attempt="target-1")
        attempt_active = AttemptRecord("target-1", "target", work_models.AttemptState.ACTIVE)
        attempt_review = AttemptRecord("target-1", "target", work_models.AttemptState.REVIEW)
        stale_definition = definition_anchor("target", 2, DIGEST_B)
        cases: tuple[tuple[LedgerSnapshot, decision_models.TransitionCommand, str], ...] = (
            (
                LedgerSnapshot("r", ()),
                decision_models.ActivateCommand(
                    action(decision_models.ActivateAction, ItemId("missing")),
                    work_models.ActivateInput(AttemptId("missing-1"), "branch", "base", "owner", ArtifactRefId(1)),
                ),
                "ITEM_NOT_FOUND",
            ),
            (
                LedgerSnapshot("r", (item("target", work_models.WorkState.INTAKE),)),
                decision_models.ActivateCommand(
                    action(decision_models.ActivateAction, ItemId("target")),
                    work_models.ActivateInput(AttemptId("target-1"), "branch", "base", "owner", ArtifactRefId(1)),
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", (ready,)),
                decision_models.PauseCommand(
                    action(decision_models.PauseAction, AttemptId("missing-1")), work_models.ReasonInput("pause")
                ),
                "ATTEMPT_NOT_FOUND",
            ),
            (
                LedgerSnapshot("r", (review,), attempts=(attempt_review,)),
                decision_models.PauseCommand(
                    action(decision_models.PauseAction, AttemptId("target-1")), work_models.ReasonInput("pause")
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", (paused,)),
                decision_models.CompleteCommand(
                    action(decision_models.CompleteAction, AttemptId("target-1")), work_models.EvidenceInput("done")
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot(
                    "r",
                    (active,),
                    attempts=(replace(attempt_active, accepted_scope_revision=1, accepted_scope_digest=DIGEST_A),),
                    definitions=(stale_definition,),
                ),
                decision_models.CompleteCommand(
                    action(decision_models.CompleteAction, AttemptId("target-1")), work_models.EvidenceInput("done")
                ),
                "ITEM_DEFINITION_STALE",
            ),
            (
                LedgerSnapshot("r", (review,), attempts=(attempt_review,), history_items=(ItemId("target"),)),
                decision_models.CompleteCommand(
                    action(decision_models.CompleteAction, AttemptId("target-1")), work_models.EvidenceInput("done")
                ),
                "HISTORY_RECORD_EXISTS",
            ),
            (
                LedgerSnapshot("r", (active,), attempts=(attempt_active,)),
                decision_models.CloseCommand(
                    action(decision_models.CloseAction, ItemId("target")),
                    work_models.CloseInput(work_models.CloseOutcome.DONE, "done"),
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot(
                    "r", (ready, replace(item("dependent", work_models.WorkState.READY), depends_on=("target",)))
                ),
                decision_models.CloseCommand(
                    action(decision_models.CloseAction, ItemId("target")),
                    work_models.CloseInput(work_models.CloseOutcome.DROPPED, "obsolete"),
                ),
                "LIVE_DEPENDENTS",
            ),
            (
                LedgerSnapshot("r", (ready,), history_items=(ItemId("target"),)),
                decision_models.CloseCommand(
                    action(decision_models.CloseAction, ItemId("target")),
                    work_models.CloseInput(work_models.CloseOutcome.DONE, "done"),
                ),
                "HISTORY_RECORD_EXISTS",
            ),
            (
                LedgerSnapshot("r", (ready,)),
                decision_models.ResumeCommand(
                    action(decision_models.ResumeAction, ItemId("target")), work_models.ResumeInput()
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot(
                    "r", (replace(paused, depends_on=("source",)), item("source", work_models.WorkState.READY))
                ),
                decision_models.ResumeCommand(
                    action(decision_models.ResumeAction, ItemId("target")), work_models.ResumeInput()
                ),
                "DEPENDENCY_NOT_SATISFIED",
            ),
            (
                LedgerSnapshot("r", (review,), attempts=(attempt_review,)),
                decision_models.SubmitReviewCommand(
                    action(decision_models.SubmitReviewAction, AttemptId("target-1")),
                    work_models.SubmitReviewInput(CandidateId("candidate")),
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot(
                    "r",
                    (active,),
                    attempts=(replace(attempt_active, accepted_scope_revision=1, accepted_scope_digest=DIGEST_A),),
                    definitions=(stale_definition,),
                ),
                decision_models.SubmitReviewCommand(
                    action(decision_models.SubmitReviewAction, AttemptId("target-1")),
                    work_models.SubmitReviewInput(CandidateId("candidate")),
                ),
                "ITEM_DEFINITION_STALE",
            ),
            (
                LedgerSnapshot("r", (active,)),
                decision_models.BlockItemCommand(
                    action(decision_models.BlockItemAction, ItemId("target")), work_models.BlockInput("blocked")
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", (item("target", work_models.WorkState.INTAKE),)),
                decision_models.ReopenCommand(
                    action(decision_models.ReopenAction, ItemId("target")), work_models.EvidenceInput("reopen")
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", (active,)),
                decision_models.DeferCommand(
                    action(decision_models.DeferAction, ItemId("target")), DeferInput("safe-to-defer", "later")
                ),
                "ACTION_NOT_AVAILABLE",
            ),
            (
                LedgerSnapshot("r", ()),
                decision_models.AcceptProposalCommand(
                    action(decision_models.AcceptProposalAction, ProposalId("proposal")),
                    AcceptProposalInput(
                        item="new-item", state=work_models.AcceptedProposalState.READY, next_action="start"
                    ),
                ),
                "PROPOSAL_NOT_FOUND",
            ),
            (
                LedgerSnapshot(
                    "r",
                    (ready, item("proposal", work_models.WorkState.INTAKE)),
                    proposals=(ProposalRecord("proposal", "p1"),),
                ),
                decision_models.AcceptProposalCommand(
                    action(decision_models.AcceptProposalAction, ProposalId("proposal")),
                    AcceptProposalInput(
                        item="target", state=work_models.AcceptedProposalState.READY, next_action="start"
                    ),
                ),
                "TRANSITION_INPUT_INVALID",
            ),
        )
        for snapshot, command, code in cases:
            with self.subTest(kind=command.action.kind.value, code=code):
                rejected = decision_outcome(snapshot, command, NOW)
                self.assertIsInstance(rejected, DecisionFailure)
                self.assertEqual(DecisionFailureCode(code), rejected.code)

    def test_proposal_rejections_use_intake_vocabulary(self) -> None:
        proposal = ProposalRecord("proposal", "p1")
        missing_item = LedgerSnapshot("r", (), proposals=(proposal,))
        cases: tuple[tuple[decision_models.TransitionCommand, str], ...] = (
            (
                decision_models.AcceptProposalCommand(
                    action(decision_models.AcceptProposalAction, ProposalId("proposal")),
                    AcceptProposalInput(
                        item="proposal",
                        state=work_models.AcceptedProposalState.READY,
                        next_action="start",
                    ),
                ),
                "Only a current intake proposal can be accepted.",
            ),
            (
                decision_models.MergeProposalCommand(
                    action(decision_models.MergeProposalAction, ProposalId("proposal")),
                    work_models.MergeProposalInput(ItemId("target")),
                ),
                "Only a current intake proposal can be merged.",
            ),
            (
                decision_models.RejectProposalCommand(
                    action(decision_models.RejectProposalAction, ProposalId("proposal")),
                    work_models.ReasonInput("obsolete"),
                ),
                "Only a current intake proposal can be returned or rejected.",
            ),
        )
        for command, message in cases:
            with self.subTest(kind=command.action.kind.value):
                rejected = decision_outcome(missing_item, command, NOW)
                self.assertIsInstance(rejected, DecisionFailure)
                assert isinstance(rejected, DecisionFailure)
                self.assertEqual(message, rejected.message)

        mismatched_identity = LedgerSnapshot(
            "r",
            (item("proposal", work_models.WorkState.INTAKE),),
            proposals=(proposal,),
        )
        rejected = decision_outcome(
            mismatched_identity,
            decision_models.AcceptProposalCommand(
                action(decision_models.AcceptProposalAction, ProposalId("proposal")),
                AcceptProposalInput(
                    item="other-item",
                    state=work_models.AcceptedProposalState.READY,
                    next_action="start",
                ),
            ),
            NOW,
        )
        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual("An intake proposal must be accepted with its same work-item identity.", rejected.message)


if __name__ == "__main__":
    unittest.main()
