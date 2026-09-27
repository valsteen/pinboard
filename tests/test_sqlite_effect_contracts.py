import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite import lifecycle, proposals
from pinboard.adapters.sqlite import state as sqlite_state
from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.artifacts import accept_checkpoint_artifact
from pinboard.adapters.sqlite.authority import (
    validate_attempt_authority,
    write_attempt_authority,
)
from pinboard.adapters.sqlite.database import initialize_database, open_database, write_transaction
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models, stored_state
from pinboard.application.artifacts import EvidenceArtifactRef
from pinboard.application.mutations import project_transition_mutation
from pinboard.application.service import start_preparation
from pinboard.domain import authority_models, decision_models, work_models
from pinboard.domain.decisions import available_actions, decide
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import ArtifactRefId, AttemptId, HostId, LeaseId, ProposalId, TaskId, WorkItemId
from tests.decision_support import project_decision_snapshot
from tests.support import (
    SQLITE_NOW,
    complete_sqlite_state,
    initialize_store,
    mutation_allocation,
    test_definition,
    with_definition_dependencies,
)


class SQLiteEffectContractTest(unittest.TestCase):
    def _store(self, state: stored_state.StoredWorkState | None = None) -> tuple[Path, SQLiteWorkStore]:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state or complete_sqlite_state())
        return roots.database_path, store

    def test_public_read_statement_growth_is_bounded_and_selection_stays_focused(self) -> None:  # noqa: PLR0915
        state = complete_sqlite_state()
        original = next(item for item in state.lifecycle.work_items if item.item_id == WorkItemId("work-c"))
        originals = next(
            definition for definition in state.lifecycle.definition_revisions if definition.item_id == original.item_id
        )
        added_ids = tuple(WorkItemId(f"extra-{index:03}") for index in range(30))
        added_items = tuple(
            replace(original, item_id=item_id, queue_position=5 + index) for index, item_id in enumerate(added_ids)
        )
        added_definitions = tuple(
            replace(
                originals,
                item_id=item_id,
                definition=test_definition(item_id)[0],
                digest=test_definition(item_id)[1],
                after_digest=test_definition(item_id)[1],
            )
            for item_id in added_ids
        )
        retained = next(item for item in state.lifecycle.work_items if item.item_id == WorkItemId("work-b"))
        retained_ids = tuple(WorkItemId(f"retained-{index:03}") for index in range(40))
        retained_items = tuple(replace(retained, item_id=item_id) for item_id in retained_ids)
        retained_definitions = tuple(
            replace(
                originals,
                item_id=item_id,
                definition=test_definition(item_id)[0],
                digest=test_definition(item_id)[1],
                after_digest=test_definition(item_id)[1],
            )
            for item_id in retained_ids
        )
        grown = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=(*state.lifecycle.work_items, *added_items, *retained_items),
                definition_revisions=(*state.lifecycle.definition_revisions, *added_definitions, *retained_definitions),
            ),
        )
        _path, store = self._store(grown)
        _base_path, base_store = self._store(state)
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        def count() -> int:
            return sum(statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement in statements)

        with patch.object(sqlite_store, "open_database", traced_open):
            base_live = base_store.read_current_action_snapshot(SQLITE_NOW)
            live_small = count()
            statements.clear()
            grown_live = store.read_current_action_snapshot(SQLITE_NOW)
            live_large = count()
            statements.clear()
            small = store.read_decision_facts(
                query_models.DecisionScope(added_ids[:1], (), (), (), (), (), (), ()), SQLITE_NOW
            )
            small_count = count()
            statements.clear()
            large = store.read_decision_facts(
                query_models.DecisionScope(added_ids, (), (), (), (), (), (), ()), SQLITE_NOW
            )
            large_count = count()
            selected_sql = tuple(statements)
            statements.clear()
            store.read_generated_view_facts(added_ids[:1], (), (), SQLITE_NOW)
            view_small = count()
            statements.clear()
            store.read_generated_view_facts(added_ids, (), (), SQLITE_NOW)
            view_large = count()
            statements.clear()
            store.read_parallel_preview(added_ids[:1])
            preview_small = count()
            statements.clear()
            store.read_parallel_preview(added_ids)
            preview_large = count()

        self.assertEqual(len(base_live.items) + len(added_ids), len(grown_live.items))
        self.assertEqual(live_small, live_large)
        self.assertEqual(added_ids[:1], tuple(item.work_item_id for item in small.snapshot.items))
        self.assertEqual(set(added_ids), {item.work_item_id for item in large.snapshot.items})
        self.assertEqual((small_count, view_small, preview_small), (large_count, view_large, preview_large))
        with sqlite3.connect(_path) as connection:
            for table in ("work_items", "item_dependencies", "work_item_definition_revisions"):
                query = next(
                    statement for statement in selected_sql if f"FROM {table}" in statement and " IN (" in statement
                )
                plan = " ".join(str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {query}"))
                self.assertIn("SEARCH", plan, (table, plan))

    def test_pending_proposal_export_batches_child_reads(self) -> None:
        state = complete_sqlite_state()
        proposal = state.proposals.proposals[0]
        proposal_item = next(
            item for item in state.lifecycle.work_items if item.item_id == WorkItemId(proposal.proposal_id)
        )
        definition = next(
            value for value in state.lifecycle.definition_revisions if value.item_id == proposal_item.item_id
        )
        evidence = state.proposals.evidence[0]
        freshness = state.proposals.freshness[0]
        ids = tuple(ProposalId(f"proposal-extra-{index:03}") for index in range(25))
        grown = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=(
                    *state.lifecycle.work_items,
                    *(
                        replace(
                            proposal_item,
                            item_id=WorkItemId(value),
                            queue_position=5 + index,
                            source=f"proposal:{value}",
                        )
                        for index, value in enumerate(ids)
                    ),
                ),
                definition_revisions=(
                    *state.lifecycle.definition_revisions,
                    *(replace(definition, item_id=WorkItemId(value)) for value in ids),
                ),
                dependencies=(
                    *state.lifecycle.dependencies,
                    *(stored_state.ItemDependency(WorkItemId(value), WorkItemId("work-c"), 0) for value in ids),
                ),
            ),
            proposals=replace(
                state.proposals,
                proposals=(*state.proposals.proposals, *(replace(proposal, proposal_id=value) for value in ids)),
                evidence=(*state.proposals.evidence, *(replace(evidence, proposal_id=value) for value in ids)),
                freshness=(*state.proposals.freshness, *(replace(freshness, proposal_id=value) for value in ids)),
            ),
        )
        _base_path, base_store = self._store(state)
        path, store = self._store(grown)
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            base = base_store.read_project_export_batches()[0]
            base_count = sum(value.lstrip().upper().startswith("SELECT") for value in statements)
            statements.clear()
            selected = store.read_project_export_batches()[0]
            grown_count = sum(value.lstrip().upper().startswith("SELECT") for value in statements)
        self.assertEqual(len(base.proposals.proposals) + len(ids), len(selected.proposals.proposals))
        self.assertEqual(base_count, grown_count)
        child_query = next(value for value in statements if "FROM proposal_evidence WHERE proposal_id IN" in value)
        with sqlite3.connect(path) as connection:
            plan = " ".join(str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {child_query}"))
        self.assertIn("SEARCH PROPOSAL_EVIDENCE", plan)
        self.assertNotIn("SCAN PROPOSAL_EVIDENCE", plan)

    def test_completion_context_batches_selected_checkpoint_references(self) -> None:
        path, store = self._store()

        def add_checkpoints(first: int, last: int) -> None:
            with closing(sqlite3.connect(path)) as connection, connection:
                for index in range(first, last):
                    connection.execute(
                        """INSERT INTO transition_history (
                               history_id, project_revision, action_id, action_kind, subject_id,
                               artifact_ref_id, artifact_kind, authorization_kind, actor_task_id,
                               actor_host_id, input_schema, input_json, outcome_schema,
                               outcome_json, committed_at
                           ) SELECT ?, ?, action_id, action_kind, subject_id, artifact_ref_id,
                                    artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                                    input_schema, input_json, 'checkpoint-acceptance/v2', '{}', committed_at
                           FROM transition_history WHERE history_id = 1""",
                        (index + 1, index + 12),
                    )
                connection.execute("UPDATE project_meta SET revision = ? WHERE singleton = 1", (last + 11,))

        def add_unrelated_history() -> None:
            with closing(sqlite3.connect(path)) as connection, connection:
                for index in range(22, 222):
                    connection.execute(
                        """INSERT INTO transition_history (
                               history_id, project_revision, action_id, action_kind, subject_id,
                               artifact_ref_id, artifact_kind, authorization_kind, actor_task_id,
                               actor_host_id, input_schema, input_json, outcome_schema,
                               outcome_json, committed_at
                           ) SELECT ?, ?, action_id, action_kind, 'unrelated-attempt', artifact_ref_id,
                                    artifact_kind, authorization_kind, actor_task_id, actor_host_id,
                                    input_schema, input_json, 'checkpoint-acceptance/v2', '{}', committed_at
                           FROM transition_history WHERE history_id = 1""",
                        (index + 1, index + 12),
                    )
                connection.execute("UPDATE project_meta SET revision = 233 WHERE singleton = 1")

        add_checkpoints(1, 2)
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            first = store.read_completion_context(AttemptId("work-a-1"))
            small_count = sum(value.lstrip().upper().startswith("SELECT") for value in statements)
            add_checkpoints(2, 22)
            add_unrelated_history()
            statements.clear()
            grown = store.read_completion_context(AttemptId("work-a-1"))
            large_count = sum(value.lstrip().upper().startswith("SELECT") for value in statements)
            completion_statements = tuple(statements)
            statements.clear()
            selected = store.read_decision_facts(
                query_models.DecisionScope((), (), (), (), (), (), (), (AttemptId("work-a-1"),)), SQLITE_NOW
            )
            decision_statements = tuple(statements)
        assert first is not None and grown is not None
        self.assertEqual(1, len(first.checkpoints))
        self.assertEqual(21, len(grown.checkpoints))
        self.assertEqual(21, len(selected.snapshot.checkpoint_history_ids))
        self.assertEqual(small_count, large_count)
        artifact_query = next(
            value for value in completion_statements if "FROM artifact_refs WHERE artifact_ref_id IN" in value
        )
        checkpoint_query = next(
            value
            for value in completion_statements
            if "FROM transition_history" in value and "checkpoint-acceptance/v2" in value
        )
        decision_query = next(
            value
            for value in decision_statements
            if "FROM transition_history" in value and "checkpoint-acceptance/v2" in value
        )
        with closing(sqlite3.connect(path)) as connection:
            plan = " ".join(str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {artifact_query}"))
            checkpoint_plan = " ".join(
                str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {checkpoint_query}")
            )
            decision_plan = " ".join(
                str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {decision_query}")
            )
        self.assertIn("SEARCH ARTIFACT_REFS USING INTEGER PRIMARY KEY", plan)
        for selected_plan in (checkpoint_plan, decision_plan):
            self.assertIn("SEARCH TRANSITION_HISTORY USING", selected_plan)
            self.assertIn("INDEX CHECKPOINT_HISTORY_BY_SUBJECT", selected_plan)

    def test_checkpoint_artifact_identity_is_exact(self) -> None:
        path, store = self._store()
        state = store.validated_snapshot()
        existing = next(value for value in state.artifact_references if value.kind == work_models.ArtifactKind.EVIDENCE)
        published = EvidenceArtifactRef(
            existing.key,
            existing.revision,
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        )
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            self.assertEqual(
                existing.artifact_ref_id,
                accept_checkpoint_artifact(
                    connection,
                    published,
                    existing.artifact_ref_id,
                    state.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                ),
            )
            with self.assertRaises(StorageError) as conflicting:
                accept_checkpoint_artifact(
                    connection,
                    replace(published, content_sha256="0" * 64),
                    ArtifactRefId(int(existing.artifact_ref_id) + 1),
                    state.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, conflicting.exception.code)
        finally:
            connection.close()

    def test_commit_does_not_assemble_complete_state_before_or_after_persistence(self) -> None:
        _path, store = self._store()
        before = store.validated_snapshot()
        with store.write() as transaction:
            snapshot = project_decision_snapshot(before, SQLITE_NOW)
            actions = available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.PROJECT,
                    decision_models.AuthorizationKind.PROJECT,
                    0,
                ),
            )
            assert not isinstance(actions, DecisionFailure)
            action = next(
                value
                for value in actions
                if str(value.capability.subject) == "intake-work"
                and value.kind == decision_models.ActionKind.BLOCK_ITEM
            )
            assert isinstance(action, decision_models.BlockWorkItemAction)
            decision = decide(
                snapshot,
                decision_models.BlockWorkItemCommand(action, work_models.BlockInput("Waiting on a dependency.")),
                SQLITE_NOW,
            )
            assert not isinstance(decision, DecisionFailure)
            mutation = project_transition_mutation(mutation_allocation(before), decision)
            with patch.object(sqlite_state, "read_state", side_effect=AssertionError("complete state assembled")):
                committed = transaction.commit(mutation)
        self.assertNotIsInstance(committed, DecisionFailure)

    def test_mutation_allocations_read_only_their_indexed_scalar_and_selected_artifact_facts(self) -> None:
        _path, store = self._store()
        state = store.validated_snapshot()
        existing = next(value for value in state.artifact_references if value.kind == work_models.ArtifactKind.EVIDENCE)
        published = EvidenceArtifactRef(
            existing.key,
            existing.revision,
            existing.selector,
            existing.content_sha256,
            existing.size_bytes,
        )

        with store.write() as transaction:
            statements: list[str] = []
            transaction.connection.set_trace_callback(statements.append)
            transaction.read_mutation_allocation()
            allocation_statements = tuple(statements)
            statements.clear()

            transaction.read_live_item_count()
            queue_statements = tuple(statements)
            statements.clear()

            transaction.read_checkpoint_mutation_allocation((published,))
            checkpoint_statements = tuple(statements)
            plans = {
                table: transaction.connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchone()["detail"]
                for table, statement in (
                    ("work_items", "SELECT COALESCE(MAX(queue_position), 0) FROM work_items"),
                    ("transition_history", "SELECT COALESCE(MAX(history_id), 0) + 1 FROM transition_history"),
                    ("artifact_refs", "SELECT COALESCE(MAX(artifact_ref_id), 0) + 1 FROM artifact_refs"),
                    (
                        "item_dependencies",
                        """
                        SELECT owner.item_id
                        FROM item_dependencies AS dependency
                        JOIN work_items AS owner ON owner.item_id = dependency.item_id
                        WHERE dependency.dependency_id = 'work-c'
                          AND owner.queue_position IS NOT NULL
                        ORDER BY owner.queue_position, owner.item_id
                        """,
                    ),
                )
            }

        allocation_sql = " ".join(allocation_statements).lower()
        self.assertIn("transition_history", allocation_sql)
        self.assertNotIn("work_items", allocation_sql)
        self.assertNotIn("artifact_refs", allocation_sql)

        queue_sql = " ".join(queue_statements).lower()
        self.assertIn("max(queue_position)", queue_sql)
        self.assertNotIn("count(", queue_sql)

        checkpoint_sql = " ".join(checkpoint_statements).lower()
        self.assertNotIn("work_items", checkpoint_sql)
        self.assertIn("max(artifact_ref_id)", checkpoint_sql)
        self.assertIn("where kind =", checkpoint_sql)
        self.assertIn("artifact_key =", checkpoint_sql)
        self.assertIn("artifact_revision =", checkpoint_sql)
        self.assertTrue(all("scan" not in detail.lower() for detail in plans.values()), plans)

    def test_current_project_reads_do_not_scan_retained_rows(self) -> None:
        path, store = self._store()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            store.read_project_status()
            store.read_project_overview(SQLITE_NOW)
            store.read_current_action_snapshot(SQLITE_NOW)
            store.read_current_parallel_snapshot(SQLITE_NOW)
            store.read_decision_facts(
                query_models.DecisionScope((WorkItemId("work-a"),), (), (), (), (), (), (), ()), SQLITE_NOW
            )

        self.assertFalse(any("from artifact_refs" in statement.lower() for statement in statements))

        connection = sqlite3.connect(path)
        try:
            for statement in statements:
                if not statement.lstrip().upper().startswith("SELECT"):
                    continue
                with self.subTest(statement=statement):
                    plan = tuple(
                        str(row[3]).upper() for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}").fetchall()
                    )
                    retained_scans = tuple(
                        detail for detail in plan if "SCAN " in detail and "ONE_LIVE_ATTEMPT_PER_ITEM" not in detail
                    )
                    self.assertEqual((), retained_scans, plan)
        finally:
            connection.close()

    def test_current_project_read_families_request_only_their_operation_facts(self) -> None:
        _path, store = self._store()
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            store.read_current_parallel_snapshot(SQLITE_NOW)
        self.assertFalse(any("from proposals" in value.lower() for value in statements))
        self.assertFalse(any("from artifact_refs" in value.lower() for value in statements))

        statements.clear()
        with patch.object(sqlite_store, "open_database", traced_open):
            store.read_project_overview(SQLITE_NOW)
        self.assertFalse(any("from attempt_leases" in value.lower() for value in statements))
        self.assertFalse(any("from artifact_refs" in value.lower() for value in statements))

    def test_decision_artifacts_and_dependency_closure_are_explicit(self) -> None:
        _path, store = self._store()
        focused = store.read_decision_facts(
            query_models.DecisionScope((WorkItemId("work-a"),), (), (), (), (), (), (), ()), SQLITE_NOW
        ).snapshot
        artifact = store.read_decision_facts(
            query_models.DecisionScope((WorkItemId("work-a"),), (), (), (), (), (), (ArtifactRefId(1),), ()),
            SQLITE_NOW,
        ).snapshot

        self.assertEqual((), focused.artifacts)
        self.assertEqual((ArtifactRefId(1),), tuple(value.artifact_ref_id for value in artifact.artifacts))

        state = complete_sqlite_state()
        state = with_definition_dependencies(state, WorkItemId("work-c"), (WorkItemId("intake-work"),))
        state = with_definition_dependencies(state, WorkItemId("work-b"), (WorkItemId("work-a"),))
        _chain_path, store = self._store(state)

        direct = store.read_decision_facts(
            query_models.DecisionScope((WorkItemId("work-a"),), (), (), (), (), (), (), ()), SQLITE_NOW
        ).snapshot
        closed = store.read_decision_facts(
            query_models.DecisionScope((WorkItemId("work-a"),), (), (WorkItemId("work-c"),), (), (), (), (), ()),
            SQLITE_NOW,
        ).snapshot
        terminal_closed = store.read_decision_facts(
            query_models.DecisionScope((WorkItemId("work-a"),), (), (WorkItemId("work-b"),), (), (), (), (), ()),
            SQLITE_NOW,
        ).snapshot

        self.assertEqual({WorkItemId("work-a"), WorkItemId("work-c")}, set(direct.work_items_by_id()))
        self.assertEqual({WorkItemId("work-a")}, {value.work_item_id for value in direct.definitions})
        self.assertEqual(
            {WorkItemId("work-a"), WorkItemId("work-c"), WorkItemId("intake-work")},
            set(closed.work_items_by_id()),
        )
        self.assertEqual(
            {WorkItemId("work-a"), WorkItemId("work-c"), WorkItemId("intake-work")},
            {value.work_item_id for value in closed.definitions},
        )
        self.assertIn(WorkItemId("work-b"), terminal_closed.history_items)
        self.assertIn(WorkItemId("work-b"), {value.work_item_id for value in terminal_closed.definitions})

    def test_generated_item_view_does_not_follow_transitive_dependencies(self) -> None:
        state = complete_sqlite_state()
        state = with_definition_dependencies(state, WorkItemId("work-c"), (WorkItemId("intake-work"),))
        _path, store = self._store(state)
        statements: list[str] = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            facts = store.read_generated_view_facts((WorkItemId("work-a"),), (), (), SQLITE_NOW)

        self.assertEqual((WorkItemId("work-c"),), facts.items[0].dependencies)
        self.assertIsNotNone(facts.items[0].overview)
        self.assertFalse(any("'intake-work'" in statement for statement in statements), statements)

    def test_committed_effect_refreshes_reverse_dependents_only_for_liveness_changes(self) -> None:
        def project_action(
            state: stored_state.StoredWorkState,
            item_id: WorkItemId,
            kind: decision_models.ActionKind,
        ) -> decision_models.Action:
            snapshot = project_decision_snapshot(state, SQLITE_NOW)
            actions = available_actions(
                snapshot,
                decision_models.ActorAuthority(
                    decision_models.Role.PROJECT,
                    decision_models.AuthorizationKind.PROJECT,
                    0,
                ),
            )
            assert not isinstance(actions, DecisionFailure)
            return next(value for value in actions if value.capability.subject == item_id and value.kind == kind)

        def reverse_dependency_queries(statements: list[str]) -> tuple[str, ...]:
            return tuple(
                statement
                for statement in statements
                if "FROM item_dependencies AS dependency" in statement and "WHERE dependency.dependency_id" in statement
            )

        state = complete_sqlite_state()
        _path, store = self._store(state)
        defer_action = project_action(state, WorkItemId("work-c"), decision_models.ActionKind.DEFER)
        assert isinstance(defer_action, decision_models.DeferAction)
        defer_decision = decide(
            project_decision_snapshot(state, SQLITE_NOW),
            decision_models.DeferCommand(
                defer_action,
                work_models.DeferInput(work_models.Timing.SAFE_TO_DEFER, "Reopen after the current review."),
            ),
            SQLITE_NOW,
        )
        assert not isinstance(defer_decision, DecisionFailure)
        statements: list[str] = []
        with store.write() as transaction:
            transaction.connection.set_trace_callback(statements.append)
            deferred = transaction.commit(project_transition_mutation(mutation_allocation(state), defer_decision))
        assert not isinstance(deferred, DecisionFailure)
        self.assertEqual((WorkItemId("work-c"),), deferred.work_item_ids)
        self.assertEqual((), reverse_dependency_queries(statements))

        _path, store = self._store()
        statements = []
        original_open = sqlite_store.open_database

        def traced_open(database_path: Path, mode: OpenMode) -> sqlite3.Connection:
            connection = original_open(database_path, mode)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(sqlite_store, "open_database", traced_open):
            prepared = start_preparation(
                store,
                work_item_id=WorkItemId("work-c"),
                task_id=TaskId("preparer"),
                host_id=HostId("host-a"),
                lease_id=LeaseId("preparation-work-c"),
                acquired_at=SQLITE_NOW,
                expires_at=SQLITE_NOW + timedelta(minutes=5),
            )
        assert not isinstance(prepared, DecisionFailure)
        self.assertEqual((WorkItemId("work-c"),), prepared.effect.work_item_ids)
        self.assertEqual((), reverse_dependency_queries(statements))

        state = complete_sqlite_state()
        _path, store = self._store(state)
        close_action = project_action(state, WorkItemId("intake-work"), decision_models.ActionKind.CLOSE)
        assert isinstance(close_action, decision_models.CloseAction)
        close_decision = decide(
            project_decision_snapshot(state, SQLITE_NOW),
            decision_models.CloseCommand(
                close_action,
                work_models.CloseInput(work_models.CloseOutcome.DROPPED, "No longer needed."),
            ),
            SQLITE_NOW,
        )
        assert not isinstance(close_decision, DecisionFailure), close_decision
        statements = []
        with store.write() as transaction:
            transaction.connection.set_trace_callback(statements.append)
            closed = transaction.commit(project_transition_mutation(mutation_allocation(state), close_decision))
        assert not isinstance(closed, DecisionFailure)
        self.assertEqual(
            (WorkItemId("intake-work"), WorkItemId("work-a"), WorkItemId("work-c"), WorkItemId("zz-proposal-a")),
            closed.work_item_ids,
        )
        self.assertEqual(1, len(reverse_dependency_queries(statements)), reverse_dependency_queries(statements))

    def test_complete_state_rejects_missing_project_invalid_queue_and_reinitialization(self) -> None:
        path, store = self._store()
        before = store.validated_snapshot()

        raw = sqlite3.connect(path)
        raw.row_factory = sqlite3.Row
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("BEGIN")
            raw.execute("DELETE FROM project_meta")
            with self.assertRaises(StorageError) as missing_project:
                sqlite_state.read_state(raw)
            self.assertEqual(StorageErrorCode.INVALID_STATE, missing_project.exception.code)
            raw.rollback()
        finally:
            raw.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                connection.execute("UPDATE work_items SET queue_position = 99 WHERE queue_position = 1")
                with self.assertRaises(StorageError) as invalid_queue:
                    sqlite_state.read_state(connection)
                self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_queue.exception.code)
                connection.rollback()
        finally:
            connection.close()

        with self.assertRaises(StorageError) as occupied:
            initialize_store(store, before)
        self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, occupied.exception.code)
        self.assertEqual(before, store.validated_snapshot())

    def test_queue_cas_failures_preserve_the_ledger(self) -> None:
        path, store = self._store()
        before = store.validated_snapshot()
        for effect, argument in ((lifecycle.compact_queue, 1), (lifecycle.make_queue_space, 1)):
            connection = open_database(path, OpenMode.READ_WRITE)
            try:
                connection.execute(
                    """
                    CREATE TEMP TRIGGER arrange_real_queue_staleness
                    BEFORE UPDATE OF queue_position ON work_items
                    BEGIN
                        SELECT RAISE(IGNORE);
                    END
                    """
                )
                with write_transaction(connection):
                    result = effect(connection, argument)
            finally:
                connection.close()
            self.assertIsInstance(result, DecisionFailure)
            self.assertEqual(before, store.validated_snapshot())

    def test_proposal_decode_and_stale_disposition_contracts_are_explicit(self) -> None:
        path, store = self._store()
        before = store.validated_snapshot()
        proposal_id = before.proposals.proposals[0].proposal_id
        for statement, parameters in (
            (
                "UPDATE proposals SET relation_kind = 'follow-up', relation_item_id = NULL WHERE proposal_id = ?",
                (proposal_id,),
            ),
            (
                """
                UPDATE proposals
                SET disposition = 'accepted', disposition_target_item_id = NULL,
                    disposition_recorded_at = ?
                WHERE proposal_id = ?
                """,
                (SQLITE_NOW.isoformat(), proposal_id),
            ),
        ):
            connection = open_database(path, OpenMode.READ_WRITE)
            try:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                with write_transaction(connection):
                    connection.execute(statement, parameters)
                    with self.assertRaises(StorageError) as malformed:
                        sqlite_state.read_state(connection)
                    self.assertEqual(StorageErrorCode.INVALID_STATE, malformed.exception.code)
                    connection.rollback()
            finally:
                connection.close()

        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            stale = proposals.set_proposal_disposition(
                connection,
                ProposalId("missing-proposal"),
                work_models.RejectedProposalDisposition("No longer relevant.", SQLITE_NOW),
                before.lifecycle.project.revision + 1,
            )
        finally:
            connection.close()
        self.assertIsInstance(stale, DecisionFailure)
        self.assertEqual(before, store.validated_snapshot())

    def test_attempt_authority_validation_rejects_missing_and_mismatched_generations(self) -> None:
        _path, store = self._store()
        before = store.validated_snapshot()
        without_counter = replace(
            before,
            authority=replace(before.authority, attempt_counters=()),
        )
        with self.assertRaises(StorageError) as invalid_generation:
            validate_attempt_authority(without_counter, StorageErrorCode.INVALID_STATE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_generation.exception.code)

        mismatched_lease = replace(
            before,
            authority=replace(
                before.authority,
                attempt_leases=(
                    replace(
                        before.authority.attempt_leases[0],
                        generation=before.authority.attempt_leases[0].generation - 1,
                    ),
                ),
            ),
        )
        with self.assertRaises(StorageError) as invalid_lease:
            validate_attempt_authority(mismatched_lease, StorageErrorCode.INVALID_STATE)
        self.assertEqual(StorageErrorCode.INVALID_STATE, invalid_lease.exception.code)

    def test_expected_insert_key_conflicts_are_stale_but_other_constraints_are_exceptional(self) -> None:
        path, store = self._store()
        before = store.validated_snapshot()
        attempt = before.lifecycle.attempts[0]
        duplicate_attempt = decision_models.ActivationChange(
            WorkItemId("work-c"),
            work_models.WorkState.READY,
            attempt.attempt_id,
            attempt.brief_artifact_ref_id,
            "duplicate-attempt",
            "base",
            "worker",
        )
        unrelated_live_attempt_conflict = replace(
            duplicate_attempt,
            work_item_id=attempt.item_id,
            attempt=AttemptId("other-live-attempt"),
        )
        work_c = next(value for value in before.lifecycle.work_items if value.item_id == WorkItemId("work-c"))
        work_c_definition = next(
            value for value in before.lifecycle.definition_revisions if value.item_id == WorkItemId("work-c")
        )
        work_a = next(value for value in before.lifecycle.work_items if value.item_id == attempt.item_id)
        work_a_definition = next(
            value for value in before.lifecycle.definition_revisions if value.item_id == attempt.item_id
        )
        connection = open_database(path, OpenMode.READ_WRITE)
        try:
            with write_transaction(connection):
                stale_attempt = lifecycle.insert_attempt(
                    connection,
                    work_c,
                    work_c_definition,
                    duplicate_attempt,
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertIsInstance(stale_attempt, DecisionFailure)

            with self.assertRaises(StorageError) as unrelated_unique, write_transaction(connection):
                lifecycle.insert_attempt(
                    connection,
                    work_a,
                    work_a_definition,
                    unrelated_live_attempt_conflict,
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_unique.exception.code)

            with self.assertRaises(StorageError) as unrelated_foreign_key, write_transaction(connection):
                lifecycle.insert_attempt(
                    connection,
                    replace(work_c, item_id=WorkItemId("missing-item")),
                    replace(work_c_definition, item_id=WorkItemId("missing-item")),
                    replace(
                        duplicate_attempt,
                        work_item_id=WorkItemId("missing-item"),
                        attempt=AttemptId("missing-item-attempt"),
                    ),
                    before.lifecycle.project.revision + 1,
                    SQLITE_NOW,
                )
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_foreign_key.exception.code)

            retained = before.authority.attempt_leases[0]
            command = project_decision_snapshot(before, SQLITE_NOW).command_attempt_authorities[0]
            current = authority_models.AttemptLeaseAuthority(
                command.host_epoch,
                command.attempt,
                command.work_item_id,
                command.task_id,
                command.host_id,
                command.lease_id,
                command.generation,
                retained.acquired_at,
                command.expires_at,
                authority_models.AttemptLeaseStatus.ACTIVE,
            )
            duplicate_current = authority_models.AttemptAuthorityDecision(
                command.attempt,
                command.generation,
                command.generation,
                None,
                current,
            )
            with write_transaction(connection):
                stale_current = write_attempt_authority(connection, duplicate_current)
            self.assertIsInstance(stale_current, DecisionFailure)

            missing_attempt = AttemptId("missing-attempt")
            invalid_counter = authority_models.AttemptAuthorityDecision(
                missing_attempt,
                0,
                -1,
                None,
                replace(current, attempt=missing_attempt),
            )
            with self.assertRaises(StorageError) as unrelated_counter_check, write_transaction(connection):
                write_attempt_authority(connection, invalid_counter)
            self.assertEqual(StorageErrorCode.INVARIANT_VIOLATION, unrelated_counter_check.exception.code)
        finally:
            connection.close()
        self.assertEqual(before, store.validated_snapshot())


if __name__ == "__main__":
    unittest.main()
