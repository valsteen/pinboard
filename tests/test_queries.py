import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pinboard.adapters.files.file_io import resolve_durable_roots
from pinboard.adapters.sqlite.database import initialize_database
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models, stored_state
from pinboard.application.actions import discover_current_actions
from pinboard.application.queries import (
    project_current_overview,
    project_item_status,
    project_overview,
    project_parallel_preview,
    select_item_definition,
    select_item_definition_history,
    select_parallel_preview,
)
from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import (
    AttemptId,
    ItemId,
    LeaseId,
    TaskId,
)
from tests.decision_support import discover_actions
from tests.domain_support import expect_success
from tests.support import (
    SQLITE_NOW,
    complete_sqlite_state,
    initialize_store,
    test_definition,
    with_definition_dependencies,
)


class SQLiteQueriesTest(unittest.TestCase):
    def _store(self, state: stored_state.StoredWorkState | None = None) -> SQLiteWorkStore:
        project = Path(tempfile.mkdtemp()).resolve()
        roots = resolve_durable_roots(project)
        initialize_database(roots, SQLITE_NOW)
        store = SQLiteWorkStore(roots.database_path)
        initialize_store(store, state or complete_sqlite_state())
        return store

    def test_overview_and_parallel_preview_read_only_sqlite_state(self) -> None:
        store = self._store()

        state = store.validated_snapshot()
        overview = project_overview(state, SQLITE_NOW)
        preview = project_parallel_preview(state, now=SQLITE_NOW)
        self.assertIsInstance(preview, query_models.ParallelPreview)
        assert isinstance(preview, query_models.ParallelPreview)

        self.assertEqual("sqlite-v6", overview.authority)
        self.assertEqual("12", overview.revision)
        self.assertEqual(("work-a-1",), overview.active_attempts)
        self.assertEqual(
            ("intake-work", "work-a", "work-c", "zz-proposal-a"),
            tuple(item.item_id for item in overview.items),
        )
        self.assertIsNone(overview.items[0].source)
        self.assertIsNone(overview.items[0].notes)
        proposal = overview.items[-1]
        self.assertEqual((4, False, ("work-c",)), (proposal.position, proposal.eligible, proposal.depends_on))
        self.assertEqual("work-c", proposal.dependency_reasons[0].item_id)
        self.assertIn("Follow-up to work-c", proposal.dependency_reasons[0].reason)
        self.assertEqual((), proposal.review_flags)
        self.assertNotIn("zz-proposal-a", overview.immediate_options)
        self.assertEqual("12", preview.revision)

    def test_overview_exposes_duplicate_contradiction_and_clarification_for_review(self) -> None:
        for relation in (
            work_models.DuplicateProposalRelation(ItemId("work-c")),
            work_models.ContradictionProposalRelation(ItemId("work-c")),
            work_models.ClarificationProposalRelation(),
        ):
            state = complete_sqlite_state()
            proposal = state.proposals.proposals[0]
            changed = replace(proposal, relation=relation)
            dependencies = tuple(
                value for value in state.lifecycle.dependencies if value.item_id != ItemId("zz-proposal-a")
            )
            state = with_definition_dependencies(state, ItemId("zz-proposal-a"), ())
            store = self._store(
                replace(
                    state,
                    lifecycle=replace(state.lifecycle, dependencies=dependencies),
                    proposals=replace(state.proposals, proposals=(changed,)),
                )
            )

            with self.subTest(relation=relation.kind.value):
                item = project_overview(store.validated_snapshot(), SQLITE_NOW).items[-1]
                self.assertEqual((), item.depends_on)
                self.assertTrue(item.eligible)
                self.assertEqual(relation.kind, item.review_flags[0].kind)
                self.assertEqual(None if relation.item is None else "work-c", item.review_flags[0].related_item)

    def test_focused_overview_preserves_returned_proposal_review_flags(self) -> None:
        state = complete_sqlite_state()
        proposal = state.proposals.proposals[0]
        returned = replace(
            proposal,
            disposition=work_models.ReturnedProposalDisposition("Clarify the retained boundary.", SQLITE_NOW),
        )
        store = self._store(replace(state, proposals=replace(state.proposals, proposals=(returned,))))

        full = project_overview(store.validated_snapshot(), SQLITE_NOW)
        focused = project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW)

        self.assertEqual(full, focused)
        self.assertEqual("Clarify the retained boundary.", focused.items[-1].review_flags[0].reason)

    def test_overview_supplies_definition_context_and_explicit_replacement_warning_in_one_read(self) -> None:
        state = complete_sqlite_state()
        relation = stored_state.StoredPlannedReplacement(
            ItemId("work-c"),
            1,
            ItemId("work-a"),
            "Starting Work C now would duplicate the replacement implementation and review.",
            work_models.PlannedReplacementStatus.CURRENT,
            TaskId("coordinator"),
            SQLITE_NOW,
            12,
        )
        state = replace(state, replacements=stored_state.ReplacementRecords((relation,), ()))
        store = self._store(state)

        full = project_overview(store.validated_snapshot(), SQLITE_NOW)
        focused = project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW)

        self.assertEqual(full, focused)
        item = next(value for value in focused.items if value.item_id == "work-c")
        definition = next(
            value.definition for value in state.lifecycle.definition_revisions if value.item_id == ItemId("work-c")
        )
        self.assertEqual(
            (definition.title, definition.effect, definition.unlock), (item.label, item.effect, item.unlock)
        )
        self.assertIsNotNone(item.planned_replacement)
        assert item.planned_replacement is not None
        self.assertEqual("work-a", item.planned_replacement.replacement_item_id)
        self.assertFalse(item.planned_replacement.temporarily_retained)
        self.assertNotIn("work-c", focused.immediate_options)

    def test_focused_action_order_remains_identity_stable_while_overview_uses_queue_order(self) -> None:
        state = complete_sqlite_state()
        positions = {
            ItemId("intake-work"): 3,
            ItemId("work-a"): 2,
            ItemId("work-c"): 1,
            ItemId("zz-proposal-a"): 4,
        }
        reordered = replace(
            state,
            lifecycle=replace(
                state.lifecycle,
                work_items=tuple(
                    replace(item, queue_position=positions.get(item.item_id))
                    if item.queue_position is not None
                    else item
                    for item in state.lifecycle.work_items
                ),
            ),
        )
        store = self._store(reordered)

        complete_actions = expect_success(discover_actions(reordered, decision_models.Role.PROJECT, now=SQLITE_NOW))
        focused_actions = expect_success(
            discover_current_actions(store.read_current_action_snapshot(SQLITE_NOW), decision_models.Role.PROJECT)
        )
        complete_overview = project_overview(reordered, SQLITE_NOW)
        focused_overview = project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW)

        self.assertEqual(
            tuple(decision_models.action_id(value) for value in complete_actions),
            tuple(decision_models.action_id(value) for value in focused_actions),
        )
        self.assertEqual(complete_overview, focused_overview)
        self.assertEqual(
            ("work-c", "work-a", "intake-work", "zz-proposal-a"),
            tuple(value.item_id for value in focused_overview.items),
        )

    def test_parallel_preview_reports_the_current_attempt_not_retained_terminal_history(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        historical = replace(active, attempt_id=type(active.attempt_id)("aaa-old"), state=work_models.AttemptState.DONE)
        store = self._store(replace(state, lifecycle=replace(state.lifecycle, attempts=(historical, active))))

        preview = select_parallel_preview(store, selected=("work-a",), now=SQLITE_NOW)
        self.assertIsInstance(preview, query_models.ParallelPreview)
        assert isinstance(preview, query_models.ParallelPreview)

        selected = preview.items[0]
        self.assertEqual("work-a-1", selected.attempt_id)

    def test_item_status_returns_exact_live_and_done_shapes_while_overview_stays_live_only(self) -> None:
        state = complete_sqlite_state()
        active = state.lifecycle.attempts[0]
        done_item = replace(
            state.lifecycle.work_items[2],
            state=stored_state.StoredWorkItemState.DONE,
            timing=work_models.Timing.SAFE_TO_DEFER,
            outcome_evidence="accepted completion",
            next_action=None,
            source=None,
            notes=None,
        )
        done_attempts = (
            replace(
                active,
                attempt_id=AttemptId("work-b-z"),
                item_id=done_item.item_id,
                state=work_models.AttemptState.DONE,
                accepted_scope_digest=test_definition(done_item.item_id)[1],
                candidate_revision="candidate-z",
                candidate_recorded_at=SQLITE_NOW,
            ),
        )
        store = self._store(
            replace(
                state,
                lifecycle=replace(
                    state.lifecycle,
                    work_items=(*state.lifecycle.work_items[:2], done_item, *state.lifecycle.work_items[3:]),
                    attempts=(*state.lifecycle.attempts, *done_attempts),
                ),
            )
        )

        live = expect_success(project_item_status(store, ItemId("work-a"), SQLITE_NOW))
        done = expect_success(project_item_status(store, done_item.item_id, SQLITE_NOW))

        self.assertEqual(
            query_models.ItemStatus(
                "pinboard-item-status/v1",
                "sqlite-v6",
                "12",
                "work-a",
                "Work work-a",
                stored_state.StoredWorkItemState.ACTIVE,
                work_models.Timing.MUST_NOW,
                None,
                "continue",
                "accepted requirement",
                "Current work remains bounded.",
                2,
                (query_models.ItemStatusAttempt("work-a-1", work_models.AttemptState.ACTIVE, None),),
                None,
            ),
            live,
        )
        self.assertEqual(
            query_models.ItemStatus(
                "pinboard-item-status/v1",
                "sqlite-v6",
                "12",
                "work-b",
                "Work work-b",
                stored_state.StoredWorkItemState.DONE,
                work_models.Timing.SAFE_TO_DEFER,
                "accepted completion",
                None,
                None,
                None,
                None,
                (),
                None,
            ),
            done,
        )
        self.assertNotIn(
            "work-b", tuple(item.item_id for item in project_overview(store.validated_snapshot(), SQLITE_NOW).items)
        )

    def test_item_status_returns_terminal_siblings_with_non_null_attempt_arrays(self) -> None:
        state = complete_sqlite_state()
        for terminal in (
            stored_state.StoredWorkItemState.SUPERSEDED,
            stored_state.StoredWorkItemState.DROPPED,
        ):
            terminal_item = replace(
                state.lifecycle.work_items[2],
                state=terminal,
                outcome_evidence=f"{terminal.value} by decision",
                notes=None,
            )
            store = self._store(
                replace(
                    state,
                    lifecycle=replace(
                        state.lifecycle,
                        work_items=(*state.lifecycle.work_items[:2], terminal_item, *state.lifecycle.work_items[3:]),
                    ),
                )
            )

            with self.subTest(terminal=terminal.value):
                status = expect_success(project_item_status(store, terminal_item.item_id, SQLITE_NOW))
                self.assertEqual(terminal.value, status.state.value)
                self.assertEqual((), status.attempts)
                self.assertIsNone(status.notes)

    def test_item_status_rejects_an_unknown_canonical_identity(self) -> None:
        store = self._store()

        missing = project_item_status(store, ItemId("missing-item"), SQLITE_NOW)

        self.assertIsInstance(missing, DecisionFailure)
        assert isinstance(missing, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ITEM_NOT_FOUND, missing.code)

    def test_item_definition_and_history_project_exact_store_reads(self) -> None:
        state = complete_sqlite_state()
        item = next(value for value in state.lifecycle.work_items if value.item_id == ItemId("work-c"))
        store = self._store(state)

        definition = expect_success(select_item_definition(store, item.item_id))
        history = expect_success(select_item_definition_history(store, item.item_id, limit=1, before_revision=None))

        self.assertEqual(state.lifecycle.project.revision, definition.project_revision)
        self.assertEqual(item.subject_revision, definition.item_subject_revision)
        self.assertEqual(1, definition.definition_revision)
        self.assertEqual(test_definition(item.item_id)[1], definition.definition_digest)
        self.assertEqual((1,), tuple(value.revision for value in history.revisions))
        self.assertIsNone(history.next_before_revision)

    def test_action_and_query_failure_matrix_is_stable_and_read_only(self) -> None:
        state = complete_sqlite_state()
        store = self._store(state)
        loaded = store.validated_snapshot()
        observer = expect_success(discover_actions(loaded, decision_models.Role.OBSERVER, now=SQLITE_NOW))
        project = expect_success(discover_actions(loaded, decision_models.Role.PROJECT, now=SQLITE_NOW))
        worker = expect_success(
            discover_actions(
                loaded,
                decision_models.Role.WORKER,
                lease_id=LeaseId("attempt-lease-a"),
                generation=3,
                now=SQLITE_NOW,
            )
        )

        self.assertEqual((decision_models.ActionKind.INSPECT,), tuple(action.kind for action in observer))
        self.assertTrue(any(action.kind == decision_models.ActionKind.DISPATCH for action in project))
        self.assertFalse(any(action.kind == decision_models.ActionKind.ACTIVATE for action in project))
        self.assertTrue(any(action.kind == decision_models.ActionKind.CONTINUE for action in worker))
        missing_worker = discover_actions(loaded, decision_models.Role.WORKER, now=SQLITE_NOW)
        self.assertIsInstance(missing_worker, DecisionFailure)
        assert isinstance(missing_worker, DecisionFailure)
        self.assertEqual(DecisionFailureCode.ATTEMPT_LEASE_REQUIRED, missing_worker.code)
        self.assertEqual(state, store.validated_snapshot())

        invalid_selection = select_parallel_preview(store, selected=("missing",), now=SQLITE_NOW)
        self.assertIsInstance(invalid_selection, query_models.ParallelSelectionInvalid)
        assert isinstance(invalid_selection, query_models.ParallelSelectionInvalid)
        self.assertEqual("Selected item identities must be current items.", invalid_selection.message)


if __name__ == "__main__":
    unittest.main()
