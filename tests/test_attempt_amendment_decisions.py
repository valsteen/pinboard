"""Attempt amendments are legal only for the project on live current-scope attempts, and decide their effects."""

import unittest
from dataclasses import replace

import msgspec

from pinboard.application import action_models, amendments, stored_state
from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    ArtifactRefId,
    AttemptId,
    CandidateId,
    HistoryId,
    HostId,
    LeaseId,
    TaskId,
    WorkItemId,
)
from pinboard.domain.ledger import LedgerSnapshot
from tests.test_decisions import DIGEST_A, DIGEST_B, NOW, available_actions, decide, definition_anchor, item

PROJECT = decision_models.ActorAuthority(decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0)
BRIEF = ArtifactRefId(7)
CANDIDATE = CandidateId("c" * 40)
AUTHORITY = work_models.AttemptAuthority(AttemptId("target-1"), WorkItemId("target"), LeaseId("worker-lease"), 3)


def snapshot(
    state: work_models.WorkState,
    attempt_state: work_models.AttemptState,
    *,
    scope_digest: str = DIGEST_A,
    planned_replacements: tuple[work_models.PlannedReplacement, ...] = (),
    replacement_dispositions: tuple[work_models.ReplacementDisposition, ...] = (),
) -> LedgerSnapshot:
    return LedgerSnapshot(
        "revision",
        (item("target", state, attempt="target-1"), item("replacement", work_models.WorkState.READY)),
        attempts=(
            work_models.AttemptRecord(
                AttemptId("target-1"),
                WorkItemId("target"),
                attempt_state,
                1,
                scope_digest,
                CANDIDATE if attempt_state == work_models.AttemptState.REVIEW else None,
                BRIEF,
                pause_reason=None,
                pending_decision_batch=None,
            ),
        ),
        artifacts=(work_models.ArtifactRecord(BRIEF, work_models.ArtifactKind.BRIEF),),
        definitions=(definition_anchor("target", 1, DIGEST_A), definition_anchor("replacement", 1, DIGEST_A)),
        attempt_authorities=(AUTHORITY,),
        accepted_checkpoints=frozenset(),
        dependency_facts=(),
        dropped_item_ids=frozenset(),
        planned_replacements=planned_replacements,
        replacement_dispositions=replacement_dispositions,
    )


def amendment_action(value: LedgerSnapshot) -> decision_models.RecordAmendmentAction | None:
    return next(
        (
            action
            for action in available_actions(value, PROJECT)
            if isinstance(action, decision_models.RecordAmendmentAction)
        ),
        None,
    )


def amendment_input(
    cleared: work_models.ClearedSubmission | None, carried: work_models.ReviewBase | None = None
) -> work_models.RecordAmendmentInput:
    return work_models.RecordAmendmentInput(
        work_models.AmendmentClass.STALE_DOCUMENTATION,
        "Correct the install command in INSTALL.md.",
        "The documented command no longer exists.",
        "Yes, fix that stale install line too.",
        cleared,
        carried,
    )


