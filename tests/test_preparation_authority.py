import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Thread
from unittest.mock import patch

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.files.views import derive_expected_view_bytes
from pinboard.adapters.sqlite.database import initialize_database, open_database
from pinboard.adapters.sqlite.errors import StorageError
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models, service, stored_state
from pinboard.application.actions import discover_actions
from pinboard.application.decision_projection import project_decision_snapshot
from pinboard.application.mutations import project_transition_mutation
from pinboard.application.queries import project_overview, select_parallel_preview
from pinboard.application.service import create_proposal, decide_and_commit_preparation_authority_change
from pinboard.domain import authority_models, decision_models, decisions, work_models
from pinboard.domain.authority_decisions import decide_preparation_authority
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import HostId, ItemId, LeaseId, ProposalId, TaskId
from pinboard.domain.proposal_models import CreateProposalOperation, ProposalIntake
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW, complete_sqlite_state, initialize_store, reject_table_inserts


class PreparationAuthorityTest(unittest.TestCase):
    def test_ordinary_start_selects_definition_after_waiting_for_a_revision_commit(self) -> None:
        store, database_path = self._store()
        before = store.snapshot()
        current = next(value for value in before.lifecycle.definition_revisions if value.item_id == ItemId("work-c"))
        action = next(
            value
            for value in expect_success(discover_actions(before, decision_models.Role.PROJECT, now=SQLITE_NOW))
            if isinstance(value, decision_models.ReviseItemAction) and value.capability.subject == ItemId("work-c")
        )
        assert isinstance(action, decision_models.ReviseItemAction)
        command = decision_models.ReviseItemCommand(
            action,
            work_models.ReviseItemDefinitionInput(
                ItemId("work-c"),
                current.revision,
                current.digest,
                TaskId("definition-owner"),
                "Use the revised definition after the lock is released.",
                replace(current.definition, objective="Revised before preparation acquires the write lock."),
            ),
        )
        decision = expect_success(decisions.decide(project_decision_snapshot(before, SQLITE_NOW), command, SQLITE_NOW))
        assert isinstance(decision, decision_models.TransitionDecision)
        mutation = project_transition_mutation(before, decision, TaskId("definition-owner"), HostId("host-a"))
        started = Event()
        finished = Event()
        results: list[DecisionFailure | authority_models.PreparationLeaseAuthority] = []

        def start() -> None:
            started.set()
            results.append(
                service.start_preparation(
                    SQLiteWorkStore(database_path),
                    item_id=ItemId("work-c"),
                    task_id=TaskId("preparer"),
                    host_id=HostId("host-a"),
                    lease_id=LeaseId("preparation"),
                    acquired_at=SQLITE_NOW,
                    expires_at=SQLITE_NOW + timedelta(minutes=1),
                )
            )
            finished.set()

        with store.write() as transaction:
            thread = Thread(target=start)
            thread.start()
            self.assertTrue(started.wait(timeout=5))
            self.assertFalse(finished.wait(timeout=0.1))
            expect_success(transaction.commit(mutation))
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        acquired = expect_success(results[0])
        after = SQLiteWorkStore(database_path).snapshot()
        revised = next(
            value for value in reversed(after.lifecycle.definition_revisions) if value.item_id == ItemId("work-c")
        )
        self.assertEqual(2, acquired.definition_revision)
        self.assertEqual(revised.digest, acquired.definition_digest)
        self.assertEqual(acquired.definition_digest, after.authority.preparation_leases[0].definition_digest)

    def test_competing_ordinary_starts_commit_one_exact_claim(self) -> None:
        store, database_path = self._store()
        before = store.snapshot()
        barrier = Barrier(2)
        results: list[DecisionFailure | authority_models.PreparationLeaseAuthority] = []

        def start(identity: str) -> None:
            barrier.wait(timeout=5)
            results.append(
                service.start_preparation(
                    SQLiteWorkStore(database_path),
                    item_id=ItemId("work-c"),
                    task_id=TaskId(identity),
                    host_id=HostId("host-a"),
                    lease_id=LeaseId(identity),
                    acquired_at=SQLITE_NOW,
                    expires_at=SQLITE_NOW + timedelta(minutes=1),
                )
            )

        threads = [Thread(target=start, args=(identity,)) for identity in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(1, sum(isinstance(value, DecisionFailure) for value in results))
        committed = next(value for value in results if isinstance(value, authority_models.PreparationLeaseAuthority))
        after = SQLiteWorkStore(database_path).snapshot()
        self.assertEqual(before.lifecycle.project.revision + 1, after.lifecycle.project.revision)
        self.assertEqual(1, len(after.authority.preparation_leases))
        self.assertEqual(committed.lease_id, after.authority.preparation_generations[0].lease_id)
        self.assertEqual(committed.definition_digest, after.authority.preparation_leases[0].definition_digest)

    def _store(self, state: stored_state.StoredWorkState | None = None) -> tuple[SQLiteWorkStore, Path]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state if state is not None else complete_sqlite_state())
        return store, roots.database_path

    def _acquisition(
        self,
        state: stored_state.StoredWorkState,
        *,
        expires_at: datetime = SQLITE_NOW + timedelta(minutes=1),
    ) -> authority_models.AcquireInitialPreparationAuthority:
        snapshot = project_decision_snapshot(state, SQLITE_NOW)
        item = snapshot.item(ItemId("work-c"))
        definition = snapshot.definition(ItemId("work-c"))
        assert item is not None
        assert definition is not None
        return authority_models.AcquireInitialPreparationAuthority(
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
            expires_at,
        )

    def test_initial_acquisition_pins_ready_item_definition_and_keeps_item_ready(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        item = snapshot.item(ItemId("work-c"))
        definition = snapshot.definition(ItemId("work-c"))
        assert item is not None
        assert definition is not None

        decision = decide_preparation_authority(
            None,
            0,
            authority_models.AcquireInitialPreparationAuthority(
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
                SQLITE_NOW + timedelta(minutes=5),
            ),
            snapshot,
            SQLITE_NOW,
        )

        self.assertNotIsInstance(decision, DecisionFailure)
        assert not isinstance(decision, DecisionFailure)
        retained_item = snapshot.item(item.item)
        assert retained_item is not None
        self.assertEqual(work_models.WorkState.READY, retained_item.state)
        self.assertEqual(
            (definition.revision, definition.digest),
            (decision.proposed_replacement.definition_revision, decision.proposed_replacement.definition_digest),
        )
        self.assertEqual(authority_models.PreparationLeaseStatus.ACTIVE, decision.proposed_replacement.state)

    def test_initial_acquisition_names_each_mismatched_observed_precondition(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        acquisition = self._acquisition(complete_sqlite_state())
        item = snapshot.item(ItemId("work-c"))
        assert item is not None
        not_ready = replace(item, state=work_models.WorkState.ACTIVE)
        blocked = replace(item, depends_on=(ItemId("work-a"),))
        cases = (
            (
                "project revision",
                snapshot,
                replace(acquisition, expected_project_revision="stale"),
                "Project revision differs from the initial preparation request.",
            ),
            (
                "missing item",
                replace(snapshot, items=tuple(value for value in snapshot.items if value.item != item.item)),
                acquisition,
                "Item 'work-c' does not exist.",
            ),
            (
                "item revision",
                snapshot,
                replace(acquisition, expected_item_subject_revision="stale"),
                "Item 'work-c' subject revision differs from the initial preparation request.",
            ),
            (
                "not ready",
                replace(
                    snapshot, items=tuple(not_ready if value.item == item.item else value for value in snapshot.items)
                ),
                acquisition,
                "Item 'work-c' is not ready.",
            ),
            (
                "live dependency",
                replace(
                    snapshot, items=tuple(blocked if value.item == item.item else value for value in snapshot.items)
                ),
                acquisition,
                "Item 'work-c' has live dependencies.",
            ),
            (
                "missing definition",
                replace(
                    snapshot,
                    definitions=tuple(value for value in snapshot.definitions if value.item != item.item),
                ),
                acquisition,
                "Item 'work-c' has no accepted definition.",
            ),
            (
                "definition revision",
                snapshot,
                replace(acquisition, expected_definition_revision=2),
                "Item 'work-c' definition revision differs from the initial preparation request.",
            ),
            (
                "definition digest",
                snapshot,
                replace(acquisition, expected_definition_digest="stale"),
                "Item 'work-c' definition digest differs from the initial preparation request.",
            ),
        )

        for label, observed, requested, message in cases:
            with self.subTest(precondition=label):
                before = observed
                decision = decide_preparation_authority(None, 0, requested, observed, SQLITE_NOW)

                self.assertEqual(DecisionFailure(DecisionFailureCode.ACTION_NOT_AVAILABLE, message, None), decision)
                self.assertEqual(before, observed)

    def test_initial_acquisition_keeps_internal_host_epoch_rejection(self) -> None:
        state = complete_sqlite_state()
        snapshot = project_decision_snapshot(state, SQLITE_NOW)
        acquisition = replace(self._acquisition(state), host_epoch=snapshot.host_epoch + 1)

        decision = decide_preparation_authority(None, 0, acquisition, snapshot, SQLITE_NOW)

        self.assertEqual(
            DecisionFailure(
                DecisionFailureCode.ACTION_NOT_AVAILABLE,
                "Initial preparation requires the exact dependency-satisfied ready item and definition.",
                None,
            ),
            decision,
        )

    def test_renew_and_release_require_the_exact_live_token(self) -> None:
        current = authority_models.PreparationLeaseAuthority(
            2,
            ItemId("work-c"),
            1,
            "definition-digest",
            TaskId("preparer"),
            HostId("host-a"),
            LeaseId("preparation-a"),
            3,
            SQLITE_NOW,
            SQLITE_NOW + timedelta(minutes=5),
            authority_models.PreparationLeaseStatus.ACTIVE,
        )
        token = work_models.PreparationCommandAuthority(
            current.host_epoch,
            current.item,
            current.definition_revision,
            current.definition_digest,
            current.task_id,
            current.host_id,
            current.lease_id,
            current.generation,
            current.expires_at,
        )

        renewed = decide_preparation_authority(
            current,
            3,
            authority_models.RenewPreparationAuthority(
                token, SQLITE_NOW + timedelta(seconds=1), SQLITE_NOW + timedelta(minutes=6)
            ),
            None,
            SQLITE_NOW + timedelta(seconds=1),
        )
        released = decide_preparation_authority(
            current,
            3,
            authority_models.ReleasePreparationAuthority(token, SQLITE_NOW + timedelta(seconds=1)),
            None,
            SQLITE_NOW + timedelta(seconds=1),
        )
        stale = decide_preparation_authority(
            current,
            3,
            authority_models.RenewPreparationAuthority(
                replace(token, generation=4),
                SQLITE_NOW + timedelta(seconds=1),
                SQLITE_NOW + timedelta(minutes=6),
            ),
            None,
            SQLITE_NOW + timedelta(seconds=1),
        )

        self.assertNotIsInstance(renewed, DecisionFailure)
        self.assertNotIsInstance(released, DecisionFailure)
        self.assertIsInstance(stale, DecisionFailure)

    def test_only_preparer_with_live_authority_receives_activation(self) -> None:
        state = complete_sqlite_state()
        snapshot = project_decision_snapshot(state, SQLITE_NOW)
        definition = snapshot.definition(ItemId("work-c"))
        assert definition is not None
        command = work_models.PreparationCommandAuthority(
            snapshot.host_epoch,
            ItemId("work-c"),
            definition.revision,
            definition.digest,
            TaskId("preparer"),
            HostId("host-a"),
            LeaseId("preparation-a"),
            1,
            SQLITE_NOW + timedelta(minutes=5),
        )
        prepared = replace(
            snapshot,
            preparation_authorities=(
                work_models.PreparationAuthority(
                    command.item,
                    command.definition_revision,
                    command.definition_digest,
                    command.lease_id,
                    command.generation,
                ),
            ),
            command_preparation_authorities=(command,),
        )
        actor = decision_models.ActorAuthority(
            decision_models.Role.PREPARER,
            decision_models.AuthorizationKind.PREPARATION,
            command.generation,
            command.lease_id,
            preparations=(command.item,),
        )

        actions = __import__("pinboard.domain.decisions", fromlist=["available_actions"]).available_actions(
            prepared, actor
        )

        self.assertNotIsInstance(actions, DecisionFailure)
        assert not isinstance(actions, DecisionFailure)
        self.assertEqual(["activate:work-c"], [str(decision_models.action_id(value)) for value in actions])
        self.assertEqual(decision_models.AuthorizationKind.PREPARATION, actions[0].capability.authorization)

    def test_preparation_persists_reloads_and_changes_visibility_exactly_at_expiry(self) -> None:
        store, database_path = self._store()
        expires_at = SQLITE_NOW + timedelta(seconds=1)
        receipt = decide_and_commit_preparation_authority_change(
            store, self._acquisition(store.snapshot(), expires_at=expires_at)
        )
        self.assertNotIsInstance(receipt, DecisionFailure)

        reloaded = SQLiteWorkStore(database_path).snapshot()
        self.assertEqual((ItemId("work-c"),), tuple(value.item_id for value in reloaded.authority.preparation_leases))
        before = project_overview(reloaded, expires_at - timedelta(microseconds=1))
        at = project_overview(reloaded, expires_at)
        before_item = next(value for value in before.items if value.item_id == "work-c")
        at_item = next(value for value in at.items if value.item_id == "work-c")
        assert before_item.preparation is not None
        assert at_item.preparation is not None
        self.assertEqual(authority_models.PreparationLeaseStatus.ACTIVE, before_item.preparation.status)
        self.assertEqual(authority_models.PreparationLeaseStatus.EXPIRED, at_item.preparation.status)
        self.assertNotIn("work-c", before.immediate_options)
        self.assertIn("work-c", at.immediate_options)
        before_parallel = select_parallel_preview(
            store, selected=("work-c",), now=expires_at - timedelta(microseconds=1)
        )
        at_parallel = select_parallel_preview(store, selected=("work-c",), now=expires_at)
        assert not isinstance(before_parallel, query_models.ParallelSelectionInvalid)
        assert not isinstance(at_parallel, query_models.ParallelSelectionInvalid)
        self.assertFalse(before_parallel.safe)
        self.assertTrue(at_parallel.safe)
        before_views = derive_expected_view_bytes(reloaded, now=expires_at - timedelta(microseconds=1))
        at_views = derive_expected_view_bytes(reloaded, now=expires_at)
        self.assertIn(b"- Preparation: active", before_views["items/work-c.md"])
        self.assertIn(b"- Preparation: expired", at_views["items/work-c.md"])
        self.assertNotEqual(before_views["queue.md"], at_views["queue.md"])

    def test_live_preparation_rejects_prerequisite_proposal_atomically_then_expiry_admits_it(self) -> None:
        store, database_path = self._store()
        expires_at = SQLITE_NOW + timedelta(seconds=1)
        acquired = decide_and_commit_preparation_authority_change(
            store, self._acquisition(store.snapshot(), expires_at=expires_at)
        )
        self.assertNotIsInstance(acquired, DecisionFailure)
        intake = ProposalIntake(
            ProposalId("required-before-work-c"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Required before Work C",
            "Work C needs one newly discovered prerequisite.",
            "The dependency must be preserved before activation.",
            "Record the prerequisite and relationship.",
            "A project can evaluate it.",
            work_models.PrerequisiteProposalRelation(ItemId("work-c")),
            "The relationship is current.",
            ("source:local",),
            ("Work C remains ready.",),
        )
        before = store.snapshot()

        rejected = create_proposal(
            store,
            CreateProposalOperation(intake),
            expires_at - timedelta(microseconds=1),
            actor_task_id=TaskId("discovering-task"),
            actor_host_id=HostId("host-a"),
        )

        self.assertIsInstance(rejected, DecisionFailure)
        self.assertEqual(before, store.snapshot())
        accepted = create_proposal(
            store,
            CreateProposalOperation(intake),
            expires_at,
            actor_task_id=TaskId("discovering-task"),
            actor_host_id=HostId("host-a"),
        )
        self.assertNotIsInstance(accepted, DecisionFailure)
        self.assertIn(
            ProposalId("required-before-work-c"),
            tuple(value.proposal_id for value in SQLiteWorkStore(database_path).snapshot().proposals.proposals),
        )

    def test_prerequisite_proposal_rolls_back_after_partial_insert_and_target_mutation_failure(self) -> None:
        store, database_path = self._store()
        intake = ProposalIntake(
            ProposalId("required-before-work-c"),
            SQLITE_NOW,
            TaskId("discovering-task"),
            "Required before Work C",
            "Work C needs one newly discovered prerequisite.",
            "The dependency must be preserved before activation.",
            "Record the prerequisite and relationship.",
            "A project can evaluate it.",
            work_models.PrerequisiteProposalRelation(ItemId("work-c")),
            "The relationship is current.",
            ("source:local",),
            ("Work C remains ready.",),
        )
        before = store.snapshot()

        for table in ("proposal_evidence", "item_dependencies"):
            with self.subTest(table=table), reject_table_inserts(table), self.assertRaises(StorageError):
                create_proposal(
                    store,
                    CreateProposalOperation(intake),
                    SQLITE_NOW,
                    actor_task_id=TaskId("discovering-task"),
                    actor_host_id=HostId("host-a"),
                )
            self.assertEqual(before, store.snapshot())
            self.assertEqual(before, SQLiteWorkStore(database_path).snapshot())

    def test_transfer_repins_current_definition_and_revocation_fences_the_holder(self) -> None:
        snapshot = project_decision_snapshot(complete_sqlite_state(), SQLITE_NOW)
        definition = snapshot.definition(ItemId("work-c"))
        assert definition is not None
        retained = authority_models.PreparationLeaseAuthority(
            snapshot.host_epoch,
            ItemId("work-c"),
            definition.revision,
            definition.digest,
            TaskId("preparer-a"),
            HostId("host-a"),
            LeaseId("preparation-a"),
            2,
            SQLITE_NOW - timedelta(minutes=2),
            SQLITE_NOW - timedelta(minutes=1),
            authority_models.PreparationLeaseStatus.RELEASED,
        )
        inactive = authority_models.InactivePreparationAuthority(
            retained.host_epoch,
            retained.item,
            retained.definition_revision,
            retained.definition_digest,
            retained.task_id,
            retained.host_id,
            retained.lease_id,
            retained.generation,
            retained.expires_at,
            retained.state,
        )
        transferred = decide_preparation_authority(
            retained,
            2,
            authority_models.TransferPreparationAuthority(
                inactive,
                TaskId("preparer-b"),
                HostId("host-b"),
                LeaseId("preparation-b"),
                SQLITE_NOW,
                SQLITE_NOW + timedelta(minutes=1),
            ),
            snapshot,
            SQLITE_NOW,
        )
        self.assertNotIsInstance(transferred, DecisionFailure)
        assert not isinstance(transferred, DecisionFailure)
        revoked = decide_preparation_authority(
            transferred.proposed_replacement,
            transferred.counter_after,
            authority_models.RevokePreparationAuthority(
                transferred.item,
                transferred.proposed_replacement.lease_id,
                transferred.proposed_replacement.generation,
                TaskId("project-task"),
                HostId("host-a"),
                SQLITE_NOW + timedelta(seconds=1),
            ),
            snapshot,
            SQLITE_NOW + timedelta(seconds=1),
        )
        self.assertNotIsInstance(revoked, DecisionFailure)
        assert not isinstance(revoked, DecisionFailure)
        self.assertEqual(authority_models.PreparationLeaseStatus.REVOKED, revoked.proposed_replacement.state)
        self.assertGreater(revoked.proposed_replacement.generation, transferred.proposed_replacement.generation)

    def test_operation_start_time_remains_authoritative_while_waiting_for_sqlite_write_lock(self) -> None:
        state = complete_sqlite_state()
        acquired_at = SQLITE_NOW
        store, database_path = self._store(state)
        snapshot = project_decision_snapshot(store.snapshot(), acquired_at)
        item = snapshot.item(ItemId("work-c"))
        definition = snapshot.definition(ItemId("work-c"))
        assert item is not None
        assert definition is not None
        expires_at = acquired_at + timedelta(seconds=1)
        acquired = decide_and_commit_preparation_authority_change(
            store,
            authority_models.AcquireInitialPreparationAuthority(
                snapshot.host_epoch,
                item.item,
                snapshot.revision,
                snapshot.subject_revision(item.item) or "",
                definition.revision,
                definition.digest,
                TaskId("preparer"),
                HostId("host-a"),
                LeaseId("preparation-lock-test"),
                acquired_at,
                expires_at,
            ),
        )
        self.assertNotIsInstance(acquired, DecisionFailure)
        command_authority = project_decision_snapshot(store.snapshot(), acquired_at).command_preparation_authorities[0]
        operation_start = expires_at - timedelta(microseconds=1)
        connection_ready = Event()
        lock_held = Event()
        write_requested = Event()

        def signal_write(statement: str) -> None:
            if statement == "BEGIN IMMEDIATE":
                write_requested.set()

        def open_contender(path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = open_database(path, mode)
            connection.set_trace_callback(signal_write)
            connection_ready.set()
            if not lock_held.wait(timeout=10):
                connection.close()
                raise AssertionError("The test did not acquire the competing write lock.")
            return connection

        with (
            patch("pinboard.adapters.sqlite.store.open_database", side_effect=open_contender),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            pending_release = executor.submit(
                decide_and_commit_preparation_authority_change,
                store,
                authority_models.ReleasePreparationAuthority(command_authority, operation_start),
            )
            self.assertTrue(connection_ready.wait(timeout=10))
            blocker = open_database(database_path, OpenMode.READ_WRITE)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                lock_held.set()
                self.assertTrue(write_requested.wait(timeout=10))
                self.assertFalse(pending_release.done())
                blocker.rollback()
                released = pending_release.result(timeout=10)
            finally:
                blocker.rollback()
                blocker.close()

        self.assertNotIsInstance(released, DecisionFailure)
        self.assertEqual(
            authority_models.PreparationLeaseStatus.RELEASED, store.snapshot().authority.preparation_leases[0].state
        )


if __name__ == "__main__":
    unittest.main()
