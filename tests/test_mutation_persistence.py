import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import stored_state
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.mutation_models import (
    AttemptAuthorityMutation,
    MutationReceipt,
)
from pinboard.application.mutations import project_transition_mutation
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import available_actions as available_actions_outcome
from pinboard.domain.decisions import decide as decision_outcome
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.history import work_item_definition_digest
from pinboard.domain.identifiers import (
    ActionId,
    ArtifactRefId,
    AttemptId,
    HistoryId,
    HistorySubjectId,
    HostId,
    ItemId,
    LeaseId,
    TaskId,
)
from pinboard.domain.ledger import LedgerSnapshot
from pinboard.interfaces.transition_input import parse_transition_command
from tests.domain_support import expect_success, expect_transition_command
from tests.support import (
    SQLITE_NOW,
    complete_sqlite_state,
    initialize_store,
    reject_table_deletes,
    reject_table_inserts,
)


def available_actions(
    snapshot: LedgerSnapshot, actor: decision_models.ActorAuthority
) -> tuple[decision_models.Action, ...]:
    return expect_success(available_actions_outcome(snapshot, actor))


def decide(
    snapshot: LedgerSnapshot, command: decision_models.TransitionCommand, now: datetime
) -> decision_models.TransitionDecision:
    result = expect_success(decision_outcome(snapshot, command, now))
    if not isinstance(result, decision_models.TransitionDecision):
        raise AssertionError(f"Expected a non-checkpoint decision, received {result!r}")
    return result