class AmendmentLegalityTest(unittest.TestCase):
    def test_project_may_amend_only_active_or_review_attempts(self) -> None:
        cases = (
            (work_models.WorkState.ACTIVE, work_models.AttemptState.ACTIVE, True),
            (work_models.WorkState.REVIEW, work_models.AttemptState.REVIEW, True),
            (work_models.WorkState.PAUSED, work_models.AttemptState.PAUSED, False),
            (work_models.WorkState.BLOCKED, work_models.AttemptState.BLOCKED, False),
        )
        for state, attempt_state, offered in cases:
            with self.subTest(state=state):
                self.assertEqual(offered, amendment_action(snapshot(state, attempt_state)) is not None)

    def test_worker_and_preparer_roles_are_never_offered_an_amendment(self) -> None:
        worker = decision_models.ActorAuthority(
            decision_models.Role.WORKER,
            decision_models.AuthorizationKind.ATTEMPT,
            3,
            LeaseId("worker-lease"),
            (AttemptId("target-1"),),
        )
        value = replace(
            snapshot(work_models.WorkState.ACTIVE, work_models.AttemptState.ACTIVE),
            command_attempt_authorities=(
                work_models.CommandAttemptAuthority(
                    0,
                    WorkItemId("target"),
                    "1",
                    AttemptId("target-1"),
                    "1",
                    TaskId("worker"),
                    HostId("host"),
                    LeaseId("worker-lease"),
                    3,
                    NOW,
                ),
            ),
        )
        kinds = {action.kind for action in available_actions(value, worker)}
        self.assertIn(decision_models.ActionKind.SUBMIT_REVIEW, kinds)
        self.assertNotIn(decision_models.ActionKind.RECORD_AMENDMENT, kinds)
        self.assertNotIn(
            decision_models.Role.PREPARER,
            decision_models.action_semantics(decision_models.ActionKind.RECORD_AMENDMENT).permitted_roles,
        )

    def test_stale_definition_withholds_an_amendment(self) -> None:
        for state, attempt_state in (
            (work_models.WorkState.ACTIVE, work_models.AttemptState.ACTIVE),
            (work_models.WorkState.REVIEW, work_models.AttemptState.REVIEW),
        ):
            with self.subTest(state=state):
                self.assertIsNone(amendment_action(snapshot(state, attempt_state, scope_digest=DIGEST_B)))

    def test_unresolved_planned_replacement_withholds_until_withdrawn_or_retained(self) -> None:
        relation = work_models.PlannedReplacement(
            WorkItemId("target"),
            1,
            WorkItemId("replacement"),
            "Avoid duplicate work.",
            work_models.PlannedReplacementStatus.CURRENT,
            TaskId("human"),
            NOW,
        )
        active = work_models.WorkState.ACTIVE, work_models.AttemptState.ACTIVE
        self.assertIsNone(amendment_action(snapshot(*active, planned_replacements=(relation,))))
        withdrawn = replace(relation, relation_revision=2, status=work_models.PlannedReplacementStatus.WITHDRAWN)
        self.assertIsNotNone(amendment_action(snapshot(*active, planned_replacements=(relation, withdrawn))))
        retained = work_models.ReplacementDisposition(
            WorkItemId("target"), 1, "Keep going for now.", relation.replacement_cost, TaskId("human"), NOW
        )
        self.assertIsNotNone(
            amendment_action(snapshot(*active, planned_replacements=(relation,), replacement_dispositions=(retained,)))
        )


class AmendmentDecisionTest(unittest.TestCase):
    def test_active_amendment_keeps_lifecycle_and_lease_and_binds_the_brief(self) -> None:
        value = snapshot(work_models.WorkState.ACTIVE, work_models.AttemptState.ACTIVE)
        selected = amendment_action(value)
        assert selected is not None
        base = work_models.ReviewBase(CANDIDATE, ArtifactRefId(8), ArtifactRefId(9))

        decision = decide(value, decision_models.RecordAmendmentCommand(selected, amendment_input(None, base)), NOW)

        change = decision.change
        assert isinstance(change, decision_models.AmendmentChange)
        self.assertEqual(work_models.AttemptState.ACTIVE, change.attempt_before)
        self.assertIsNone(change.authority_change)
        self.assertEqual(BRIEF, change.amendment.brief_artifact_ref_id)
        self.assertIsNone(change.amendment.cleared_candidate)
        self.assertEqual(base, change.amendment.review_base)
        self.assertEqual("record-amendment", decision.receipt.outcome)

    def test_review_amendment_fences_authority_and_captures_the_cleared_submission(self) -> None:
        value = snapshot(work_models.WorkState.REVIEW, work_models.AttemptState.REVIEW)
        selected = amendment_action(value)
        assert selected is not None
        carried = work_models.ReviewBase(CandidateId("a" * 40), ArtifactRefId(3), ArtifactRefId(4))
        cases = (
            (
                "own ready review",
                ArtifactRefId(9),
                work_models.ReviewBase(CANDIDATE, ArtifactRefId(8), ArtifactRefId(9)),
            ),
            ("carried earlier review", None, carried),
        )
        for label, ready, expected in cases:
            with self.subTest(label):
                cleared = work_models.ClearedSubmission(CANDIDATE, ArtifactRefId(8), ready)
                decision = decide(
                    value, decision_models.RecordAmendmentCommand(selected, amendment_input(cleared, carried)), NOW
                )
                change = decision.change
                assert isinstance(change, decision_models.AmendmentChange)
                self.assertEqual(work_models.AttemptState.REVIEW, change.attempt_before)
                assert change.authority_change is not None
                self.assertIsNone(change.authority_change.after.lease_id)
                self.assertEqual(4, change.authority_change.after.generation)
                self.assertEqual(CANDIDATE, change.amendment.cleared_candidate)
                self.assertEqual(ArtifactRefId(8), change.amendment.cleared_snapshot_artifact_ref_id)
                self.assertEqual(expected, change.amendment.review_base)

    def test_rejects_stale_observation_empty_text_and_unavailable_lifecycle_unchanged(self) -> None:
        review = snapshot(work_models.WorkState.REVIEW, work_models.AttemptState.REVIEW)
        selected = amendment_action(review)
        assert selected is not None
        other = work_models.ClearedSubmission(CandidateId("d" * 40), ArtifactRefId(8), None)
        blank = replace(amendment_input(work_models.ClearedSubmission(CANDIDATE, ArtifactRefId(8), None)), reason=" ")
        paused = replace(
            review,
            items=(item("target", work_models.WorkState.PAUSED, attempt="target-1"), review.items[1]),
            attempts=(
                replace(review.attempts[0], state=work_models.AttemptState.PAUSED, protected_candidate_revision=None),
            ),
        )
        cases = (
            ("stale observation", review, amendment_input(other), DecisionFailureCode.ACTION_NOT_AVAILABLE),
            ("missing observation", review, amendment_input(None), DecisionFailureCode.ACTION_NOT_AVAILABLE),
            ("blank reason", review, blank, DecisionFailureCode.TRANSITION_INPUT_INVALID),
            ("paused", paused, amendment_input(None), DecisionFailureCode.ACTION_NOT_AVAILABLE),
        )
        for label, value, command_input, code in cases:
            with self.subTest(label):
                failure = decision_outcome(value, decision_models.RecordAmendmentCommand(selected, command_input), NOW)
                assert isinstance(failure, DecisionFailure)
                self.assertEqual(code, failure.code)


