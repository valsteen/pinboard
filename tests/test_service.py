import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.artifacts import write_revision
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import (
    CheckpointArtifacts,
    EvidenceArtifactRef,
    NewArtifact,
    ResultArtifactRef,
    WorkBriefIdentity,
)
from pinboard.application.mutation_models import CommittedEffect
from pinboard.application.service import (
    create_proposal,
    decide_and_commit_attempt_authority_change,
    decide_and_commit_checkpoint_acceptance,
    decide_and_commit_transition,
)
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import (
    available_actions,
)
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    CandidateId,
    CheckpointId,
    HostId,
    ItemId,
    LeaseId,
    ProposalId,
    TaskId,
)
from pinboard.domain.proposal_models import (
    CreateProposalOperation,
    ProposalIntake,
)
from pinboard.interfaces.transition_input import parse_transition_command
from tests.decision_support import (
    project_decision_snapshot,
    project_inactive_attempt_authority,
)
from tests.domain_support import expect_transition_command
from tests.support import (
    SQLITE_NOW,
    complete_sqlite_state,
    initialize_store,
    reject_table_deletes,
    with_definition_dependencies,
)


def non_checkpoint_command(
    result: DecisionFailure | decision_models.TransitionCommand,
) -> decision_models.NonCheckpointTransitionCommand:
    if isinstance(result, (DecisionFailure, decision_models.AcceptCheckpointCommand)):
        raise AssertionError(f"Expected a non-checkpoint command, received {result!r}")
    return result


def checkpoint_command(
    result: DecisionFailure | decision_models.TransitionCommand,
) -> decision_models.AcceptCheckpointCommand:
    if not isinstance(result, decision_models.AcceptCheckpointCommand):
        raise AssertionError(f"Expected checkpoint acceptance, received {result!r}")
    return result