class MutationPersistenceTest(unittest.TestCase):
    def _store(self) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, complete_sqlite_state())
        return store

    def _store_pair(self) -> tuple[SQLiteWorkStore, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        first = SQLiteWorkStore(roots.database_path)
        initialize_store(first, complete_sqlite_state())
        return first, SQLiteWorkStore(roots.database_path)

    def _store_with_state(self, state: stored_state.StoredWorkState) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state)
        return store

    def _attempt_renewal_decision(
        self, before: stored_state.StoredWorkState
    ) -> authority_models.AttemptAuthorityDecision:
        snapshot = project_decision_snapshot(before, SQLITE_NOW)
        command = snapshot.command_attempt_authorities[0]
        stored = before.authority.attempt_leases[0]
        retained = authority_models.AttemptLeaseAuthority(
            command.host_epoch,
            command.attempt,
            command.item,
            command.task_id,
            command.host_id,
            command.lease_id,
            command.generation,
            stored.acquired_at,
            command.expires_at,
            authority_models.AttemptLeaseStatus.ACTIVE,
        )
        return authority_models.AttemptAuthorityDecision(
            command.attempt,
            command.generation,
            command.generation,
            retained,
            replace(retained, expires_at=retained.expires_at + timedelta(minutes=1)),
        )

    def _receipt_state(
        self, before: stored_state.StoredWorkState, action: str
    ) -> tuple[decision_models.TransitionReceipt, stored_state.StoredWorkState]:
        decided_at = before.lifecycle.project.updated_at + timedelta(seconds=1)
        receipt = decision_models.TransitionReceipt(ActionId(action), ItemId("work-a"), action, None, decided_at)
        stored_receipt = stored_state.StoredTransitionReceipt(
            HistoryId(1 + max((int(value.history_id) for value in before.transition_receipts), default=0)),
            before.lifecycle.project.revision + 1,
            receipt.action_id,
            decision_models.ActionKind.INSPECT,
            HistorySubjectId("work-a"),
            None,
            decision_models.AuthorizationKind.PROJECT,
            None,
            None,
            "mutation/v1",
            work_models.CanonicalJson(b"{}"),
            "transition-receipt/v1",
            work_models.CanonicalJson(
                json.dumps(
                    {"evidence": receipt.evidence, "outcome": receipt.outcome},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            decided_at,
        )
        after = replace(
            before,
            lifecycle=replace(
                before.lifecycle,
                project=replace(
                    before.lifecycle.project,
                    revision=before.lifecycle.project.revision + 1,
                    updated_at=decided_at,
                ),
            ),
            transition_receipts=(*before.transition_receipts, stored_receipt),
        )
        return receipt, after

    def _stored_receipt(self, after: stored_state.StoredWorkState) -> stored_state.StoredTransitionReceipt:
        return after.transition_receipts[-1]

    def _mutation_receipt(
        self, receipt: decision_models.TransitionReceipt, after: stored_state.StoredWorkState
    ) -> MutationReceipt:
        stored = self._stored_receipt(after)
        return MutationReceipt(
            receipt,
            stored.history_id,
            stored.project_revision,
            stored.action_kind,
            stored.subject_id,
            stored.artifact_ref_id,
            stored.authorization,
            stored.actor_task_id,
            stored.actor_host_id,
            stored.input_schema,
            stored.input_payload,
        )

    def test_activation_decision_retains_and_persists_creation_facts(self) -> None:
        state = complete_sqlite_state()
        definition = next(value for value in state.lifecycle.definition_revisions if value.item_id == ItemId("work-c"))
        state = replace(
            state,
            authority=replace(
                state.authority,
                preparation_counters=(stored_state.PreparationLeaseCounter(ItemId("work-c"), 1),),
                preparation_generations=(
                    stored_state.PreparationLeaseGeneration(
                        ItemId("work-c"), 1, LeaseId("preparation-c"), TaskId("preparer-c"), HostId("host-a")
                    ),
                ),
                preparation_leases=(
                    stored_state.StoredPreparationLease(
                        ItemId("work-c"),
                        1,
                        definition.revision,
                        definition.digest,
                        SQLITE_NOW,
                        SQLITE_NOW + timedelta(minutes=5),
                        authority_models.PreparationLeaseStatus.ACTIVE,
                    ),
                ),
            ),
        )
        store = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PREPARER,
            decision_models.AuthorizationKind.PREPARATION,
            1,
            LeaseId("preparation-c"),
            preparations=(ItemId("work-c"),),
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.ACTIVATE
        )
        assert isinstance(action, decision_models.ActivateAction)
        decided_at = SQLITE_NOW + timedelta(seconds=1)
        decision = decide(
            snapshot,
            decision_models.ActivateCommand(
                action,
                work_models.ActivateInput(
                    AttemptId("work-c-1"),
                    "codex/work-c",
                    "base-c",
                    "worker-c",
                    ArtifactRefId(1),
                ),
            ),
            decided_at,
        )

        self.assertIsInstance(decision.change, decision_models.ActivationChange)
        assert isinstance(decision.change, decision_models.ActivationChange)
        self.assertEqual(
            ("codex/work-c", "base-c", "worker-c", ArtifactRefId(1)),
            (
                decision.change.branch,
                decision.change.base_revision,
                decision.change.owner,
                decision.change.brief_artifact_ref_id,
            ),
        )
        before_commit = store.snapshot()
        with reject_table_inserts("attempts"), self.assertRaises(StorageError), store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))
        self.assertEqual(before_commit, store.snapshot())
        with reject_table_deletes("work_items"), store.write() as transaction:
            transaction.commit(project_transition_mutation(transaction.snapshot(), decision))

        reopened = store.snapshot()
        attempt = next(value for value in reopened.lifecycle.attempts if value.attempt_id == AttemptId("work-c-1"))
        self.assertEqual(
            ("codex/work-c", "base-c", "worker-c", ArtifactRefId(1)),
            (attempt.branch, attempt.base_revision, attempt.provenance, attempt.brief_artifact_ref_id),
        )
        self.assertEqual(13, reopened.lifecycle.project.revision)
        self.assertEqual(2, len(reopened.transition_receipts))
        self.assertEqual("transition-receipt/v1", reopened.transition_receipts[-1].outcome_schema)
        self.assertEqual(TaskId("preparer-c"), reopened.transition_receipts[-1].actor_task_id)
        self.assertEqual(HostId("host-a"), reopened.transition_receipts[-1].actor_host_id)
        self.assertEqual(
            authority_models.PreparationLeaseStatus.REVOKED, reopened.authority.preparation_leases[0].state
        )

    def test_definition_revision_persists_atomically_and_reloads_every_audit_fact(self) -> None:
        state = complete_sqlite_state()
        current = work_models.WorkItemDefinition(
            "Work work-a",
            "Make the state explicit.",
            "The workflow needs this fact.",
            ("artifacts/design.md",),
            ("The state becomes explicit.",),
            (),
            ("The next decision can run.",),
            (ItemId("work-c"),),
            "The state becomes explicit.",
            "The next decision can run.",
        )
        current_digest = expect_success(work_item_definition_digest(current))
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                definition_revisions=(
                    *(value for value in state.lifecycle.definition_revisions if value.item_id != ItemId("work-a")),
                    stored_state.ItemDefinitionRevision(
                        ItemId("work-a"),
                        1,
                        current_digest,
                        current,
                        "Accepted proposal definition.",
                        TaskId("proposal-source"),
                        None,
                        current_digest,
                        3,
                        SQLITE_NOW,
                    ),
                ),
                attempts=(replace(state.lifecycle.attempts[0], accepted_scope_digest=current_digest),),
            ),
        )
        store = self._store_with_state(state)
        before = store.snapshot()
        snapshot = project_decision_snapshot(before, SQLITE_NOW)
        revised = replace(
            current,
            objective="Make the state explicit and observable.",
            dependencies=(ItemId("intake-work"),),
        )
        decided_at = SQLITE_NOW + timedelta(seconds=1)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT,
            decision_models.AuthorizationKind.PROJECT,
            0,
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.REVISE_ITEM and value.capability.subject == ItemId("work-a")
        )
        assert isinstance(action, decision_models.ReviseItemAction)
        decision = decide(
            snapshot,
            decision_models.ReviseItemCommand(
                action,
                work_models.ReviseItemDefinitionInput(
                    ItemId("work-a"),
                    1,
                    current_digest,
                    TaskId("revision-owner"),
                    "Clarify observability and replace the prerequisite.",
                    revised,
                ),
            ),
            decided_at,
        )

        with store.write() as transaction:
            committed = transaction.commit(
                project_transition_mutation(transaction.snapshot(), decision, TaskId("project-task"), HostId("host-a"))
            )

        self.assertNotIsInstance(committed, DecisionFailure)
        assert not isinstance(committed, DecisionFailure)
        self.assertEqual(ActionId("revise-item:work-a"), committed.transition.action_id)
        reopened = store.snapshot()
        reopened_item = project_decision_snapshot(reopened, SQLITE_NOW).item(ItemId("work-a"))
        assert reopened_item is not None
        self.assertEqual(work_models.WorkState.ACTIVE, reopened_item.state)
        self.assertEqual(
            2,
            sum(value.item_id == ItemId("work-a") for value in reopened.lifecycle.definition_revisions),
        )
        persisted = next(
            value for value in reversed(reopened.lifecycle.definition_revisions) if value.item_id == ItemId("work-a")
        )
        self.assertEqual(revised, persisted.definition)
        self.assertEqual(current_digest, persisted.before_digest)
        self.assertEqual(expect_success(work_item_definition_digest(revised)), persisted.after_digest)
        self.assertEqual(TaskId("revision-owner"), persisted.source_task_id)
        self.assertEqual("Clarify observability and replace the prerequisite.", persisted.reason)
        self.assertEqual(13, persisted.accepted_project_revision)
        self.assertEqual(decided_at, persisted.accepted_at)
        self.assertEqual(
            (ItemId("intake-work"),),
            tuple(
                value.dependency_id for value in reopened.lifecycle.dependencies if value.item_id == ItemId("work-a")
            ),
        )

    def test_resume_replaces_the_attempt_brief_and_reloads_it_from_sqlite(self) -> None:
        state = complete_sqlite_state()
        current_definition = next(
            value
            for value in state.lifecycle.definition_revisions
            if value.item_id == state.lifecycle.attempts[0].item_id
        )
        revised_definition = replace(current_definition.definition, dependencies=())
        revised_scope_digest = expect_success(work_item_definition_digest(revised_definition))
        current = state.artifact_references[0]
        replacement = replace(
            current,
            artifact_ref_id=ArtifactRefId(99),
            revision=current.revision + 1,
            selector="artifacts/briefs/work-a/2.json",
            content_sha256="b" * 64,
            size_bytes=23,
        )
        attempt = state.lifecycle.attempts[0]
        items = tuple(
            replace(
                item,
                state=stored_state.StoredWorkItemState.PAUSED,
            )
            if item.item_id == attempt.item_id
            else item
            for item in state.lifecycle.work_items
        )
        state = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=items,
                definition_revisions=(
                    *state.lifecycle.definition_revisions,
                    stored_state.ItemDefinitionRevision(
                        attempt.item_id,
                        2,
                        revised_scope_digest,
                        revised_definition,
                        "Revised test definition.",
                        TaskId("test-source"),
                        current_definition.digest,
                        revised_scope_digest,
                        state.lifecycle.project.revision,
                        SQLITE_NOW,
                    ),
                ),
                dependencies=tuple(value for value in state.lifecycle.dependencies if value.item_id != attempt.item_id),
                attempts=(replace(attempt, state=work_models.AttemptState.PAUSED),),
            ),
            artifact_references=(*state.artifact_references, replacement),
        )
        store = self._store_with_state(state)
        snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
        )
        action = next(
            value for value in available_actions(snapshot, actor) if value.kind == decision_models.ActionKind.RESUME
        )
        assert isinstance(action, decision_models.ResumeAction)
        decision = decide(
            snapshot,
            decision_models.ResumeCommand(action, work_models.ResumeInput(replacement.artifact_ref_id)),
            SQLITE_NOW,
        )

        with store.write() as transaction:
            transaction.commit(
                project_transition_mutation(transaction.snapshot(), decision, TaskId("project-task"), HostId("host-a"))
            )

        persisted = next(
            value for value in store.snapshot().lifecycle.attempts if value.attempt_id == attempt.attempt_id
        )
        self.assertEqual(work_models.AttemptState.ACTIVE, persisted.state)
        self.assertEqual(replacement.artifact_ref_id, persisted.brief_artifact_ref_id)
        self.assertEqual(2, persisted.accepted_scope_revision)
        self.assertEqual(revised_scope_digest, persisted.accepted_scope_digest)

    def test_proposal_acceptance_round_trips_semantics_and_ordered_dependencies(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.ACCEPT_PROPOSAL
        )
        assert isinstance(action, decision_models.AcceptProposalAction)
        decision = decide(
            snapshot,
            decision_models.AcceptProposalCommand(
                action,
                work_models.AcceptProposalInput(
                    ItemId("zz-proposal-a"),
                    work_models.AcceptedProposalState.READY,
                    "activate",
                    timing=None,
                    depends_on=(ItemId("intake-work"),),
                ),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )

        self.assertIsInstance(decision.change, decision_models.AcceptedProposalChange)
        with reject_table_deletes("work_items"), store.write() as transaction:
            transaction.commit(
                project_transition_mutation(transaction.snapshot(), decision, TaskId("project-task"), HostId("host-a"))
            )

        reopened = store.snapshot()
        item = next(value for value in reopened.lifecycle.work_items if value.item_id == ItemId("zz-proposal-a"))
        proposal = reopened.proposals.proposals[0]
        accepted_definition = next(
            value.definition
            for value in reversed(reopened.lifecycle.definition_revisions)
            if value.item_id == item.item_id
        )
        self.assertEqual(
            ("Proposal A", "A related observation", "Record the follow-up."),
            (accepted_definition.title, proposal.trigger, accepted_definition.effect),
        )
        self.assertEqual(stored_state.StoredWorkItemState.READY, item.state)
        self.assertEqual(4, item.queue_position)
        self.assertEqual(5, len(reopened.lifecycle.work_items))
        self.assertEqual(
            (ItemId("work-c"), ItemId("intake-work")),
            tuple(value.dependency_id for value in reopened.lifecycle.dependencies if value.item_id == item.item_id),
        )
        definitions = tuple(value for value in reopened.lifecycle.definition_revisions if value.item_id == item.item_id)
        self.assertEqual((1, 2), tuple(value.revision for value in definitions))
        self.assertEqual((ItemId("work-c"), ItemId("intake-work")), definitions[-1].definition.dependencies)
        self.assertEqual("Accepted explicit proposal dependencies.", definitions[-1].reason)
        self.assertEqual(TaskId("source-task"), definitions[-1].source_task_id)
        self.assertEqual(
            work_models.AcceptedProposalDisposition(
                ItemId("zz-proposal-a"),
                SQLITE_NOW + timedelta(seconds=1),
            ),
            proposal.disposition,
        )
        self.assertEqual(13, reopened.lifecycle.project.revision)
        self.assertEqual(2, len(reopened.transition_receipts))

    def test_cross_family_stale_mutation_is_rejected_without_partial_state(self) -> None:
        first, second = self._store_pair()
        before = first.snapshot()
        self.assertEqual(before, second.snapshot())
        snapshot = project_decision_snapshot(before, SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.DEFER and value.capability.subject == ItemId("intake-work")
        )
        assert isinstance(action, decision_models.DeferAction)
        first_decision = decide(
            snapshot,
            decision_models.DeferCommand(
                action, work_models.DeferInput(work_models.Timing.SAFE_TO_DEFER, "First writer commits.")
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )
        first_mutation = project_transition_mutation(before, first_decision, TaskId("first-task"), HostId("host-a"))
        with first.write() as transaction:
            transaction.commit(first_mutation)
        after = first.snapshot()

        stale_receipt, stale_after = self._receipt_state(before, "inspect:stale-attempt-authority")
        stale_mutation = AttemptAuthorityMutation(
            self._mutation_receipt(stale_receipt, stale_after),
            self._attempt_renewal_decision(before),
        )
        with second.write() as transaction:
            rejected = transaction.commit(stale_mutation)
        self.assertIsInstance(rejected, DecisionFailure)
        assert isinstance(rejected, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, rejected.code)
        reloaded = second.snapshot()
        self.assertEqual(after, reloaded)
        self.assertEqual(before.authority.attempt_counters, reloaded.authority.attempt_counters)
        self.assertEqual(before.authority.attempt_generations, reloaded.authority.attempt_generations)
        self.assertEqual(before.authority.attempt_leases, reloaded.authority.attempt_leases)

    def test_proposal_disposition_matrix_persists_each_closed_value(self) -> None:
        for action_type, payload, expected in (
            (
                decision_models.MergeProposalAction,
                b'{"target":"work-c"}',
                work_models.MergedProposalDisposition(
                    ItemId("work-c"),
                    SQLITE_NOW + timedelta(seconds=1),
                ),
            ),
            (
                decision_models.ReturnProposalAction,
                b'{"reason":"Clarify the evidence."}',
                work_models.ReturnedProposalDisposition(
                    "Clarify the evidence.",
                    SQLITE_NOW + timedelta(seconds=1),
                ),
            ),
            (
                decision_models.RejectProposalAction,
                b'{"reason":"The proposal is obsolete."}',
                work_models.RejectedProposalDisposition(
                    "The proposal is obsolete.",
                    SQLITE_NOW + timedelta(seconds=1),
                ),
            ),
        ):
            with self.subTest(action_type=action_type.__name__):
                store = self._store()
                snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
                actor = decision_models.ActorAuthority(
                    decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
                )
                action = next(value for value in available_actions(snapshot, actor) if isinstance(value, action_type))
                decision = decide(
                    snapshot,
                    expect_transition_command(parse_transition_command(action, payload)),
                    SQLITE_NOW + timedelta(seconds=1),
                )
                with store.write() as transaction:
                    transaction.commit(
                        project_transition_mutation(
                            transaction.snapshot(), decision, TaskId("project-task"), HostId("host-a")
                        )
                    )
                proposal = store.snapshot().proposals.proposals[0]
                self.assertEqual(expected, proposal.disposition)

    def test_defer_decision_persists_reopen_state(self) -> None:
        store = self._store()
        snapshot = project_decision_snapshot(store.snapshot(), SQLITE_NOW)
        actor = decision_models.ActorAuthority(
            decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
        )
        action = next(
            value
            for value in available_actions(snapshot, actor)
            if value.kind == decision_models.ActionKind.DEFER and value.capability.subject == ItemId("intake-work")
        )
        assert isinstance(action, decision_models.DeferAction)
        decision = decide(
            snapshot,
            decision_models.DeferCommand(
                action,
                work_models.DeferInput(work_models.Timing.SAFE_TO_DEFER, "Reopen when the prerequisite is accepted."),
            ),
            SQLITE_NOW + timedelta(seconds=1),
        )
        with store.write() as transaction:
            transaction.commit(
                project_transition_mutation(transaction.snapshot(), decision, TaskId("project-task"), HostId("host-a"))
            )
        reopened = store.snapshot()
        self.assertEqual(stored_state.StoredWorkItemState.DEFERRED, reopened.lifecycle.work_items[0].state)

    def test_real_stale_transition_effects_return_failure_and_roll_back(self) -> None:
        ignore_item_update = """
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            BEFORE UPDATE OF state ON work_items
            BEGIN
                SELECT RAISE(IGNORE);
            END
        """
        stale_attempt_after_item = """
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            AFTER UPDATE OF state ON work_items
            BEGIN
                UPDATE attempts SET subject_revision = subject_revision + 1
                WHERE attempt_id = 'work-a-1';
            END
        """
        stale_proposal_after_item = f"""
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            AFTER UPDATE OF state ON work_items
            BEGIN
                UPDATE proposals
                SET disposition = 'returned', disposition_reason = 'Competing disposition.',
                    disposition_recorded_at = '{SQLITE_NOW.isoformat()}'
                WHERE proposal_id = 'zz-proposal-a';
            END
        """
        ignore_proposal_update = """
            CREATE TEMP TRIGGER arrange_real_transition_staleness
            BEFORE UPDATE OF disposition ON proposals
            BEGIN
                SELECT RAISE(IGNORE);
            END
        """
        scenarios: tuple[tuple[type[decision_models.Action], bytes, str], ...] = (
            (
                decision_models.PauseAction,
                b'{"reason":"Pause at the checkpoint boundary."}',
                ignore_item_update,
            ),
            (
                decision_models.PauseAction,
                b'{"reason":"Pause at the checkpoint boundary."}',
                stale_attempt_after_item,
            ),
            (
                decision_models.AcceptProposalAction,
                b'{"item":"zz-proposal-a","state":"ready","next_action":"activate","depends_on":["intake-work"]}',
                ignore_item_update,
            ),
            (
                decision_models.AcceptProposalAction,
                b'{"item":"zz-proposal-a","state":"ready","next_action":"activate","depends_on":["intake-work"]}',
                stale_proposal_after_item,
            ),
            (
                decision_models.MergeProposalAction,
                b'{"target":"work-c"}',
                ignore_item_update,
            ),
            (
                decision_models.MergeProposalAction,
                b'{"target":"work-c"}',
                stale_proposal_after_item,
            ),
            (
                decision_models.ReturnProposalAction,
                b'{"reason":"Clarify the evidence."}',
                ignore_proposal_update,
            ),
            (
                decision_models.RejectProposalAction,
                b'{"reason":"The proposal is obsolete."}',
                ignore_item_update,
            ),
            (
                decision_models.RejectProposalAction,
                b'{"reason":"The proposal is obsolete."}',
                stale_proposal_after_item,
            ),
        )
        for action_type, payload, trigger in scenarios:
            with self.subTest(action_type=action_type.__name__, trigger=trigger):
                project = Path(tempfile.mkdtemp()).resolve()
                roots = resolve_durable_roots(project)
                initialize_database(roots, SQLITE_NOW)
                store = SQLiteWorkStore(roots.database_path)
                initialize_store(store, complete_sqlite_state())
                before = store.snapshot()
                snapshot = project_decision_snapshot(before, SQLITE_NOW)
                actor = decision_models.ActorAuthority(
                    decision_models.Role.PROJECT,
                    decision_models.AuthorizationKind.PROJECT,
                    0,
                )
                action = next(value for value in available_actions(snapshot, actor) if isinstance(value, action_type))
                decision = decide(
                    snapshot,
                    expect_transition_command(parse_transition_command(action, payload)),
                    SQLITE_NOW + timedelta(seconds=1),
                )
                mutation = project_transition_mutation(before, decision, TaskId("project-task"), HostId("host-a"))
                connection = open_database(roots.database_path, OpenMode.READ_WRITE)
                connection.execute(trigger)

                with (
                    patch("pinboard.adapters.sqlite.store.open_database", return_value=connection),
                    store.write() as transaction,
                ):
                    result = transaction.commit(mutation)

                self.assertIsInstance(result, DecisionFailure)
                assert isinstance(result, DecisionFailure)
                self.assertEqual(DecisionFailureCode.ACTION_NOT_AVAILABLE, result.code)
                self.assertEqual(before, store.snapshot())


if __name__ == "__main__":
    unittest.main()
