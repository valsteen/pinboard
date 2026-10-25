"""Initiative row selection and effects on a supplied connection; transaction lifetime belongs to the caller."""

import sqlite3
from typing import assert_never

import msgspec

from pinboard.adapters.sqlite.database import decode_row, require_one_changed_row, select_by_ids
from pinboard.adapters.sqlite.dependencies import DependencyRow, read_accepted_checkpoints
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import read_current_definitions
from pinboard.adapters.sqlite.models import ItemIdRow, ProjectRevisionRow
from pinboard.application import initiative_records as records
from pinboard.application import roadmap, stored_state
from pinboard.domain import initiatives as models
from pinboard.domain import work_models
from pinboard.domain.identifiers import WorkItemId


class SubjectRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: models.InitiativeId
    subject_revision: int


class RevisionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: models.InitiativeId
    revision: int
    evaluation_context: int
    definition_json: bytes
    provenance_json: bytes


class MembershipRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: models.InitiativeId
    item_id: WorkItemId
    episode: int
    added_json: bytes
    removed_json: bytes | None


class DecisionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: models.InitiativeId
    project_revision: int
    decision: str
    links_json: bytes
    provenance_json: bytes


class AssessmentRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: models.InitiativeId
    project_revision: int
    evaluation_context: int
    judgment: models.Judgment
    links_json: bytes
    provenance_json: bytes
    goal: str
    success_signal: str


class ConditionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_exists: int
    active: int


class EpisodeRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    episode: int


def _decode[Value](payload: bytes, value_type: type[Value]) -> Value:
    try:
        value = msgspec.json.decode(payload, type=value_type)
        if msgspec.json.encode(value, order="sorted") != payload:
            raise ValueError("Noncanonical initiative value")
        return value
    except (msgspec.DecodeError, ValueError) as error:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Invalid stored initiative value: {error}") from error


def _committed_provenance(row: DecisionRow | AssessmentRow) -> models.Provenance:
    provenance = _decode(row.provenance_json, records.ProvenanceRecord)
    if row.project_revision != provenance.project_revision:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            f"Initiative '{row.initiative_id}' history row at project revision {row.project_revision} "
            f"disagrees with its commit provenance revision {provenance.project_revision}.",
        )
    return records.provenance(provenance)


def _revision(row: RevisionRow) -> models.Revision:
    return models.Revision(
        row.initiative_id,
        row.revision,
        row.evaluation_context,
        records.definition(_decode(row.definition_json, records.DefinitionRecord)),
        records.provenance(_decode(row.provenance_json, records.ProvenanceRecord)),
    )


def read_current(connection: sqlite3.Connection, initiative_id: models.InitiativeId) -> models.CurrentFacts | None:
    subject = connection.execute(
        "SELECT initiative_id, subject_revision FROM initiatives WHERE initiative_id = ?", (initiative_id,)
    ).fetchone()
    if subject is None:
        return None
    row = connection.execute(
        "SELECT initiative_id, revision, evaluation_context, definition_json, provenance_json FROM initiative_revisions WHERE initiative_id = ? ORDER BY revision DESC LIMIT 1",
        (initiative_id,),
    ).fetchone()
    if row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, f"Initiative '{initiative_id}' has no definition revision.")
    current = models.CurrentFacts(
        decode_row(subject, SubjectRow).subject_revision, _revision(decode_row(row, RevisionRow))
    )
    if current.revision.revision > current.subject_revision:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            f"Initiative '{initiative_id}' definition revision exceeds its subject history.",
        )
    return current


def membership_condition(
    connection: sqlite3.Connection, initiative_id: models.InitiativeId, change: models.AddMember | models.RemoveMember
) -> tuple[bool, bool]:
    row = connection.execute(
        "SELECT EXISTS(SELECT 1 FROM work_items WHERE item_id = ?) AS item_exists, EXISTS(SELECT 1 FROM initiative_memberships WHERE initiative_id = ? AND item_id = ? AND removed_json IS NULL) AS active",
        (change.item_id, initiative_id, change.item_id),
    ).fetchone()
    selected = decode_row(row, ConditionRow)
    return bool(selected.item_exists), bool(selected.active)