class AmendmentReviewBaseTest(unittest.TestCase):
    def recorded(
        self, brief: ArtifactRefId, base: work_models.ReviewBase | None
    ) -> stored_state.StoredAttemptAmendment:
        return stored_state.StoredAttemptAmendment(
            HistoryId(5),
            AttemptId("target-1"),
            2,
            NOW,
            work_models.AttemptAmendment(
                work_models.AmendmentClass.LINT_OR_TOOLING,
                "Fix the lint configuration.",
                "The linter rejects the new file.",
                "Yes, fix the lint setup.",
                brief,
                None if base is None else base.candidate,
                None if base is None else base.snapshot_artifact_ref_id,
                base,
            ),
        )

    def test_delta_base_needs_a_resubmission_with_no_return_rebind_or_acceptance_between(self) -> None:
        base = work_models.ReviewBase(CANDIDATE, ArtifactRefId(8), ArtifactRefId(9))
        current = amendments.current_amendments((self.recorded(BRIEF, base),), BRIEF)
        kind = decision_models.ActionKind
        cases = (
            ((kind.SUBMIT_REVIEW,), base),
            ((), None),
            ((kind.SUBMIT_REVIEW, kind.RETURN_FOR_CORRECTION, kind.SUBMIT_REVIEW), None),
            ((kind.REBIND_ATTEMPT, kind.SUBMIT_REVIEW), None),
            ((kind.SUBMIT_REVIEW, kind.ACCEPT_REVIEW_AND_CONTINUE), None),
            ((kind.SUBMIT_REVIEW, kind.ACCEPT_CHECKPOINT), None),
        )
        for later, expected in cases:
            with self.subTest(later=later):
                self.assertEqual(expected, amendments.delta_review_base(current, later))
        self.assertEqual(base, amendments.carried_review_base(current, ()))

    def test_an_amendment_under_another_brief_is_not_current_and_carries_nothing(self) -> None:
        base = work_models.ReviewBase(CANDIDATE, ArtifactRefId(8), ArtifactRefId(9))
        current = amendments.current_amendments((self.recorded(ArtifactRefId(6), base),), BRIEF)
        self.assertEqual((), current)
        self.assertIsNone(amendments.delta_review_base(current, (decision_models.ActionKind.SUBMIT_REVIEW,)))


class AmendmentPayloadTest(unittest.TestCase):
    def test_payload_accepts_only_the_closed_classes(self) -> None:
        payload = {
            "schema": "pinboard-attempt-amendment/v1",
            "change": "Rename the helper.",
            "reason": "Its name is misleading.",
            "human_decision": "Go ahead and rename it.",
        }
        for amendment_class in work_models.AmendmentClass:
            with self.subTest(amendment_class=amendment_class):
                decoded = msgspec.json.decode(
                    msgspec.json.encode(payload | {"amendment_class": amendment_class.value}),
                    type=action_models.RecordAmendmentInputPayload,
                )
                self.assertIs(amendment_class, decoded.amendment_class)
        for invalid in ("new-capability", "", "Stale-Documentation"):
            with self.subTest(invalid=invalid), self.assertRaises(msgspec.ValidationError):
                msgspec.json.decode(
                    msgspec.json.encode(payload | {"amendment_class": invalid}),
                    type=action_models.RecordAmendmentInputPayload,
                )
        with self.assertRaises(msgspec.ValidationError):
            msgspec.json.decode(
                msgspec.json.encode(payload | {"amendment_class": "comment-or-name", "change": ""}),
                type=action_models.RecordAmendmentInputPayload,
            )


if __name__ == "__main__":
    unittest.main()
