import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from pinboard.adapters.sqlite import store as sqlite_store
from pinboard.adapters.sqlite.models import OpenMode
from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import query_models, stored_state
from pinboard.application.mutation_models import CheckpointMutationAllocation
from pinboard.domain import authority_models, decision_models, work_models
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
    ProposalId,
    TaskId,
)
from tests.decision_support import project_decision_snapshot
from tests.sqlite_support import insert_initial_state


def mutation_allocation(state: stored_state.StoredWorkState) -> CheckpointMutationAllocation:
    return CheckpointMutationAllocation(
        state.lifecycle.project.revision,
        HistoryId(1 + max((int(value.history_id) for value in state.transition_receipts), default=0)),
        ArtifactRefId(1 + max((int(value.artifact_ref_id) for value in state.artifact_references), default=0)),
        state.artifact_references,
    )


def decision_facts(state: stored_state.StoredWorkState, now: datetime) -> query_models.DecisionFacts:
    return query_models.DecisionFacts(
        project_decision_snapshot(state, now),
        tuple(
            query_models.AttemptLineage(value.attempt_id, value.item_id, value.branch, value.base_revision)
            for value in state.lifecycle.attempts
        ),
    )


type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

SQLITE_NOW = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)
SQLITE_DEFINITION = work_models.WorkItemDefinition(
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
_SQLITE_DEFINITION_DIGEST = work_item_definition_digest(SQLITE_DEFINITION)
assert isinstance(_SQLITE_DEFINITION_DIGEST, str)
SQLITE_DIGEST = _SQLITE_DEFINITION_DIGEST


def initialize_store(store: SQLiteWorkStore, state: stored_state.StoredWorkState) -> None:
    """Seed a freshly initialized test database with an exact aggregate."""

    with store.write() as transaction:
        insert_initial_state(transaction.connection, state)


def test_definition(item: ItemId) -> tuple[work_models.WorkItemDefinition, str]:
    if item == ItemId("work-a"):
        definition = SQLITE_DEFINITION
    elif item == ItemId("zz-proposal-a"):
        definition = work_models.WorkItemDefinition(
            "Proposal A",
            "Record the follow-up.",
            "It may affect work C.",
            ("evidence:observation",),
            ("Record the follow-up.",),
            (),
            ("A later task can assess it.",),
            (ItemId("work-c"),),
            "Record the follow-up.",
            "A later task can assess it.",
        )
    else:
        definition = work_models.WorkItemDefinition(
            f"Work {item}",
            SQLITE_DEFINITION.objective,
            SQLITE_DEFINITION.hypothesis,
            SQLITE_DEFINITION.evidence,
            SQLITE_DEFINITION.scope,
            SQLITE_DEFINITION.non_scope,
            SQLITE_DEFINITION.acceptance_criteria,
            (),
            SQLITE_DEFINITION.effect,
            SQLITE_DEFINITION.unlock,
        )
    digest = work_item_definition_digest(definition)
    assert isinstance(digest, str)
    return definition, digest


def with_definition_dependencies(
    state: stored_state.StoredWorkState,
    item_id: ItemId,
    dependencies: tuple[ItemId, ...],
) -> stored_state.StoredWorkState:
    def revision_number(value: stored_state.ItemDefinitionRevision) -> int:
        return value.revision

    current = max(
        (value for value in state.lifecycle.definition_revisions if value.item_id == item_id),
        key=revision_number,
    )
    definition = replace(current.definition, dependencies=dependencies)
    digest = work_item_definition_digest(definition)
    assert isinstance(digest, str)
    definitions = tuple(
        replace(value, definition=definition, digest=digest, after_digest=digest)
        if value.item_id == item_id and value.revision == current.revision
        else value
        for value in state.lifecycle.definition_revisions
    )
    attempts = tuple(
        replace(value, accepted_scope_digest=digest)
        if value.item_id == item_id
        and value.accepted_scope_revision == current.revision
        and value.accepted_scope_digest == current.digest
        else value
        for value in state.lifecycle.attempts
    )
    links = tuple(value for value in state.lifecycle.dependencies if value.item_id != item_id) + tuple(
        stored_state.ItemDependency(item_id, dependency, position) for position, dependency in enumerate(dependencies)
    )
    return replace(
        state,
        lifecycle=replace(state.lifecycle, definition_revisions=definitions, dependencies=links, attempts=attempts),
    )


@contextmanager
def reject_table_deletes(table_name: str) -> Generator[None]:
    """Reject deletion of one unrelated live relation."""

    original_open = sqlite_store.open_database

    def guarded_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
        connection = original_open(path, mode)
        if mode == OpenMode.READ_WRITE:

            def authorize(
                action: int,
                argument: str | None,
                _secondary_argument: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_DELETE and argument == table_name:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
        return connection

    with patch.object(sqlite_store, "open_database", guarded_open):
        yield


@contextmanager
def reject_table_inserts(table_name: str) -> Generator[None]:
    """Inject a SQLite failure when one targeted relation is inserted."""

    original_open = sqlite_store.open_database

    def guarded_open(path: Path, mode: OpenMode) -> sqlite3.Connection:
        connection = original_open(path, mode)
        if mode == OpenMode.READ_WRITE:

            def authorize(
                action: int,
                argument: str | None,
                _secondary_argument: str | None,
                _database: str | None,
                _trigger: str | None,
            ) -> int:
                if action == sqlite3.SQLITE_INSERT and argument == table_name:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
        return connection

    with patch.object(sqlite_store, "open_database", guarded_open):
        yield


def _stored_item(
    item_id: ItemId,
    state: stored_state.StoredWorkItemState,
    *,
    outcome_evidence: str | None = None,
    sparse: bool = False,
    queue_position: int | None = 1,
    source: str | None = None,
) -> stored_state.StoredWorkItem:
    return stored_state.StoredWorkItem(
        item_id,
        state,
        None if sparse else work_models.Timing.MUST_NOW,
        source if source is not None else (None if sparse else "accepted requirement"),
        outcome_evidence,
        "continue" if state == stored_state.StoredWorkItemState.ACTIVE else "activate",
        None if sparse else "Current work remains bounded.",
        7,
        SQLITE_NOW,
        SQLITE_NOW,
        queue_position,
    )


def complete_sqlite_state() -> stored_state.StoredWorkState:
    item_a = ItemId("work-a")
    item_b = ItemId("work-b")
    item_c = ItemId("work-c")
    intake_item = ItemId("intake-work")
    proposal_item = ItemId("zz-proposal-a")
    attempt_id = AttemptId("work-a-1")
    attempt_lease_id = LeaseId("attempt-lease-a")
    brief = stored_state.ArtifactReference(
        ArtifactRefId(1),
        "work-a-brief",
        1,
        work_models.ArtifactKind.BRIEF,
        "artifacts/briefs/work-a-brief/1.opaque",
        SQLITE_DIGEST,
        100,
        3,
        SQLITE_NOW,
    )
    requirements = stored_state.ArtifactReference(
        ArtifactRefId(2),
        "work-a-requirements",
        1,
        work_models.ArtifactKind.REQUIREMENTS,
        "artifacts/requirements/work-a-requirements/1.md",
        SQLITE_DIGEST,
        200,
        3,
        SQLITE_NOW,
    )
    evidence = stored_state.ArtifactReference(
        ArtifactRefId(3),
        "work-a-evidence",
        1,
        work_models.ArtifactKind.EVIDENCE,
        "artifacts/evidence.md",
        SQLITE_DIGEST,
        50,
        4,
        SQLITE_NOW,
    )
    lifecycle = stored_state.LifecycleRecords(
        stored_state.ProjectRecord("pinboard", 5, 12, 2, SQLITE_NOW, SQLITE_NOW),
        (
            _stored_item(intake_item, stored_state.StoredWorkItemState.INTAKE, sparse=True, queue_position=1),
            _stored_item(item_a, stored_state.StoredWorkItemState.ACTIVE, queue_position=2),
            _stored_item(
                item_b,
                stored_state.StoredWorkItemState.SUPERSEDED,
                outcome_evidence="work-b superseded",
                queue_position=None,
            ),
            _stored_item(item_c, stored_state.StoredWorkItemState.READY, queue_position=3),
            _stored_item(
                proposal_item,
                stored_state.StoredWorkItemState.INTAKE,
                sparse=True,
                queue_position=4,
                source="proposal:zz-proposal-a",
            ),
        ),
        (stored_state.ItemDependency(item_a, item_c, 0), stored_state.ItemDependency(proposal_item, item_c, 0)),
        (
            stored_state.StoredAttempt(
                attempt_id,
                item_a,
                work_models.AttemptState.ACTIVE,
                "codex/work-a",
                "base-revision",
                "source-task",
                brief.artifact_ref_id,
                None,
                None,
                None,
                1,
                SQLITE_DIGEST,
                8,
                SQLITE_NOW,
                SQLITE_NOW,
            ),
        ),
        tuple(
            stored_state.ItemDefinitionRevision(
                item,
                1,
                test_definition(item)[1],
                test_definition(item)[0],
                "Accepted test definition.",
                TaskId("test-source"),
                None,
                test_definition(item)[1],
                3,
                SQLITE_NOW,
            )
            for item in (intake_item, item_a, item_b, item_c, proposal_item)
        ),
    )
    proposal_id = ProposalId(proposal_item)
    proposals = stored_state.ProposalRecords(
        (
            stored_state.StoredProposal(
                proposal_id,
                SQLITE_NOW,
                SQLITE_NOW,
                TaskId("source-task"),
                "Proposal A",
                "A related observation",
                "It may affect work C.",
                work_models.FollowUpProposalRelation(item_c),
                "Record the follow-up.",
                "A later task can assess it.",
                "No immediate scheduling impact.",
                None,
                4,
            ),
        ),
        (stored_state.ProposalEvidence(proposal_id, 0, "evidence:observation"),),
        (stored_state.ProposalFreshness(proposal_id, 0, "Work C remains live."),),
    )
    authority = stored_state.AuthorityRecords(
        (stored_state.AttemptLeaseCounter(attempt_id, 3),),
        (stored_state.AttemptLeaseGeneration(attempt_id, 3, attempt_lease_id, TaskId("worker"), HostId("host-a")),),
        (
            stored_state.StoredAttemptLease(
                attempt_id, 3, SQLITE_NOW, SQLITE_NOW + timedelta(minutes=5), authority_models.AttemptLeaseStatus.ACTIVE
            ),
        ),
    )
    transition_receipts = (
        stored_state.StoredTransitionReceipt(
            HistoryId(1),
            11,
            ActionId("continue:work-a-1"),
            decision_models.ActionKind.CONTINUE,
            HistorySubjectId("work-a-1"),
            evidence.artifact_ref_id,
            decision_models.AuthorizationKind.ATTEMPT,
            TaskId("worker"),
            HostId("host-a"),
            "empty/v1",
            work_models.CanonicalJson(b"{}"),
            "continued/v1",
            work_models.CanonicalJson(b"{}"),
            SQLITE_NOW,
        ),
    )
    return stored_state.StoredWorkState(
        lifecycle,
        proposals,
        (brief, requirements, evidence),
        authority,
        transition_receipts,
    )
