import unittest
from dataclasses import replace
from datetime import UTC, datetime

from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import available_actions, decide
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import AttemptId, ItemId, LeaseId, TaskId
from pinboard.domain.ledger import LedgerSnapshot
from tests.domain_support import action, expect_success

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def work_item(item_id: str, state: work_models.WorkState, attempt: str | None = None) -> work_models.WorkItem:
    return work_models.WorkItem(
        ItemId(item_id), state, None, (), AttemptId(attempt) if attempt else None, None, None, None, 1
    )


def relation(
    revision: int = 1, *, status: work_models.PlannedReplacementStatus = work_models.PlannedReplacementStatus.CURRENT
) -> work_models.PlannedReplacement:
    return work_models.PlannedReplacement(
        ItemId("old-work"),
        revision,
        ItemId("new-work"),
        "Doing old-work first spends implementation and review time on code new-work will replace.",
        status,
        TaskId("planner"),
        NOW,
    )


class PlannedReplacementTests(unittest.TestCase):
    def snapshot(
        self,
        state: work_models.WorkState = work_models.WorkState.READY,
        *,
        attempt_state: work_models.AttemptState | None = None,
        dispositions: tuple[work_models.ReplacementDisposition, ...] = (),
        relation_value: work_models.PlannedReplacement | None = None,
    ) -> LedgerSnapshot:
        attempt = AttemptId("old-work-1") if attempt_state is not None else None
        attempts = (
            ()
            if attempt_state is None
            else (work_models.AttemptRecord(AttemptId("old-work-1"), ItemId("old-work"), attempt_state),)
        )
        return LedgerSnapshot(
            "7",
            (
                work_item("old-work", state, None if attempt is None else str(attempt)),
                work_item("new-work", work_models.WorkState.INTAKE),
            ),
            attempts,
            subject_revisions=(
                work_models.SubjectRevision(ItemId("old-work"), "4"),
                work_models.SubjectRevision(ItemId("new-work"), "2"),
                *(() if attempt is None else (work_models.SubjectRevision(attempt, "3"),)),
            ),
            planned_replacements=() if relation_value is None else (relation_value,),
            replacement_dispositions=dispositions,
        )

    def action_ids(self, snapshot: LedgerSnapshot) -> set[str]:
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT,
            decision_models.AuthorizationKind.PROJECT,
            0,
        )
        return {str(decision_models.action_id(value)) for value in expect_success(available_actions(snapshot, actor))}

    def test_unresolved_relation_withholds_start_continue_resume_and_acceptance_but_keeps_safe_routes(self) -> None:
        ready = self.action_ids(self.snapshot(relation_value=relation()))
        self.assertNotIn("activate:old-work", ready)
        self.assertIn("revise-item:old-work", ready)
        self.assertIn("defer:old-work", ready)
        self.assertIn("close:old-work", ready)
        self.assertIn("record-replacement:old-work", ready)
        self.assertIn("retain-temporarily:old-work", ready)

        active = self.action_ids(
            self.snapshot(
                work_models.WorkState.ACTIVE, attempt_state=work_models.AttemptState.ACTIVE, relation_value=relation()
            )
        )
        self.assertNotIn("continue:old-work-1", active)
        self.assertNotIn("dispatch:old-work-1", active)
        self.assertNotIn("complete:old-work-1", active)
        self.assertIn("pause:old-work-1", active)
        self.assertIn("block:old-work-1", active)
        self.assertIn("revise-item:old-work", active)

        paused = self.action_ids(
            self.snapshot(
                work_models.WorkState.PAUSED, attempt_state=work_models.AttemptState.PAUSED, relation_value=relation()
            )
        )
        self.assertNotIn("resume:old-work", paused)
        self.assertIn("close:old-work", paused)

        blocked_attempt = self.action_ids(
            self.snapshot(
                work_models.WorkState.BLOCKED,
                attempt_state=work_models.AttemptState.BLOCKED,
                relation_value=relation(),
            )
        )
        self.assertNotIn("resume:old-work", blocked_attempt)
        self.assertIn("close:old-work", blocked_attempt)

        blocked_unstarted = self.action_ids(self.snapshot(work_models.WorkState.BLOCKED, relation_value=relation()))
        self.assertNotIn("resume:old-work", blocked_unstarted)
        self.assertIn("close:old-work", blocked_unstarted)

        review = self.action_ids(
            self.snapshot(
                work_models.WorkState.REVIEW, attempt_state=work_models.AttemptState.REVIEW, relation_value=relation()
            )
        )
        self.assertNotIn("accept-checkpoint:old-work-1", review)
        self.assertNotIn("accept-review-and-continue:old-work-1", review)
        self.assertNotIn("complete:old-work-1", review)
        self.assertIn("return-for-correction:old-work-1", review)

        worker_snapshot = replace(
            self.snapshot(
                work_models.WorkState.ACTIVE,
                attempt_state=work_models.AttemptState.ACTIVE,
                relation_value=relation(),
            ),
            attempt_authorities=(
                work_models.AttemptAuthority(AttemptId("old-work-1"), ItemId("old-work"), LeaseId("worker-lease"), 1),
            ),
        )
        worker = decision_models.ActorAuthority(
            decision_models.Role.WORKER,
            decision_models.AuthorizationKind.ATTEMPT,
            1,
            LeaseId("worker-lease"),
            (AttemptId("old-work-1"),),
        )
        worker_actions = {
            str(decision_models.action_id(value))
            for value in expect_success(available_actions(worker_snapshot, worker))
        }
        self.assertNotIn("continue:old-work-1", worker_actions)
        self.assertNotIn("submit-review:old-work-1", worker_actions)
        self.assertIn("report-blocker:old-work-1", worker_actions)

    def test_exact_revision_disposition_restores_guarded_actions_and_old_disposition_does_not(self) -> None:
        current = relation(2)
        old_disposition = work_models.ReplacementDisposition(
            ItemId("old-work"),
            1,
            "A short-lived customer commitment still needs it.",
            "One implementation and review pass will be discarded.",
            TaskId("human"),
            NOW,
        )
        self.assertNotIn(
            "continue:old-work-1",
            self.action_ids(
                self.snapshot(
                    work_models.WorkState.ACTIVE,
                    attempt_state=work_models.AttemptState.ACTIVE,
                    relation_value=current,
                    dispositions=(old_disposition,),
                )
            ),
        )
        accepted = work_models.ReplacementDisposition(
            ItemId("old-work"),
            2,
            "A short-lived customer commitment still needs it.",
            current.replacement_cost,
            TaskId("human"),
            NOW,
        )
        action_ids = self.action_ids(
            self.snapshot(
                work_models.WorkState.ACTIVE,
                attempt_state=work_models.AttemptState.ACTIVE,
                relation_value=current,
                dispositions=(accepted,),
            )
        )
        self.assertIn("continue:old-work-1", action_ids)
        self.assertIn("dispatch:old-work-1", action_ids)
        self.assertIn("complete:old-work-1", action_ids)

        withdrawn = relation(3, status=work_models.PlannedReplacementStatus.WITHDRAWN)
        after_withdrawal = self.action_ids(
            self.snapshot(
                work_models.WorkState.ACTIVE,
                attempt_state=work_models.AttemptState.ACTIVE,
                relation_value=withdrawn,
                dispositions=(accepted,),
            )
        )
        self.assertIn("continue:old-work-1", after_withdrawal)

    def test_record_and_disposition_decisions_preserve_lifecycle_and_bind_exact_revision(self) -> None:
        snapshot = self.snapshot(relation_value=None)
        record = decision_models.RecordReplacementCommand(
            action(decision_models.RecordReplacementAction, ItemId("old-work")),
            work_models.RecordPlannedReplacementInput(
                ItemId("old-work"),
                0,
                ItemId("new-work"),
                "Doing old-work first spends implementation and review time on code new-work will replace.",
                work_models.PlannedReplacementStatus.CURRENT,
                TaskId("planner"),
            ),
        )
        recorded = expect_success(decide(snapshot, record, NOW))
        self.assertIsInstance(recorded.change, decision_models.PlannedReplacementChange)
        self.assertEqual(work_models.WorkState.READY, snapshot.item(ItemId("old-work")).state)  # type: ignore[union-attr]

        current = relation()
        retain = decision_models.RetainTemporarilyCommand(
            action(decision_models.RetainTemporarilyAction, ItemId("old-work")),
            work_models.RetainTemporarilyInput(
                ItemId("old-work"),
                1,
                "A short-lived customer commitment still needs it.",
                current.replacement_cost,
                TaskId("human"),
            ),
        )
        retained = expect_success(decide(self.snapshot(relation_value=current), retain, NOW))
        self.assertIsInstance(retained.change, decision_models.ReplacementDispositionChange)
        self.assertEqual(1, retained.change.disposition.relation_revision)

    def test_resume_decision_rejects_a_replacement_recorded_after_selection(self) -> None:
        resume = decision_models.ResumeCommand(
            action(decision_models.ResumeAction, ItemId("old-work")),
            work_models.ResumeInput(None),
        )

        rejected = decide(
            self.snapshot(work_models.WorkState.BLOCKED, relation_value=relation()),
            resume,
            NOW,
        )

        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, rejected.code)
        self.assertEqual("Item 'old-work' has an unresolved planned replacement.", rejected.message)

    def test_title_similarity_never_creates_a_relation(self) -> None:
        snapshot = LedgerSnapshot(
            "1",
            (
                work_item("replace-cache", work_models.WorkState.READY),
                work_item("replace-cache-again", work_models.WorkState.INTAKE),
            ),
            subject_revisions=(
                work_models.SubjectRevision(ItemId("replace-cache"), "1"),
                work_models.SubjectRevision(ItemId("replace-cache-again"), "1"),
            ),
        )
        self.assertNotIn("retain-temporarily:replace-cache", self.action_ids(snapshot))


if __name__ == "__main__":
    unittest.main()