class ServiceTest(unittest.TestCase):
    def _store(self) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        return store

    def _store_with_state(self, state: stored_state.StoredWorkState) -> tuple[SQLiteWorkStore, Path]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)
        return store, roots.database_path

    def _project_action[ActionT: decision_models.Action](
        self, store: SQLiteWorkStore, action_type: type[ActionT], subject: str | None = None
    ) -> ActionT:
        snapshot = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT,
            decision_models.AuthorizationKind.PROJECT,
            0,
        )
        result = available_actions(snapshot, actor)
        self.assertIsInstance(result, tuple)
        selected = next(
            action
            for action in result
            if isinstance(action, action_type) and (subject is None or str(action.capability.subject) == subject)
        )
        assert isinstance(selected, action_type)
        return selected

    def _commit_transition(
        self,
        store: SQLiteWorkStore,
        command: decision_models.NonCheckpointTransitionCommand,
        now: datetime,
        *,
        transition_brief_identity: WorkBriefIdentity | None = None,
    ) -> DecisionFailure | CommittedEffect:
        is_project = command.action.capability.authorization == decision_models.AuthorizationKind.PROJECT
        return decide_and_commit_transition(
            store,
            command,
            now,
            actor_task_id=TaskId("project-task") if is_project else None,
            actor_host_id=HostId("host-a") if is_project else None,
            transition_brief_identity=transition_brief_identity,
        )

    def _create_proposal(
        self, store: SQLiteWorkStore, operation: CreateProposalOperation, now: datetime
    ) -> DecisionFailure | CommittedEffect:
        return create_proposal(
            store,
            operation,
            now,
            actor_task_id=TaskId("discovering-task"),
            actor_host_id=HostId("host-a"),
        )

    def _worker_action[ActionT: decision_models.Action](
        self, store: SQLiteWorkStore, action_type: type[ActionT]
    ) -> ActionT:
        snapshot = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW)
        authority = snapshot.command_attempt_authorities[0]
        actor = decision_models.ActorAuthority(
            decision_models.Role.WORKER,
            decision_models.AuthorizationKind.ATTEMPT,
            authority.generation,
            authority.lease_id,
            (authority.attempt,),
            False,
        )
        result = available_actions(snapshot, actor)
        self.assertIsInstance(result, tuple)
        selected = next(action for action in result if isinstance(action, action_type))
        assert isinstance(selected, action_type)
        return selected

    def test_decide_and_commit_transition_uses_one_locked_snapshot(self) -> None:
        store = self._store()
        before = store.validated_snapshot()
        action = self._project_action(store, decision_models.PauseAction)
        result = non_checkpoint_command(
            decision_models.PauseCommand(action, work_models.ReasonInput("Pause at a stable checkpoint."))
        )

        outcome = self._commit_transition(store, result, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(outcome, DecisionFailure)
        after = store.validated_snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(len(before.transition_receipts) + 1, len(after.transition_receipts))

    def test_revised_brief_identity_mismatches_reject_before_commit(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(value, state=stored_state.StoredWorkItemState.PAUSED)
                    if value.item_id == ItemId("work-a")
                    else value
                    for value in state.lifecycle.work_items
                ),
                dependencies=tuple(
                    value for value in state.lifecycle.dependencies if value.item_id != ItemId("work-a")
                ),
                attempts=(replace(state.lifecycle.attempts[0], state=work_models.AttemptState.PAUSED),),
            ),
        )
        state = with_definition_dependencies(state, ItemId("work-a"), ())
        store, _database = self._store_with_state(state)
        action = self._project_action(store, decision_models.ResumeAction)
        command = non_checkpoint_command(
            decision_models.ResumeCommand(action, work_models.ResumeInput(state.artifact_references[0].artifact_ref_id))
        )
        identity = WorkBriefIdentity(
            "work-a-1",
            "work-a",
            "codex/work-a",
            "base-revision",
            1,
            next(
                value.digest
                for value in state.lifecycle.definition_revisions
                if value.item_id == ItemId("work-a") and value.revision == 1
            ),
        )
        mismatches = (
            replace(identity, attempt_id="different-1"),
            replace(identity, item_id="different"),
            replace(identity, branch="codex/different"),
            replace(identity, base_revision="different-base"),
            replace(identity, accepted_scope_revision=2),
            replace(identity, accepted_scope_digest="f" * 64),
        )
        before = store.validated_snapshot()

        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                result = self._commit_transition(
                    store,
                    command,
                    SQLITE_NOW + timedelta(seconds=1),
                    transition_brief_identity=mismatch,
                )
                self.assertIsInstance(result, DecisionFailure)
                assert isinstance(result, DecisionFailure)
                self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, result.code)
                self.assertEqual(before, store.validated_snapshot())

    def test_active_and_paused_rebind_commit_exact_lineage_and_authority_fence(self) -> None:
        for item_state, attempt_state in (
            (stored_state.StoredWorkItemState.ACTIVE, work_models.AttemptState.ACTIVE),
            (stored_state.StoredWorkItemState.PAUSED, work_models.AttemptState.PAUSED),
        ):
            with self.subTest(item_state=item_state):
                state = complete_sqlite_state()
                accepted_definition = next(
                    value for value in state.lifecycle.definition_revisions if value.item_id == ItemId("work-a")
                )
                current_definition_value = replace(
                    accepted_definition.definition,
                    objective="Accept current scope while correcting the Git lineage.",
                )
                current_digest = work_item_definition_digest(current_definition_value)
                assert isinstance(current_digest, str)
                current_definition = replace(
                    accepted_definition,
                    revision=accepted_definition.revision + 1,
                    digest=current_digest,
                    definition=current_definition_value,
                    reason="Exercise definition-stale rebinding.",
                    before_digest=accepted_definition.digest,
                    after_digest=current_digest,
                    accepted_project_revision=state.lifecycle.project.revision,
                )
                protected_result = replace(
                    state.artifact_references[2],
                    artifact_ref_id=ArtifactRefId(98),
                    key="work-a-protected-result",
                    kind=work_models.ArtifactKind.RESULT,
                    selector="artifacts/results/work-a-protected-result/1.opaque",
                    content_sha256="a" * 64,
                )
                current_attempt = replace(
                    state.lifecycle.attempts[0],
                    result_artifact_ref_id=protected_result.artifact_ref_id,
                )
                replacement = replace(
                    state.artifact_references[0],
                    artifact_ref_id=ArtifactRefId(99),
                    key="work-a-rebound-brief",
                    selector="artifacts/briefs/work-a-rebound-brief/1.opaque",
                    content_sha256="b" * 64,
                )
                state = replace(
                    state,
                    lifecycle=replace(
                        state.lifecycle,
                        work_items=tuple(
                            replace(value, state=item_state) if value.item_id == ItemId("work-a") else value
                            for value in state.lifecycle.work_items
                        ),
                        attempts=(replace(current_attempt, state=attempt_state),),
                        definition_revisions=(*state.lifecycle.definition_revisions, current_definition),
                    ),
                    artifact_references=(*state.artifact_references, protected_result, replacement),
                )
                store, database_path = self._store_with_state(state)
                action = self._project_action(store, decision_models.RebindAttemptAction)
                command = non_checkpoint_command(
                    decision_models.RebindAttemptCommand(
                        action,
                        work_models.RebindAttemptInput(
                            AttemptId("work-a-1"),
                            "codex/corrected-work-a",
                            "corrected-base",
                            replacement.artifact_ref_id,
                        ),
                    )
                )
                identity = WorkBriefIdentity(
                    "work-a-1",
                    "work-a",
                    "codex/corrected-work-a",
                    "corrected-base",
                    current_definition.revision,
                    current_definition.digest,
                )
                before = store.validated_snapshot()
                for mismatch in (
                    replace(identity, attempt_id="work-a-2"),
                    replace(identity, item_id="work-c"),
                    replace(identity, branch="codex/other"),
                    replace(identity, base_revision="other-base"),
                    replace(identity, accepted_scope_revision=identity.accepted_scope_revision + 1),
                    replace(identity, accepted_scope_digest="f" * 64),
                ):
                    mismatched = self._commit_transition(
                        store,
                        command,
                        SQLITE_NOW + timedelta(milliseconds=1),
                        transition_brief_identity=mismatch,
                    )
                    self.assertIsInstance(mismatched, DecisionFailure)
                    self.assertEqual(before, store.validated_snapshot())

                receipt = self._commit_transition(
                    store,
                    command,
                    SQLITE_NOW + timedelta(seconds=1),
                    transition_brief_identity=identity,
                )

                self.assertNotIsInstance(receipt, DecisionFailure)
                reopened = SQLiteWorkStore(database_path).validated_snapshot()
                rebound = reopened.lifecycle.attempts[0]
                self.assertEqual(
                    replace(
                        current_attempt,
                        state=attempt_state,
                        branch="codex/corrected-work-a",
                        base_revision="corrected-base",
                        brief_artifact_ref_id=replacement.artifact_ref_id,
                        accepted_scope_revision=current_definition.revision,
                        accepted_scope_digest=current_definition.digest,
                        subject_revision=13,
                        updated_at=SQLITE_NOW + timedelta(seconds=1),
                    ),
                    rebound,
                )
                self.assertEqual(4, reopened.authority.attempt_counters[0].generation_high_water)
                self.assertEqual(4, reopened.authority.attempt_leases[0].generation)
                self.assertEqual(
                    authority_models.AttemptLeaseStatus.REVOKED,
                    reopened.authority.attempt_leases[0].state,
                )

                stale = self._commit_transition(
                    store,
                    command,
                    SQLITE_NOW + timedelta(seconds=2),
                    transition_brief_identity=identity,
                )
                self.assertIsInstance(stale, DecisionFailure)
                self.assertEqual(reopened, store.validated_snapshot())

    def test_decide_and_commit_transition_accepts_exact_live_worker_authority(self) -> None:
        store = self._store()
        before = store.validated_snapshot()
        action = self._worker_action(store, decision_models.SubmitReviewAction)
        command = non_checkpoint_command(
            decision_models.SubmitReviewCommand(action, work_models.SubmitReviewInput(CandidateId("candidate-review")))
        )
        committed_mutation = self._commit_transition(store, command, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(committed_mutation, DecisionFailure)
        assert not isinstance(committed_mutation, DecisionFailure)
        self.assertEqual(before.lifecycle.project.revision + 1, committed_mutation.receipt.project_revision)
        self.assertEqual(ActionId("submit-review:work-a-1"), committed_mutation.receipt.transition.action_id)
        self.assertEqual("review", store.validated_snapshot().lifecycle.attempts[0].state.value)

    def test_positive_item_state_variants_reload_from_fresh_stores(self) -> None:
        for action_type, initial, payload, expected in (
            (
                decision_models.MarkReadyAction,
                stored_state.StoredWorkItemState.INTAKE,
                b'{"reason":"The intake is ready."}',
                stored_state.StoredWorkItemState.READY,
            ),
            (
                decision_models.BlockItemAction,
                stored_state.StoredWorkItemState.INTAKE,
                b'{"reason":"The intake awaits a dependency."}',
                stored_state.StoredWorkItemState.BLOCKED,
            ),
            (
                decision_models.ReopenAction,
                stored_state.StoredWorkItemState.DEFERRED,
                b'{"evidence":"The prerequisite is now available."}',
                stored_state.StoredWorkItemState.INTAKE,
            ),
        ):
            with self.subTest(action_type=action_type.__name__):
                state = complete_sqlite_state()
                lifecycle = state.lifecycle
                state = replace(
                    state,
                    lifecycle=replace(
                        lifecycle,
                        work_items=tuple(
                            replace(value, state=initial) if value.item_id == ItemId("intake-work") else value
                            for value in lifecycle.work_items
                        ),
                    ),
                )
                store, database_path = self._store_with_state(state)
                command = non_checkpoint_command(
                    expect_transition_command(
                        parse_transition_command(
                            self._project_action(store, action_type, "intake-work"),
                            payload,
                        )
                    )
                )

                result = self._commit_transition(store, command, SQLITE_NOW + timedelta(seconds=1))

                self.assertNotIsInstance(result, DecisionFailure)
                reloaded = SQLiteWorkStore(database_path).validated_snapshot()
                item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("intake-work"))
                self.assertEqual(expected, item.state)

    def test_positive_attempt_state_variants_reload_from_fresh_stores(self) -> None:
        for action_type, payload, expected_attempt, expected_item in (
            (
                decision_models.PauseAction,
                b'{"reason":"Pause at a stable point."}',
                work_models.AttemptState.PAUSED,
                stored_state.StoredWorkItemState.PAUSED,
            ),
            (
                decision_models.BlockAttemptAction,
                b'{"reason":"The attempt awaits a dependency."}',
                work_models.AttemptState.BLOCKED,
                stored_state.StoredWorkItemState.BLOCKED,
            ),
        ):
            with self.subTest(action_type=action_type.__name__):
                store, database_path = self._store_with_state(complete_sqlite_state())
                command = non_checkpoint_command(
                    expect_transition_command(
                        parse_transition_command(self._project_action(store, action_type), payload)
                    )
                )

                result = self._commit_transition(store, command, SQLITE_NOW + timedelta(seconds=1))

                self.assertNotIsInstance(result, DecisionFailure)
                reloaded = SQLiteWorkStore(database_path).validated_snapshot()
                item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
                attempt = next(
                    value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1")
                )
                self.assertEqual(expected_item, item.state)
                self.assertEqual(expected_attempt, attempt.state)

    def test_checkpoint_acceptance_requires_protected_candidate_and_reloads_from_fresh_store(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        database_path = roots.database_path
        submit_action = self._worker_action(store, decision_models.SubmitReviewAction)
        submit = non_checkpoint_command(
            decision_models.SubmitReviewCommand(
                submit_action,
                work_models.SubmitReviewInput(CandidateId("protected-candidate")),
            )
        )
        submitted = self._commit_transition(store, submit, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(submitted, DecisionFailure)
        submitted_store = SQLiteWorkStore(database_path)
        submitted_attempt = submitted_store.validated_snapshot().lifecycle.attempts[0]
        self.assertEqual("protected-candidate", submitted_attempt.candidate_revision)
        accept_action = self._project_action(submitted_store, decision_models.AcceptCheckpointAction)
        mismatch = checkpoint_command(
            decision_models.AcceptCheckpointCommand(
                accept_action,
                work_models.AcceptCheckpointInput(
                    CheckpointId("checkpoint-a"),
                    CandidateId("supplied-different-candidate"),
                    "This must not commit.",
                ),
            )
        )
        result_artifact = write_revision(
            roots,
            NewArtifact(work_models.ArtifactKind.RESULT, "work-a-1-checkpoint-a-result", 1, ".md", b"result\n"),
        )
        review_artifact = write_revision(
            roots,
            NewArtifact(work_models.ArtifactKind.EVIDENCE, "work-a-1-checkpoint-a-review", 1, ".md", b"review\n"),
        )
        checkpoint_artifacts = CheckpointArtifacts(
            ResultArtifactRef(
                result_artifact.key,
                result_artifact.revision,
                result_artifact.selector,
                result_artifact.content_sha256,
                result_artifact.size_bytes,
            ),
            EvidenceArtifactRef(
                review_artifact.key,
                review_artifact.revision,
                review_artifact.selector,
                review_artifact.content_sha256,
                review_artifact.size_bytes,
            ),
        )
        before_acceptance = submitted_store.validated_snapshot()

        rejected = decide_and_commit_checkpoint_acceptance(
            submitted_store,
            mismatch,
            SQLITE_NOW + timedelta(seconds=2),
            checkpoint_artifacts,
            actor_task_id=TaskId("project-task"),
            actor_host_id=HostId("host-a"),
        )

        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)
        self.assertEqual(before_acceptance, submitted_store.validated_snapshot())
        accept = checkpoint_command(
            decision_models.AcceptCheckpointCommand(
                accept_action,
                work_models.AcceptCheckpointInput(
                    CheckpointId("checkpoint-a"),
                    CandidateId("protected-candidate"),
                    "Checkpoint evidence is accepted.",
                ),
            )
        )

        with (
            patch(
                "pinboard.adapters.sqlite.state.append_history",
                side_effect=StorageError(StorageErrorCode.IO_ERROR, "injected checkpoint write failure"),
            ),
            self.assertRaises(StorageError),
        ):
            decide_and_commit_checkpoint_acceptance(
                submitted_store,
                accept,
                SQLITE_NOW + timedelta(seconds=2),
                checkpoint_artifacts,
                actor_task_id=TaskId("project-task"),
                actor_host_id=HostId("host-a"),
            )

        self.assertEqual(before_acceptance, SQLiteWorkStore(database_path).validated_snapshot())

        with reject_table_deletes("work_items"):
            accepted = decide_and_commit_checkpoint_acceptance(
                submitted_store,
                accept,
                SQLITE_NOW + timedelta(seconds=2),
                checkpoint_artifacts,
                actor_task_id=TaskId("project-task"),
                actor_host_id=HostId("host-a"),
            )

        self.assertNotIsInstance(accepted, DecisionFailure)
        reloaded = SQLiteWorkStore(database_path).validated_snapshot()
        item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        authority = reloaded.authority.attempt_leases[0]
        self.assertEqual(stored_state.StoredWorkItemState.PAUSED, item.state)
        self.assertEqual(work_models.AttemptState.PAUSED, attempt.state)
        self.assertIsNone(attempt.candidate_revision)
        self.assertEqual(authority_models.AttemptLeaseStatus.REVOKED, authority.state)
        self.assertEqual(4, authority.generation)
        self.assertEqual(before_acceptance.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        self.assertEqual(len(before_acceptance.transition_receipts) + 1, len(reloaded.transition_receipts))
        result_reference = next(
            value for value in reloaded.artifact_references if value.kind == work_models.ArtifactKind.RESULT
        )
        review_reference = next(
            value for value in reloaded.artifact_references if value.key == "work-a-1-checkpoint-a-review"
        )
        self.assertEqual(result_reference.artifact_ref_id, attempt.result_artifact_ref_id)
        self.assertEqual(review_reference.artifact_ref_id, reloaded.transition_receipts[-1].artifact_ref_id)
        outcome = reloaded.transition_receipts[-1].outcome_payload
        self.assertIn(b'"candidate":"protected-candidate"', outcome)
        self.assertIn(b'"checkpoint":"checkpoint-a"', outcome)

    def test_review_acceptance_continues_the_attempt_and_reloads_every_fact(self) -> None:
        store, database_path = self._store_with_state(complete_sqlite_state())
        submit_action = self._worker_action(store, decision_models.SubmitReviewAction)
        submit = non_checkpoint_command(
            decision_models.SubmitReviewCommand(
                submit_action,
                work_models.SubmitReviewInput(CandidateId("protected-candidate")),
            )
        )
        submitted = self._commit_transition(store, submit, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(submitted, DecisionFailure)
        submitted_store = SQLiteWorkStore(database_path)
        mismatch_action = self._project_action(submitted_store, decision_models.AcceptReviewAndContinueAction)
        mismatch = non_checkpoint_command(
            decision_models.AcceptReviewAndContinueCommand(
                mismatch_action,
                work_models.AcceptReviewAndContinueInput(CandidateId("different-candidate"), "This must not commit."),
            )
        )
        before_mismatch = submitted_store.validated_snapshot()
        rejected = self._commit_transition(submitted_store, mismatch, SQLITE_NOW + timedelta(seconds=2))
        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.TRANSITION_INPUT_INVALID, rejected.code)
        self.assertEqual(before_mismatch, submitted_store.validated_snapshot())
        accept_action = self._project_action(submitted_store, decision_models.AcceptReviewAndContinueAction)
        accept = non_checkpoint_command(
            decision_models.AcceptReviewAndContinueCommand(
                accept_action,
                work_models.AcceptReviewAndContinueInput(
                    CandidateId("protected-candidate"),
                    "The reviewed checkpoint is accepted; continue this attempt.",
                ),
            )
        )

        accepted = self._commit_transition(submitted_store, accept, SQLITE_NOW + timedelta(seconds=3))

        self.assertNotIsInstance(accepted, DecisionFailure)
        reloaded = SQLiteWorkStore(database_path).validated_snapshot()
        item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        authority = reloaded.authority.attempt_leases[0]
        self.assertEqual(stored_state.StoredWorkItemState.ACTIVE, item.state)
        self.assertEqual(work_models.AttemptState.ACTIVE, attempt.state)
        self.assertIsNone(attempt.candidate_revision)
        self.assertIsNone(attempt.candidate_recorded_at)
        self.assertEqual(authority_models.AttemptLeaseStatus.REVOKED, authority.state)
        self.assertEqual(4, authority.generation)
        receipt = reloaded.transition_receipts[-1]
        self.assertEqual(decision_models.ActionKind.ACCEPT_REVIEW_AND_CONTINUE, receipt.action_kind)
        self.assertEqual("transition-receipt/v1", receipt.outcome_schema)
        self.assertIn(b'"candidate":"protected-candidate"', receipt.outcome_payload)
        self.assertIn(
            b'"evidence":"The reviewed checkpoint is accepted; continue this attempt."', receipt.outcome_payload
        )
        self.assertNotIn(b'"checkpoint"', receipt.outcome_payload)

    def test_retained_attempt_closure_fences_authority_and_reloads_from_fresh_store(self) -> None:
        state = complete_sqlite_state()
        lifecycle = state.lifecycle
        state = replace(
            state,
            lifecycle=replace(
                lifecycle,
                work_items=tuple(
                    replace(value, state=stored_state.StoredWorkItemState.PAUSED)
                    if value.item_id == ItemId("work-a")
                    else value
                    for value in lifecycle.work_items
                ),
                attempts=tuple(
                    replace(value, state=work_models.AttemptState.PAUSED)
                    if value.attempt_id == AttemptId("work-a-1")
                    else value
                    for value in lifecycle.attempts
                ),
            ),
        )
        store, database_path = self._store_with_state(state)
        close_action = self._project_action(store, decision_models.CloseAction, "work-a")
        close = non_checkpoint_command(
            decision_models.CloseCommand(
                close_action,
                work_models.CloseInput(work_models.CloseOutcome.DROPPED, "The retained attempt is no longer needed."),
            )
        )

        closed = self._commit_transition(store, close, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(closed, DecisionFailure)
        reloaded = SQLiteWorkStore(database_path).validated_snapshot()
        item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId("work-a"))
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        authority = reloaded.authority.attempt_leases[0]
        self.assertEqual(stored_state.StoredWorkItemState.DROPPED, item.state)
        self.assertEqual("The retained attempt is no longer needed.", item.outcome_evidence)
        self.assertEqual(work_models.AttemptState.DONE, attempt.state)
        self.assertEqual(authority_models.AttemptLeaseStatus.REVOKED, authority.state)
        self.assertEqual(4, authority.generation)

    def test_attempt_authority_renewal_and_release_persist_exact_generation(self) -> None:
        state = complete_sqlite_state()
        store, _database_path = self._store_with_state(state)
        current = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW).command_attempt_authorities[0]
        renewed = decide_and_commit_attempt_authority_change(
            store,
            authority_models.RenewAttemptAuthority(
                current,
                SQLITE_NOW + timedelta(seconds=1),
                current.expires_at + timedelta(minutes=1),
            ),
        )
        self.assertNotIsInstance(renewed, DecisionFailure)
        current = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW).command_attempt_authorities[0]

        with reject_table_deletes("work_items"):
            released = decide_and_commit_attempt_authority_change(
                store,
                authority_models.ReleaseAttemptAuthority(current, SQLITE_NOW + timedelta(seconds=2)),
            )

        self.assertNotIsInstance(released, DecisionFailure)
        after = store.validated_snapshot()
        self.assertEqual(4, after.authority.attempt_counters[0].generation_high_water)
        self.assertEqual(authority_models.AttemptLeaseStatus.RELEASED, after.authority.attempt_leases[0].state)

    def test_attempt_authority_initial_acquire_transfer_and_revoke_persist_exact_generations(self) -> None:
        state = complete_sqlite_state()
        state = replace(
            state,
            authority=replace(
                state.authority,
                attempt_counters=(replace(state.authority.attempt_counters[0], generation_high_water=0),),
                attempt_generations=(),
                attempt_leases=(),
            ),
        )
        store, _database_path = self._store_with_state(state)
        attempt = state.lifecycle.attempts[0]
        acquired = decide_and_commit_attempt_authority_change(
            store,
            authority_models.AcquireInitialAttemptAuthority(
                state.lifecycle.project.host_epoch,
                attempt.attempt_id,
                attempt.item_id,
                TaskId("worker-initial"),
                HostId("host-a"),
                LeaseId("attempt-initial"),
                SQLITE_NOW + timedelta(seconds=1),
                SQLITE_NOW + timedelta(minutes=2),
            ),
        )
        self.assertNotIsInstance(acquired, DecisionFailure)

        normal_state = complete_sqlite_state()
        normal_store, _normal_database_path = self._store_with_state(normal_state)
        snapshot = project_decision_snapshot(normal_store.validated_snapshot(), SQLITE_NOW)
        current = snapshot.command_attempt_authorities[0]
        released = decide_and_commit_attempt_authority_change(
            normal_store,
            authority_models.ReleaseAttemptAuthority(current, SQLITE_NOW + timedelta(seconds=1)),
        )
        self.assertNotIsInstance(released, DecisionFailure)
        proof = project_inactive_attempt_authority(
            normal_store.validated_snapshot(),
            current.attempt,
            SQLITE_NOW + timedelta(seconds=2),
        )
        self.assertNotIsInstance(proof, DecisionFailure)
        assert not isinstance(proof, DecisionFailure)
        transferred = decide_and_commit_attempt_authority_change(
            normal_store,
            authority_models.TransferAttemptAuthority(
                proof,
                TaskId("worker-next"),
                HostId("host-a"),
                LeaseId("attempt-next"),
                SQLITE_NOW + timedelta(seconds=2),
                SQLITE_NOW + timedelta(minutes=2),
            ),
        )
        self.assertNotIsInstance(transferred, DecisionFailure)
        next_authority = project_decision_snapshot(
            normal_store.validated_snapshot(), SQLITE_NOW
        ).command_attempt_authorities[0]
        revoked = decide_and_commit_attempt_authority_change(
            normal_store,
            authority_models.RevokeAttemptAuthority(
                next_authority.attempt,
                next_authority.lease_id,
                next_authority.generation,
                TaskId("project-task"),
                HostId("host-a"),
                SQLITE_NOW + timedelta(seconds=3),
            ),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        self.assertEqual(
            authority_models.AttemptLeaseStatus.REVOKED,
            normal_store.validated_snapshot().authority.attempt_leases[0].state,
        )

    def test_decide_and_commit_transition_rejects_stale_action_before_decision(self) -> None:
        store = self._store()
        action = self._project_action(store, decision_models.PauseAction)
        command = non_checkpoint_command(
            decision_models.PauseCommand(action, work_models.ReasonInput("Pause at a stable checkpoint."))
        )

        first = self._commit_transition(store, command, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(first, DecisionFailure)
        committed = store.validated_snapshot()

        rejected = self._commit_transition(store, command, SQLITE_NOW + timedelta(seconds=2))

        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, rejected.code)
        self.assertEqual(committed, store.validated_snapshot())

    def test_completion_fences_attempt_authority_atomically(self) -> None:
        store = self._store()
        before = store.validated_snapshot()
        prior_attempt = before.authority.attempt_leases[0]
        action = self._project_action(store, decision_models.CompleteAction)
        command = non_checkpoint_command(
            decision_models.CompleteCommand(action, work_models.EvidenceInput("The accepted outcome is complete."))
        )

        receipt = self._commit_transition(store, command, SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.validated_snapshot()
        current_attempt = after.authority.attempt_leases[0]
        self.assertEqual(prior_attempt.generation + 1, current_attempt.generation)
        self.assertEqual(authority_models.AttemptLeaseStatus.REVOKED, current_attempt.state)

    def test_sqlite_proposal_intake_is_placed_at_the_back_without_changing_current_work(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, proposals=replace(state.proposals, proposals=(), evidence=(), freshness=()))
        store, _database_path = self._store_with_state(state)
        before = store.validated_snapshot()
        created_at = SQLITE_NOW - timedelta(days=1)
        recorded_at = SQLITE_NOW + timedelta(seconds=1)
        intake = ProposalIntake(
            ProposalId("sqlite-proposal"),
            created_at,
            TaskId("discovering-task"),
            "SQLite proposal",
            "A local observation needs to be preserved.",
            "The proposal should be preserved immutably.",
            "A proposal row becomes available for disposition.",
            "A task can inspect it later.",
            work_models.IndependentProposalRelation(),
            "The evidence is current.",
            ("source:local",),
            ("The current schema remains accepted.",),
        )

        with reject_table_deletes("work_items"):
            receipt = self._create_proposal(store, CreateProposalOperation(intake), recorded_at)
        duplicate = self._create_proposal(store, CreateProposalOperation(intake), recorded_at + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        self.assertIsInstance(duplicate, DecisionFailure)
        self.assertEqual(DecisionFailureCode.PROPOSAL_ALREADY_EXISTS, duplicate.code)
        after = store.validated_snapshot()
        self.assertEqual(
            (ProposalId("sqlite-proposal"),), tuple(value.proposal_id for value in after.proposals.proposals)
        )
        self.assertEqual(("source:local",), tuple(value.selector for value in after.proposals.evidence))
        proposal = after.proposals.proposals[0]
        intake_item = next(value for value in after.lifecycle.work_items if value.item_id == ItemId("sqlite-proposal"))
        self.assertEqual(stored_state.StoredWorkItemState.INTAKE, intake_item.state)
        self.assertEqual(5, intake_item.queue_position)
        self.assertEqual("proposal:sqlite-proposal", intake_item.source)
        self.assertEqual(before.authority, after.authority)
        self.assertEqual(before.lifecycle.attempts, after.lifecycle.attempts)
        self.assertEqual(created_at, proposal.created_at)
        self.assertEqual(recorded_at, proposal.recorded_at)
        self.assertEqual(recorded_at, after.lifecycle.project.updated_at)
        self.assertEqual(recorded_at, after.transition_receipts[-1].committed_at)

    def test_proposal_requested_position_and_prerequisite_relation_update_one_transaction(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, proposals=replace(state.proposals, proposals=(), evidence=(), freshness=()))
        store, _database_path = self._store_with_state(state)
        before = store.validated_snapshot()
        intake = ProposalIntake(
            ProposalId("required-first"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Required first",
            "Work C needs one newly discovered prerequisite.",
            "The dependency must be in intake before scheduling.",
            "Record the prerequisite candidate and relationship.",
            "A task can evaluate it in queue order.",
            work_models.PrerequisiteProposalRelation(ItemId("work-c")),
            "The relationship is current.",
            ("source:local",),
            ("Work C remains live.",),
            2,
        )

        receipt = self._create_proposal(store, CreateProposalOperation(intake), SQLITE_NOW + timedelta(seconds=1))

        self.assertNotIsInstance(receipt, DecisionFailure)
        after = store.validated_snapshot()
        positions = {
            str(value.item_id): value.queue_position
            for value in after.lifecycle.work_items
            if value.queue_position is not None
        }
        self.assertEqual(
            {
                "intake-work": 1,
                "required-first": 2,
                "work-a": 3,
                "work-c": 4,
                "zz-proposal-a": 5,
            },
            positions,
        )
        target_definitions = tuple(
            value for value in after.lifecycle.definition_revisions if value.item_id == ItemId("work-c")
        )
        self.assertEqual(2, len(target_definitions))
        self.assertEqual(target_definitions[0].revision + 1, target_definitions[1].revision)
        self.assertIn(
            ItemId("required-first"),
            tuple(value.dependency_id for value in after.lifecycle.dependencies if value.item_id == ItemId("work-c")),
        )
        self.assertEqual(before.authority, after.authority)

    def test_proposal_position_outside_the_live_queue_is_rejected_atomically(self) -> None:
        store = self._store()
        intake = ProposalIntake(
            ProposalId("invalid-position"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Invalid position",
            "The requested queue position exceeds the live bounds.",
            "Invalid insertion must not partially persist.",
            "Reject the request before mutation.",
            "Keep the prior queue intact.",
            work_models.IndependentProposalRelation(),
            "The queue currently contains four live items.",
            (),
            (),
            6,
        )
        before = store.validated_snapshot()

        result = self._create_proposal(store, CreateProposalOperation(intake), SQLITE_NOW + timedelta(seconds=1))

        self.assertIsInstance(result, DecisionFailure)
        assert isinstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.PROPOSAL_INVALID, result.code)
        self.assertEqual(before, store.validated_snapshot())

    def test_sqlite_proposal_intake_rejects_a_missing_related_item_before_persistence(self) -> None:
        state = complete_sqlite_state()
        state = replace(state, proposals=replace(state.proposals, proposals=(), evidence=(), freshness=()))
        store, _database_path = self._store_with_state(state)
        intake = ProposalIntake(
            ProposalId("missing-related-item"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Missing relation",
            "A proposal names an absent item.",
            "Storage must not be the first validator.",
            "The proposal is rejected without mutation.",
            "Return a typed item rejection.",
            work_models.FollowUpProposalRelation(ItemId("does-not-exist")),
            "The related item is absent.",
            (),
            (),
        )
        before = store.validated_snapshot()

        result = self._create_proposal(store, CreateProposalOperation(intake), SQLITE_NOW + timedelta(seconds=1))

        self.assertIsInstance(result, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_NOT_FOUND, result.code)
        self.assertEqual(before, store.validated_snapshot())

    def test_proposal_relationships_reject_missing_identities_before_sqlite(self) -> None:
        dependency_store = self._store()
        accept = self._project_action(dependency_store, decision_models.AcceptProposalAction)
        dependency_command = non_checkpoint_command(
            decision_models.AcceptProposalCommand(
                accept,
                work_models.AcceptProposalInput(
                    ItemId("zz-proposal-a"),
                    work_models.AcceptedProposalState.INTAKE,
                    "review-intake",
                    work_models.Timing.SAFE_TO_DEFER,
                    (ItemId("missing-dependency"),),
                ),
            ),
        )
        before = dependency_store.validated_snapshot()
        dependency_rejected = self._commit_transition(
            dependency_store,
            dependency_command,
            SQLITE_NOW + timedelta(seconds=1),
        )
        self.assertIsInstance(dependency_rejected, DecisionFailure)
        assert isinstance(dependency_rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.DEPENDENCY_NOT_SATISFIED, dependency_rejected.code)
        self.assertEqual(before, dependency_store.validated_snapshot())

        merge_store = self._store()
        merge = self._project_action(merge_store, decision_models.MergeProposalAction)
        merge_command = non_checkpoint_command(
            decision_models.MergeProposalCommand(merge, work_models.MergeProposalInput(ItemId("missing-target")))
        )
        before = merge_store.validated_snapshot()
        merge_rejected = self._commit_transition(merge_store, merge_command, SQLITE_NOW + timedelta(seconds=1))
        self.assertIsInstance(merge_rejected, DecisionFailure)
        assert isinstance(merge_rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_NOT_FOUND, merge_rejected.code)
        self.assertEqual(before, merge_store.validated_snapshot())

    def test_attempt_transfer_rejects_terminal_work_without_mutation(self) -> None:
        store = self._store()
        complete_action = self._project_action(store, decision_models.CompleteAction)
        complete_command = non_checkpoint_command(
            decision_models.CompleteCommand(
                complete_action, work_models.EvidenceInput("Terminal transfer must stay fenced.")
            )
        )
        completed = self._commit_transition(store, complete_command, SQLITE_NOW + timedelta(seconds=1))
        self.assertNotIsInstance(completed, DecisionFailure)
        terminal = store.validated_snapshot()
        proof = project_inactive_attempt_authority(
            terminal,
            AttemptId("work-a-1"),
            SQLITE_NOW + timedelta(seconds=2),
        )
        self.assertNotIsInstance(proof, DecisionFailure)
        assert not isinstance(proof, DecisionFailure)
        rejected = decide_and_commit_attempt_authority_change(
            store,
            authority_models.TransferAttemptAuthority(
                proof,
                TaskId("terminal-worker"),
                HostId("host-a"),
                LeaseId("terminal-attempt"),
                SQLITE_NOW + timedelta(seconds=3),
                SQLITE_NOW + timedelta(minutes=2),
            ),
        )

        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, rejected.code)
        self.assertEqual(terminal, store.validated_snapshot())


if __name__ == "__main__":
    unittest.main()
