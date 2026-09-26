"""Exercise frozen populated ledgers produced by the released SQLite v6 writer."""

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import timedelta
from pathlib import Path

from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import queries, service, stored_state
from pinboard.application.artifacts import WorkBriefIdentity
from pinboard.cli.entrypoint import main
from pinboard.domain import decision_models, work_models
from pinboard.domain.decisions import available_actions
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import AttemptId, HostId, ItemId, LeaseId, TaskId
from tests.decision_support import project_decision_snapshot
from tests.domain_support import expect_success
from tests.support import SQLITE_NOW

FIXTURES = (
    ("intake", "54630c601c4dea4a6647453d6a678dd51a516c1b034cb5257ab7ed7e19a99ff3"),
    ("ready", "bdff7e9eaf0597c74b3b809b910bf5437960afd1e947f8a5c102234936822fb5"),
)
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "released_v6"


class ReleasedV6CompatibilityTest(unittest.TestCase):
    def _copy_fixture(self, name: str, destination: Path) -> SQLiteWorkStore:
        shutil.copyfile(FIXTURE_ROOT / f"{name}.sqlite3", destination)
        return SQLiteWorkStore(destination)

    def _raw_counts(self, database: Path) -> dict[str, int]:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            return dict(connection.execute("SELECT state, item_count FROM work_item_state_counts"))

    def test_populated_released_ledgers_project_ready_without_read_writes(self) -> None:
        for name, digest in FIXTURES:
            with self.subTest(name=name):
                path = FIXTURE_ROOT / f"{name}.sqlite3"
                before = path.read_bytes()
                self.assertEqual(digest, hashlib.sha256(before).hexdigest())
                store = SQLiteWorkStore(path)
                state = store.validated_snapshot()
                retained_dispositions = {
                    value.disposition.kind for value in state.proposals.proposals if value.disposition is not None
                }
                self.assertTrue(
                    {work_models.ProposalDispositionKind.ACCEPTED, work_models.ProposalDispositionKind.RETURNED}
                    <= retained_dispositions
                )
                retained = next(
                    value for value in state.lifecycle.work_items if value.item_id == ItemId("zz-proposal-a")
                )
                self.assertEqual(name, retained.state.value)
                self.assertEqual(
                    "intake",
                    next(
                        value.state.value
                        for value in state.lifecycle.work_items
                        if value.item_id == ItemId("intake-work")
                    ),
                )
                status = expect_success(queries.project_item_status(store, ItemId("intake-work"), SQLITE_NOW))
                self.assertEqual(stored_state.StoredWorkItemState.READY, status.state)
                overview = queries.project_current_overview(store.read_project_overview(SQLITE_NOW), SQLITE_NOW)
                self.assertEqual("pinboard-overview/v6", overview.schema)
                proposal = next(value for value in overview.items if value.item_id == "zz-proposal-a")
                self.assertEqual(work_models.WorkState.READY, proposal.state)
                self.assertIsNotNone(proposal.proposal_origin)
                counts = dict(store.read_project_status().counts)
                self.assertEqual(5, counts["ready"])
                self.assertNotIn("intake", counts)
                self.assertEqual(before, path.read_bytes())

                with tempfile.TemporaryDirectory() as temporary:
                    project = Path(temporary)
                    work_root = project / ".pinboard"
                    work_root.mkdir()
                    shutil.copyfile(path, work_root / "state.sqlite3")
                    output = io.StringIO()
                    with redirect_stdout(output):
                        result = main(
                            ("--project-root", str(project), "--work-root", str(work_root), "status", "--json")
                        )
                    self.assertEqual(0, result)
                    self.assertEqual(counts, json.loads(output.getvalue())["counts"])

    def test_raw_intake_and_ready_prepare_and_activate_from_a_fresh_store(self) -> None:
        for name, _digest in FIXTURES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                database = Path(temporary) / "state.sqlite3"
                store = self._copy_fixture(name, database)
                item_id = "intake-work" if name == "intake" else "work-c"
                attempt_id = f"{item_id}-1"
                raw_state = "intake" if name == "intake" else "ready"
                before_counts = self._raw_counts(database)
                prepared = expect_success(
                    service.start_preparation(
                        store,
                        item_id=ItemId(item_id),
                        task_id=TaskId("preparer"),
                        host_id=HostId("local"),
                        lease_id=LeaseId("legacy-preparation"),
                        acquired_at=SQLITE_NOW,
                        expires_at=SQLITE_NOW + timedelta(minutes=5),
                    )
                )
                fresh = SQLiteWorkStore(database)
                snapshot = project_decision_snapshot(fresh.validated_snapshot(), SQLITE_NOW)
                authority = prepared.authority
                actor = decision_models.ActorAuthority(
                    decision_models.Role.PREPARER,
                    decision_models.AuthorizationKind.PREPARATION,
                    authority.generation,
                    authority.lease_id,
                    preparations=(ItemId(item_id),),
                )
                action = next(
                    value
                    for value in expect_success(available_actions(snapshot, actor))
                    if isinstance(value, decision_models.ActivateAction) and value.capability.subject == ItemId(item_id)
                )
                brief_ref_id = fresh.validated_snapshot().artifact_references[0].artifact_ref_id
                command = decision_models.ActivateCommand(
                    action,
                    work_models.ActivateInput(
                        AttemptId(attempt_id), "codex/legacy", "candidate-base", "worker", brief_ref_id
                    ),
                )
                identity = WorkBriefIdentity(
                    attempt_id,
                    item_id,
                    "codex/legacy",
                    "candidate-base",
                    authority.definition_revision,
                    authority.definition_digest,
                )
                committed = service.decide_and_commit_transition(
                    fresh,
                    command,
                    SQLITE_NOW + timedelta(seconds=1),
                    read_authorization_time=lambda: SQLITE_NOW + timedelta(seconds=1),
                    actor_task_id=None,
                    actor_host_id=None,
                    transition_brief_identity=identity,
                )
                self.assertNotIsInstance(committed, DecisionFailure)
                reloaded = SQLiteWorkStore(database).validated_snapshot()
                item = next(value for value in reloaded.lifecycle.work_items if value.item_id == ItemId(item_id))
                self.assertEqual(stored_state.StoredWorkItemState.ACTIVE, item.state)
                self.assertIn(
                    AttemptId(attempt_id),
                    tuple(value.attempt_id for value in reloaded.lifecycle.attempts if value.item_id == item.item_id),
                )
                after_counts = self._raw_counts(database)
                self.assertEqual(before_counts[raw_state] - 1, after_counts[raw_state])
                self.assertEqual(before_counts["active"] + 1, after_counts["active"])

    def test_undispatched_proposals_can_merge_or_reject_from_both_raw_states(self) -> None:
        for name, _digest in FIXTURES:
            for kind in (decision_models.ActionKind.MERGE_PROPOSAL, decision_models.ActionKind.REJECT_PROPOSAL):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temporary:
                    database = Path(temporary) / "state.sqlite3"
                    store = self._copy_fixture(name, database)
                    snapshot = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW)
                    actor = decision_models.ActorAuthority(
                        decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
                    )
                    action = next(
                        value
                        for value in expect_success(available_actions(snapshot, actor))
                        if value.kind == kind and value.capability.subject == ItemId("zz-proposal-a")
                    )
                    if isinstance(action, decision_models.MergeProposalAction):
                        command = decision_models.MergeProposalCommand(
                            action, work_models.MergeProposalInput(ItemId("work-c"))
                        )
                    else:
                        assert isinstance(action, decision_models.RejectProposalAction)
                        command = decision_models.RejectProposalCommand(
                            action, work_models.ReasonInput("No longer needed.")
                        )
                    committed = service.decide_and_commit_transition(
                        store,
                        command,
                        SQLITE_NOW + timedelta(seconds=1),
                        read_authorization_time=lambda: SQLITE_NOW + timedelta(seconds=1),
                        actor_task_id=TaskId("project"),
                        actor_host_id=HostId("local"),
                    )
                    self.assertNotIsInstance(committed, DecisionFailure)
                    reloaded = SQLiteWorkStore(database).validated_snapshot()
                    proposal = next(
                        value for value in reloaded.proposals.proposals if value.proposal_id == "zz-proposal-a"
                    )
                    self.assertEqual(
                        work_models.ProposalDispositionKind.MERGED
                        if kind == decision_models.ActionKind.MERGE_PROPOSAL
                        else work_models.ProposalDispositionKind.REJECTED,
                        proposal.disposition.kind if proposal.disposition is not None else None,
                    )

    def test_selected_proposal_resolution_becomes_stale_after_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            store = self._copy_fixture("ready", database)
            project_actor = decision_models.ActorAuthority(
                decision_models.Role.PROJECT, decision_models.AuthorizationKind.PROJECT, 0
            )
            snapshot = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW)
            initial_actions = expect_success(available_actions(snapshot, project_actor))
            stale_merge = next(
                value
                for value in initial_actions
                if isinstance(value, decision_models.MergeProposalAction)
                and value.capability.subject == ItemId("zz-proposal-a")
            )
            close_dependency = next(
                value
                for value in initial_actions
                if isinstance(value, decision_models.CloseAction) and value.capability.subject == ItemId("work-c")
            )
            expect_success(
                service.decide_and_commit_transition(
                    store,
                    decision_models.CloseCommand(
                        close_dependency, work_models.CloseInput(work_models.CloseOutcome.DONE, "Dependency complete.")
                    ),
                    SQLITE_NOW,
                    read_authorization_time=lambda: SQLITE_NOW,
                    actor_task_id=TaskId("project"),
                    actor_host_id=HostId("local"),
                )
            )
            prepared = expect_success(
                service.start_preparation(
                    store,
                    item_id=ItemId("zz-proposal-a"),
                    task_id=TaskId("preparer"),
                    host_id=HostId("local"),
                    lease_id=LeaseId("proposal-preparation"),
                    acquired_at=SQLITE_NOW,
                    expires_at=SQLITE_NOW + timedelta(minutes=5),
                )
            )
            authority = prepared.authority
            snapshot = project_decision_snapshot(store.validated_snapshot(), SQLITE_NOW)
            preparer = decision_models.ActorAuthority(
                decision_models.Role.PREPARER,
                decision_models.AuthorizationKind.PREPARATION,
                authority.generation,
                authority.lease_id,
                preparations=(ItemId("zz-proposal-a"),),
            )
            activate = next(
                value
                for value in expect_success(available_actions(snapshot, preparer))
                if isinstance(value, decision_models.ActivateAction)
                and value.capability.subject == ItemId("zz-proposal-a")
            )
            brief_ref_id = store.validated_snapshot().artifact_references[0].artifact_ref_id
            expect_success(
                service.decide_and_commit_transition(
                    store,
                    decision_models.ActivateCommand(
                        activate,
                        work_models.ActivateInput(
                            AttemptId("zz-proposal-a-1"),
                            "codex/proposal",
                            "candidate-base",
                            "worker",
                            brief_ref_id,
                        ),
                    ),
                    SQLITE_NOW + timedelta(seconds=1),
                    read_authorization_time=lambda: SQLITE_NOW + timedelta(seconds=1),
                    actor_task_id=None,
                    actor_host_id=None,
                    transition_brief_identity=WorkBriefIdentity(
                        "zz-proposal-a-1",
                        "zz-proposal-a",
                        "codex/proposal",
                        "candidate-base",
                        authority.definition_revision,
                        authority.definition_digest,
                    ),
                )
            )
            fresh = SQLiteWorkStore(database)
            before = fresh.validated_snapshot()
            current_actions = expect_success(
                available_actions(project_decision_snapshot(before, SQLITE_NOW), project_actor)
            )
            self.assertFalse(
                any(
                    isinstance(value, decision_models.MergeProposalAction | decision_models.RejectProposalAction)
                    and value.capability.subject == ItemId("zz-proposal-a")
                    for value in current_actions
                )
            )
            rejected = service.decide_and_commit_transition(
                fresh,
                decision_models.MergeProposalCommand(stale_merge, work_models.MergeProposalInput(ItemId("work-c"))),
                SQLITE_NOW + timedelta(seconds=2),
                read_authorization_time=lambda: SQLITE_NOW + timedelta(seconds=2),
                actor_task_id=TaskId("project"),
                actor_host_id=HostId("local"),
            )
            self.assertIsInstance(rejected, DecisionFailure)
            self.assertEqual(before, SQLiteWorkStore(database).validated_snapshot())