def persist(  # noqa: C901, PLR0912 - exact effects for one closed mutation family
    connection: sqlite3.Connection,
    initiative_id: models.InitiativeId,
    change: models.Change,
    accepted: models.Accepted,
    provenance: models.Provenance,
) -> int:
    note = records.encode_provenance(provenance)
    current = read_current(connection, initiative_id)
    if isinstance(change, models.Create):
        connection.execute("INSERT INTO initiatives VALUES (?, ?)", (initiative_id, accepted.subject_revision))
    else:
        cursor = connection.execute(
            "UPDATE initiatives SET subject_revision = ? WHERE initiative_id = ? AND subject_revision = ?",
            (accepted.subject_revision, initiative_id, accepted.subject_revision - 1),
        )
        if require_one_changed_row(cursor, "Locked initiative revision changed.") is not None:
            raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Locked initiative revision changed.")
    match change:
        case models.Create(definition=definition) | models.Revise(definition=definition):
            if current is None or current.revision.evaluation_context != accepted.evaluation_context:
                connection.execute(
                    "INSERT INTO initiative_contexts VALUES (?, ?, ?, ?)",
                    (initiative_id, accepted.evaluation_context, definition.goal, definition.success_signal),
                )
            else:
                row = connection.execute(
                    "SELECT initiative_id, evaluation_context, goal, success_signal FROM initiative_contexts WHERE initiative_id = ? AND evaluation_context = ?",
                    (initiative_id, accepted.evaluation_context),
                ).fetchone()
                if row is None:
                    raise StorageError(
                        StorageErrorCode.INVALID_STATE,
                        f"Initiative '{initiative_id}' is missing its current evaluation context.",
                    )
                context = decode_row(row, ContextRow)
                if (context.goal, context.success_signal) != (definition.goal, definition.success_signal):
                    raise StorageError(
                        StorageErrorCode.INVALID_STATE,
                        f"Initiative '{initiative_id}' current evaluation context disagrees with its definition.",
                    )
            connection.execute(
                "INSERT INTO initiative_revisions VALUES (?, ?, ?, ?, ?)",
                (
                    initiative_id,
                    accepted.subject_revision,
                    accepted.evaluation_context,
                    records.encode_definition(definition),
                    note,
                ),
            )
        case models.AddMember(item_id=item_id):
            row = connection.execute(
                "SELECT coalesce(max(episode), 0) + 1 AS episode FROM initiative_memberships WHERE initiative_id = ? AND item_id = ?",
                (initiative_id, item_id),
            ).fetchone()
            episode = decode_row(row, EpisodeRow).episode
            connection.execute(
                "INSERT INTO initiative_memberships VALUES (?, ?, ?, ?, NULL)", (initiative_id, item_id, episode, note)
            )
        case models.RemoveMember(item_id=item_id):
            cursor = connection.execute(
                "UPDATE initiative_memberships SET removed_json = ? WHERE initiative_id = ? AND item_id = ? AND removed_json IS NULL",
                (note, initiative_id, item_id),
            )
            if require_one_changed_row(cursor, "Locked initiative membership changed.") is not None:
                raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Locked initiative membership changed.")
        case models.RecordDecision(decision=decision, supporting_links=links):
            connection.execute(
                "INSERT INTO initiative_decisions VALUES (?, ?, ?, ?, ?)",
                (initiative_id, provenance.project_revision, decision, msgspec.json.encode(links), note),
            )
        case models.Assess(judgment=judgment, supporting_links=links):
            connection.execute(
                "INSERT INTO initiative_assessments VALUES (?, ?, ?, ?, ?, ?)",
                (
                    initiative_id,
                    provenance.project_revision,
                    accepted.evaluation_context,
                    judgment.value,
                    msgspec.json.encode(links),
                    note,
                ),
            )
        case _ as unreachable:
            assert_never(unreachable)
    cursor = connection.execute(
        "UPDATE project_meta SET revision = ?, updated_at = ? WHERE singleton = 1 AND revision = ?",
        (provenance.project_revision, provenance.committed_at.isoformat(), provenance.project_revision - 1),
    )
    if require_one_changed_row(cursor, "Locked project revision changed.") is not None:
        raise StorageError(StorageErrorCode.INVARIANT_VIOLATION, "Locked project revision changed.")
    return provenance.project_revision


