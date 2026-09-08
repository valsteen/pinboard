from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from multiprocessing.synchronize import Barrier
from pathlib import Path

from pinboard.adapters.files.artifacts import write_revision
from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.artifacts import (
    CheckpointArtifacts,
    EvidenceArtifactRef,
    NewArtifact,
    ResultArtifactRef,
    WorkBriefIdentity,
)
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.mutations import project_checkpoint_acceptance_mutation, project_transition_mutation
from pinboard.application.service import (
    create_proposal,
    decide_and_commit_preparation_authority_change,
    decide_and_commit_transition,
)
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import (
    available_actions,
    decide,
)
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import (
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
from pinboard.domain.proposal_models import CreateProposalOperation, ProposalIntake
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store, mutation_allocation


def _commit_same_pause(
    database_path: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    store = SQLiteWorkStore(Path(database_path))
    before = store.snapshot()
    snapshot = project_decision_snapshot(before, SQLITE_NOW)
    actor = decision_models.ActorAuthority(decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0)
    actions = expect_success(available_actions(snapshot, actor))
    action = next(value for value in actions if value.kind == decision_models.ActionKind.PAUSE)
    assert isinstance(action, decision_models.PauseAction)
    selected_command = decision_models.PauseCommand(action, work_models.ReasonInput("Concurrent pause."))
    decision = expect_success(decide(snapshot, selected_command, SQLITE_NOW))
    assert isinstance(decision, decision_models.TransitionDecision)
    mutation = project_transition_mutation(
        mutation_allocation(before), decision, TaskId("project-task"), HostId("host-a")
    )
    barrier.wait()
    with store.write() as transaction:
        result = transaction.commit(mutation)
    results.put(result.code.value if isinstance(result, DecisionFailure) else "committed")


def _commit_same_rebind(
    database_path: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    store = SQLiteWorkStore(Path(database_path))
    before = store.snapshot()
    snapshot = project_decision_snapshot(before, SQLITE_NOW)
    actor = decision_models.ActorAuthority(decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0)
    actions = expect_success(available_actions(snapshot, actor))
    action = next(value for value in actions if value.kind == decision_models.ActionKind.REBIND_ATTEMPT)
    assert isinstance(action, decision_models.RebindAttemptAction)
    selected_command = decision_models.RebindAttemptCommand(
        action,
        work_models.RebindAttemptInput(
            AttemptId("work-a-1"), "codex/corrected-work-a", "corrected-base", ArtifactRefId(99)
        ),
    )
    decision = expect_success(decide(snapshot, selected_command, SQLITE_NOW))
    assert isinstance(decision, decision_models.TransitionDecision)
    mutation = project_transition_mutation(
        mutation_allocation(before), decision, TaskId("project-task"), HostId("host-a")
    )
    barrier.wait()
    with store.write() as transaction:
        result = transaction.commit(mutation)
    results.put(result.code.value if isinstance(result, DecisionFailure) else "committed")


def _commit_same_checkpoint(
    project_path: str,
    database_path: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    roots = resolve_durable_roots(Path(project_path))
    store = SQLiteWorkStore(Path(database_path))
    before = store.snapshot()
    snapshot = project_decision_snapshot(before, SQLITE_NOW)
    actor = decision_models.ActorAuthority(
        decision_models.Role.PROJECT,
        decision_models.AuthorizationKind.PROJECT,
        0,
    )
    actions = expect_success(available_actions(snapshot, actor))
    action = next(value for value in actions if value.kind == decision_models.ActionKind.ACCEPT_CHECKPOINT)
    assert isinstance(action, decision_models.AcceptCheckpointAction)
    selected_command = decision_models.AcceptCheckpointCommand(
        action,
        work_models.AcceptCheckpointInput(
            CheckpointId("checkpoint-a"),
            CandidateId("candidate-a"),
            "Accept concurrent checkpoint evidence.",
        ),
    )
    decision = expect_success(decide(snapshot, selected_command, SQLITE_NOW))
    assert isinstance(decision, decision_models.CheckpointAcceptanceDecision)
    result = write_revision(
        roots,
        NewArtifact(work_models.ArtifactKind.RESULT, "work-a-1-checkpoint-a-result", 1, ".md", b"result\n"),
    )
    review = write_revision(
        roots,
        NewArtifact(work_models.ArtifactKind.EVIDENCE, "work-a-1-checkpoint-a-review", 1, ".md", b"review\n"),
    )
    artifacts = CheckpointArtifacts(
        ResultArtifactRef(result.key, result.revision, result.selector, result.content_sha256, result.size_bytes),
        EvidenceArtifactRef(review.key, review.revision, review.selector, review.content_sha256, review.size_bytes),
    )
    mutation = project_checkpoint_acceptance_mutation(
        mutation_allocation(before), decision, artifacts, TaskId("project-task"), HostId("host-a")
    )
    barrier.wait()
    with store.write() as transaction:
        result = transaction.commit(mutation)
    results.put(result.code.value if isinstance(result, DecisionFailure) else "committed")


def _commit_same_definition_revision(
    database_path: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    store = SQLiteWorkStore(Path(database_path))
    before = store.snapshot()
    snapshot = project_decision_snapshot(before, SQLITE_NOW)
    actor = decision_models.ActorAuthority(decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0)
    actions = expect_success(available_actions(snapshot, actor))
    action = next(
        value
        for value in actions
        if value.kind == decision_models.ActionKind.REVISE_ITEM and value.capability.subject == ItemId("work-a")
    )
    assert isinstance(action, decision_models.ReviseItemAction)
    current = next(value for value in before.lifecycle.definition_revisions if value.item_id == ItemId("work-a"))
    revised = replace(
        current.definition,
        objective="Commit exactly one concurrent definition revision.",
        dependencies=(ItemId("intake-work"),),
    )
    selected_command = decision_models.ReviseItemCommand(
        action,
        work_models.ReviseItemDefinitionInput(
            ItemId("work-a"),
            current.revision,
            current.digest,
            TaskId("concurrent-owner"),
            "Exercise concurrent revision fencing.",
            revised,
        ),
    )
    decision = expect_success(decide(snapshot, selected_command, SQLITE_NOW))
    assert isinstance(decision, decision_models.TransitionDecision)
    mutation = project_transition_mutation(
        mutation_allocation(before), decision, TaskId("project-task"), HostId("host-a")
    )
    barrier.wait()
    with store.write() as transaction:
        result = transaction.commit(mutation)
    results.put(result.code.value if isinstance(result, DecisionFailure) else "committed")


def _acquire_same_preparation(
    database_path: str,
    lease_id: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    store = SQLiteWorkStore(Path(database_path))
    snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
    item = snapshot.item(ItemId("work-c"))
    definition = snapshot.definition(ItemId("work-c"))
    assert item is not None
    assert definition is not None
    operation = authority_models.AcquireInitialPreparationAuthority(
        snapshot.host_epoch,
        item.item,
        snapshot.revision,
        snapshot.subject_revision(item.item) or "",
        definition.revision,
        definition.digest,
        TaskId(f"preparer-{lease_id}"),
        HostId("host-a"),
        LeaseId(lease_id),
        SQLITE_NOW,
        SQLITE_NOW + timedelta(minutes=1),
    )
    barrier.wait()
    result = decide_and_commit_preparation_authority_change(store, operation)
    results.put(result.code.value if isinstance(result, DecisionFailure) else "committed")


def _race_preparation_and_prerequisite_proposal(
    database_path: str,
    operation_kind: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    store = SQLiteWorkStore(Path(database_path))
    if operation_kind == "preparation":
        snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
        item = snapshot.item(ItemId("work-c"))
        definition = snapshot.definition(ItemId("work-c"))
        assert item is not None
        assert definition is not None
        operation = authority_models.AcquireInitialPreparationAuthority(
            snapshot.host_epoch,
            item.item,
            snapshot.revision,
            snapshot.subject_revision(item.item) or "",
            definition.revision,
            definition.digest,
            TaskId("preparer"),
            HostId("host-a"),
            LeaseId("preparation-a"),
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=1),
        )
        barrier.wait()
        result = decide_and_commit_preparation_authority_change(store, operation)
    else:
        intake = ProposalIntake(
            ProposalId("required-before-work-c"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Required before Work C",
            "Work C needs one newly discovered prerequisite.",
            "The dependency must be preserved before activation.",
            "Record the prerequisite and relationship.",
            "A task can evaluate it.",
            work_models.PrerequisiteProposalRelation(ItemId("work-c")),
            "The relationship is current.",
            ("source:local",),
            ("Work C remains ready.",),
        )
        barrier.wait()
        result = create_proposal(
            store,
            CreateProposalOperation(intake),
            SQLITE_NOW,
            actor_task_id=TaskId("discovering-task"),
            actor_host_id=HostId("host-a"),
        )
    results.put(result.code.value if isinstance(result, DecisionFailure) else "committed")


def _activate_same_prepared_item(
    database_path: str,
    barrier: Barrier,
    results: multiprocessing.queues.Queue[str],
) -> None:
    store = SQLiteWorkStore(Path(database_path))
    snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
    authority = snapshot.command_preparation_authorities[0]
    actor = decision_models.ActorAuthority(
        decision_models.Role.PREPARER,
        decision_models.AuthorizationKind.PREPARATION,
        authority.generation,
        authority.lease_id,
        preparations=(authority.item,),
    )
    actions = expect_success(available_actions(snapshot, actor))
    action = next(value for value in actions if value.kind == decision_models.ActionKind.ACTIVATE)
    assert isinstance(action, decision_models.ActivateAction)
    state_artifact_ref_id = store.snapshot().artifact_references[0].artifact_ref_id
    selected_command = decision_models.ActivateCommand(
        action,
        work_models.ActivateInput(
            AttemptId("work-c-1"),
            "codex/work-c",
            "candidate-base",
            "worker-task",
            state_artifact_ref_id,
        ),
    )
    identity = WorkBriefIdentity(
        "work-c-1",
        "work-c",
        "codex/work-c",
        "candidate-base",
        authority.definition_revision,
        authority.definition_digest,
    )
    barrier.wait()
    result = decide_and_commit_transition(
        store,
        selected_command,
        SQLITE_NOW + timedelta(seconds=1),
        actor_task_id=None,
        actor_host_id=None,
        transition_brief_identity=identity,
    )
    results.put(result.code.value if isinstance(result, DecisionFailure) else f"committed:{state_artifact_ref_id}")


class SQLiteConcurrencyTest(unittest.TestCase):
    def test_concurrent_activation_consumes_preparation_and_creates_one_attempt(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        state = complete_sqlite_state()
        definition = next(value for value in state.lifecycle.definition_revisions if value.item_id == ItemId("work-c"))
        state = replace(
            state,
            authority=replace(
                state.authority,
                preparation_counters=(stored_state.PreparationLeaseCounter(ItemId("work-c"), 1),),
                preparation_generations=(
                    stored_state.PreparationLeaseGeneration(
                        ItemId("work-c"),
                        1,
                        LeaseId("preparation-c"),
                        TaskId("preparer"),
                        HostId("host-a"),
                    ),
                ),
                preparation_leases=(
                    stored_state.StoredPreparationLease(
                        ItemId("work-c"),
                        1,
                        definition.revision,
                        definition.digest,
                        SQLITE_NOW,
                        SQLITE_NOW + timedelta(minutes=1),
                        authority_models.PreparationLeaseStatus.ACTIVE,
                    ),
                ),
            ),
        )
        initialize_store(store, state)

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(target=_activate_same_prepared_item, args=(str(roots.database_path), barrier, results))
            for _ in range(2)
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        observed = (results.get(), results.get())
        self.assertEqual(1, sum(value.startswith("committed:") for value in observed))
        self.assertIn("ACTION_NOT_AVAILABLE", observed)
        after = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertEqual(authority_models.PreparationLeaseStatus.REVOKED, after.authority.preparation_leases[0].state)
        self.assertEqual(1, sum(value.attempt_id == AttemptId("work-c-1") for value in after.lifecycle.attempts))

    def test_preparation_acquisition_and_prerequisite_proposal_serialize_to_one_winner(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        before = store.snapshot()

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(
                target=_race_preparation_and_prerequisite_proposal,
                args=(str(roots.database_path), operation_kind, barrier, results),
            )
            for operation_kind in ("preparation", "proposal")
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        self.assertCountEqual(("committed", "ACTION_NOT_AVAILABLE"), (results.get(), results.get()))
        after = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        preparation_won = bool(after.authority.preparation_leases)
        proposal_won = any(
            value.proposal_id == ProposalId("required-before-work-c") for value in after.proposals.proposals
        )
        self.assertNotEqual(preparation_won, proposal_won)

    def test_concurrent_initial_preparation_acquisition_has_one_winner(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        before = store.snapshot()

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(
                target=_acquire_same_preparation,
                args=(str(roots.database_path), f"preparation-{index}", barrier, results),
            )
            for index in range(2)
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        self.assertCountEqual(("committed", "ACTION_NOT_AVAILABLE"), (results.get(), results.get()))
        after = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(1, len(after.authority.preparation_leases))
        self.assertEqual(1, after.authority.preparation_counters[0].generation_high_water)

    def test_concurrent_same_action_commits_once_and_rejects_stale_writer(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        state = complete_sqlite_state()
        initialize_store(store, state)

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(target=_commit_same_pause, args=(str(roots.database_path), barrier, results))
            for _ in range(2)
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        self.assertCountEqual(("committed", "ACTION_NOT_AVAILABLE"), (results.get(), results.get()))
        self.assertEqual(13, store.snapshot().lifecycle.project.revision)

    def test_concurrent_rebind_commits_lineage_and_fence_once(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        state = complete_sqlite_state()
        current_definition = next(
            value
            for value in state.lifecycle.definition_revisions
            if value.item_id == ItemId("work-a") and value.revision == 1
        )
        revised_definition = replace(current_definition.definition, title="Revised work A")
        revised_digest = expect_success(work_item_definition_digest(revised_definition))
        current_definition = replace(
            current_definition,
            revision=2,
            digest=revised_digest,
            definition=revised_definition,
            before_digest=current_definition.digest,
            after_digest=revised_digest,
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
                definition_revisions=(*state.lifecycle.definition_revisions, current_definition),
            ),
            artifact_references=(*state.artifact_references, replacement),
        )
        initialize_store(store, state)

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(target=_commit_same_rebind, args=(str(roots.database_path), barrier, results))
            for _ in range(2)
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        self.assertCountEqual(("committed", "ACTION_NOT_AVAILABLE"), (results.get(), results.get()))
        rebound = SQLiteWorkStore(roots.database_path).snapshot()
        self.assertEqual(13, rebound.lifecycle.project.revision)
        self.assertEqual(
            ("codex/corrected-work-a", "corrected-base", 99, 2, revised_digest),
            (
                rebound.lifecycle.attempts[0].branch,
                rebound.lifecycle.attempts[0].base_revision,
                rebound.lifecycle.attempts[0].brief_artifact_ref_id,
                rebound.lifecycle.attempts[0].accepted_scope_revision,
                rebound.lifecycle.attempts[0].accepted_scope_digest,
            ),
        )
        self.assertEqual(4, rebound.authority.attempt_counters[0].generation_high_water)
        self.assertEqual(authority_models.AttemptLeaseStatus.REVOKED, rebound.authority.attempt_leases[0].state)

    def test_concurrent_checkpoint_acceptance_commits_both_references_once(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        state = complete_sqlite_state()
        initialize_store(store, state)
        snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
        authority = snapshot.command_attempt_authorities[0]
        actor = decision_models.ActorAuthority(
            decision_models.Role.WORKER,
            decision_models.AuthorizationKind.ATTEMPT,
            authority.generation,
            authority.lease_id,
            (authority.attempt,),
            False,
        )
        submit_action = next(
            value
            for value in expect_success(available_actions(snapshot, actor))
            if value.kind == decision_models.ActionKind.SUBMIT_REVIEW
        )
        assert isinstance(submit_action, decision_models.SubmitReviewAction)
        submit_command = decision_models.SubmitReviewCommand(
            submit_action, work_models.SubmitReviewInput(CandidateId("candidate-a"))
        )
        submit_decision = expect_success(decide(snapshot, submit_command, SQLITE_NOW))
        assert isinstance(submit_decision, decision_models.TransitionDecision)
        state = store.snapshot()
        with store.write() as transaction:
            transaction.commit(project_transition_mutation(mutation_allocation(state), submit_decision))
        state = store.snapshot()

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(
                target=_commit_same_checkpoint,
                args=(str(project), str(roots.database_path), barrier, results),
            )
            for _ in range(2)
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        self.assertCountEqual(("committed", "ACTION_NOT_AVAILABLE"), (results.get(), results.get()))
        reloaded = store.snapshot()
        self.assertEqual(state.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        self.assertEqual(len(state.artifact_references) + 2, len(reloaded.artifact_references))
        self.assertEqual(len(state.transition_receipts) + 1, len(reloaded.transition_receipts))
        attempt = next(value for value in reloaded.lifecycle.attempts if value.attempt_id == AttemptId("work-a-1"))
        self.assertEqual(work_models.AttemptState.PAUSED, attempt.state)
        self.assertIsNotNone(attempt.result_artifact_ref_id)
        self.assertIsNotNone(reloaded.transition_receipts[-1].artifact_ref_id)

    def test_concurrent_definition_revision_commits_one_history_and_dependency_replacement(self) -> None:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        before = complete_sqlite_state()
        initialize_store(store, before)

        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = tuple(
            context.Process(
                target=_commit_same_definition_revision,
                args=(str(roots.database_path), barrier, results),
            )
            for _ in range(2)
        )
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(0, worker.exitcode)

        self.assertCountEqual(("committed", "ACTION_NOT_AVAILABLE"), (results.get(), results.get()))
        reloaded = store.snapshot()
        revisions = tuple(
            value for value in reloaded.lifecycle.definition_revisions if value.item_id == ItemId("work-a")
        )
        self.assertEqual((1, 2), tuple(value.revision for value in revisions))
        self.assertEqual((ItemId("intake-work"),), revisions[-1].definition.dependencies)
        self.assertEqual(work_item_definition_digest(revisions[-1].definition), revisions[-1].digest)
        self.assertEqual(before.lifecycle.project.revision + 1, reloaded.lifecycle.project.revision)
        self.assertEqual(len(before.transition_receipts) + 1, len(reloaded.transition_receipts))
        self.assertEqual(
            decision_models.ActionKind.REVISE_ITEM,
            reloaded.transition_receipts[-1].action_kind,
        )


if __name__ == "__main__":
    unittest.main()