def read_selected(
    connection: sqlite3.Connection, initiative_ids: tuple[models.InitiativeId, ...]
) -> tuple[models.Initiative, ...]:
    subjects = tuple(
        decode_row(row, SubjectRow)
        for row in select_by_ids(
            connection,
            "SELECT initiative_id, subject_revision FROM initiatives WHERE initiative_id IN ({ids}) ORDER BY initiative_id",
            initiative_ids,
        )
    )
    revisions: dict[models.InitiativeId, list[models.Revision]] = {subject.initiative_id: [] for subject in subjects}
    memberships: dict[models.InitiativeId, list[models.Membership]] = {
        subject.initiative_id: [] for subject in subjects
    }
    decisions: dict[models.InitiativeId, list[models.Decision]] = {subject.initiative_id: [] for subject in subjects}
    assessments: dict[models.InitiativeId, list[models.Assessment]] = {
        subject.initiative_id: [] for subject in subjects
    }
    for row in select_by_ids(
        connection,
        "SELECT initiative_id, revision, evaluation_context, definition_json, provenance_json FROM initiative_revisions WHERE initiative_id IN ({ids}) ORDER BY initiative_id, revision",
        initiative_ids,
    ):
        selected = decode_row(row, RevisionRow)
        revisions[selected.initiative_id].append(_revision(selected))
    for row in select_by_ids(
        connection,
        "SELECT initiative_id, item_id, episode, added_json, removed_json FROM initiative_memberships WHERE initiative_id IN ({ids}) ORDER BY initiative_id, item_id, episode",
        initiative_ids,
    ):
        member = decode_row(row, MembershipRow)
        memberships[member.initiative_id].append(
            models.Membership(
                member.initiative_id,
                member.item_id,
                member.episode,
                records.provenance(_decode(member.added_json, records.ProvenanceRecord)),
                None
                if member.removed_json is None
                else records.provenance(_decode(member.removed_json, records.ProvenanceRecord)),
            )
        )
    for row in select_by_ids(
        connection,
        "SELECT initiative_id, project_revision, decision, links_json, provenance_json FROM initiative_decisions WHERE initiative_id IN ({ids}) ORDER BY initiative_id, project_revision",
        initiative_ids,
    ):
        decision = decode_row(row, DecisionRow)
        decisions[decision.initiative_id].append(
            models.Decision(
                decision.initiative_id,
                decision.decision,
                _decode(decision.links_json, tuple[str, ...]),
                _committed_provenance(decision),
            )
        )
    for row in select_by_ids(
        connection,
        "SELECT a.initiative_id, a.project_revision, a.evaluation_context, a.judgment, a.links_json, a.provenance_json, c.goal, c.success_signal FROM initiative_assessments AS a JOIN initiative_contexts AS c USING (initiative_id, evaluation_context) WHERE a.initiative_id IN ({ids}) ORDER BY a.initiative_id, a.project_revision",
        initiative_ids,
    ):
        assessment = decode_row(row, AssessmentRow)
        assessments[assessment.initiative_id].append(
            models.Assessment(
                assessment.initiative_id,
                assessment.evaluation_context,
                assessment.goal,
                assessment.success_signal,
                assessment.judgment,
                _decode(assessment.links_json, tuple[str, ...]),
                _committed_provenance(assessment),
            )
        )
    for subject in subjects:
        selected_revisions = revisions[subject.initiative_id]
        if (
            not selected_revisions
            or selected_revisions[0].revision != 1
            or selected_revisions[-1].revision > subject.subject_revision
        ):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Initiative '{subject.initiative_id}' definition revisions differ from its subject history.",
            )
    return tuple(
        models.Initiative(
            subject.initiative_id,
            subject.subject_revision,
            (revisions[subject.initiative_id][0], *revisions[subject.initiative_id][1:]),
            tuple(memberships[subject.initiative_id]),
            tuple(decisions[subject.initiative_id]),
            tuple(assessments[subject.initiative_id]),
        )
        for subject in subjects
    )


def read_all(connection: sqlite3.Connection) -> tuple[models.Initiative, ...]:
    ids = tuple(
        decode_row(row, SubjectRow).initiative_id
        for row in connection.execute(
            "SELECT initiative_id, subject_revision FROM initiatives ORDER BY initiative_id"
        ).fetchall()
    )
    return read_selected(connection, ids)


class RoadmapItemRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: WorkItemId
    state: stored_state.StoredWorkItemState
    queue_position: int | None


def read_roadmap(connection: sqlite3.Connection) -> roadmap.Facts:
    """Read initiative history, live/unassigned items and selected member dependency closure with batched keys."""
    project_row = connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone()
    if project_row is None:
        raise StorageError(StorageErrorCode.INVALID_STATE, "Project metadata is missing.")
    project_revision = decode_row(project_row, ProjectRevisionRow).revision
    initiatives = read_all(connection)
    selected = {member.item_id for initiative in initiatives for member in initiative.memberships}
    selected.update(
        decode_row(row, ItemIdRow).item_id
        for row in connection.execute("SELECT item_id FROM work_items WHERE queue_position IS NOT NULL").fetchall()
    )
    items: dict[WorkItemId, RoadmapItemRow] = {}
    definitions: dict[WorkItemId, stored_state.ItemDefinitionRevision] = {}
    dependencies: dict[WorkItemId, list[work_models.Dependency]] = {}
    pending = tuple(sorted(selected))
    while pending:
        batch = tuple(
            decode_row(row, RoadmapItemRow)
            for row in select_by_ids(
                connection,
                "SELECT item_id, state, queue_position FROM work_items WHERE item_id IN ({ids}) ORDER BY item_id",
                pending,
            )
        )
        items.update((item.item_id, item) for item in batch)
        definitions.update(read_current_definitions(connection, pending))
        unfinished = tuple(item.item_id for item in batch if roadmap.is_unfinished(item.state))
        discovered: set[WorkItemId] = set()
        for row in select_by_ids(
            connection,
            "SELECT item_id, dependency_id, checkpoint_id FROM item_dependencies WHERE item_id IN ({ids}) ORDER BY item_id, position",
            unfinished,
        ):
            dependency = decode_row(row, DependencyRow)
            dependencies.setdefault(dependency.item_id, []).append(dependency.target)
            if dependency.dependency_id not in selected:
                discovered.add(dependency.dependency_id)
        selected.update(discovered)
        pending = tuple(sorted(discovered))
    return roadmap.Facts(
        project_revision,
        initiatives,
        tuple(
            sorted(
                (
                    roadmap.Item(
                        item.item_id,
                        definitions[item.item_id].definition.title,
                        item.state,
                        tuple(dependencies.get(item.item_id, ())),
                        item.queue_position,
                    )
                    for item in items.values()
                ),
                key=roadmap.item_order,
            )
        ),
        read_accepted_checkpoints(connection, (target for targets in dependencies.values() for target in targets)),
    )


class ContextRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_id: models.InitiativeId
    evaluation_context: int
    goal: str
    success_signal: str


def validate_all(connection: sqlite3.Connection, values: tuple[models.Initiative, ...], project_revision: int) -> None:
    contexts = {
        (row.initiative_id, row.evaluation_context): (row.goal, row.success_signal)
        for row in (
            decode_row(value, ContextRow)
            for value in connection.execute(
                "SELECT initiative_id, evaluation_context, goal, success_signal FROM initiative_contexts"
            ).fetchall()
        )
    }
    expected_contexts: dict[tuple[models.InitiativeId, int], tuple[str, str]] = {}
    revisions: set[int] = set()
    for initiative in values:
        notes = [value.provenance for value in initiative.revisions]
        context = 0
        previous: tuple[str, str] | None = None
        for revision in initiative.revisions:
            pair = (revision.definition.goal, revision.definition.success_signal)
            if pair != previous:
                context += 1
            if revision.evaluation_context != context:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    f"Initiative '{initiative.initiative_id}' has an invalid evaluation context chain.",
                )
            expected_contexts[(initiative.initiative_id, context)] = pair
            previous = pair
        episodes: dict[WorkItemId, int] = {}
        for member in initiative.memberships:
            expected = episodes.get(member.item_id, 0) + 1
            if member.episode != expected:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    f"Initiative '{initiative.initiative_id}' has an invalid membership episode chain.",
                )
            episodes[member.item_id] = expected
            notes.append(member.added)
            if member.removed is not None:
                notes.append(member.removed)
        notes.extend(value.provenance for value in initiative.decisions)
        notes.extend(value.provenance for value in initiative.assessments)
        if initiative.subject_revision != len(notes):
            raise StorageError(
                StorageErrorCode.INVALID_STATE,
                f"Initiative '{initiative.initiative_id}' subject revision differs from its complete history.",
            )
        for note in notes:
            if note.project_revision in revisions or not 1 <= note.project_revision <= project_revision:
                raise StorageError(
                    StorageErrorCode.INVALID_STATE,
                    f"Initiative '{initiative.initiative_id}' has conflicting commit provenance.",
                )
            revisions.add(note.project_revision)
    if contexts != expected_contexts:
        raise StorageError(
            StorageErrorCode.INVALID_STATE,
            "Initiative evaluation contexts differ from their preserved definition history.",
        )
