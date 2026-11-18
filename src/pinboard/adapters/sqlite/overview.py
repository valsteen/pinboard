"""Select compact discovery facts inside the caller's read transaction.

Queue selection precedes definition projection. Dependency counts are aggregate facts, not
truncated lists; selected item reads own their complete targets and reasons.
"""

import sqlite3

import msgspec

from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.dependencies import read_accepted_checkpoints
from pinboard.adapters.sqlite.lifecycle import read_item_unblockers, read_selected_dependency_states
from pinboard.adapters.sqlite.replacements import read_current_replacements
from pinboard.application import query_models, stored_state
from pinboard.domain import work_models
from pinboard.domain.identifiers import WorkItemId


class _Revision(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int


class _Count(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    total_items: int


class _RoadmapCounts(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    initiative_count: int
    unassigned_count: int
    off_roadmap_count: int


class _OverviewRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: str
    state: work_models.WorkState
    position: int
    label: str
    attempt_id: str | None
    dependency_count: int
    unsatisfied_dependency_count: int


def _resume_needed_by_item(connection: sqlite3.Connection, rows: tuple[_OverviewRow, ...]) -> dict[str, bool]:
    held_ids = tuple(
        WorkItemId(row.item_id)
        for row in rows
        if row.state in {work_models.WorkState.PAUSED, work_models.WorkState.BLOCKED}
    )
    if not held_ids:
        return {}
    held_unblockers = read_item_unblockers(connection, held_ids)
    items_hold_ids = tuple(
        item_id for item_id in held_ids if isinstance(held_unblockers.get(item_id), work_models.WaitingForItemsToFinish)
    )
    if not items_hold_ids:
        return {}
    dependency_rows = read_selected_dependency_states(connection, items_hold_ids)
    accepted_checkpoints = read_accepted_checkpoints(
        connection, (row.target for values in dependency_rows.values() for row in values)
    )
    replacements, dispositions = read_current_replacements(connection, items_hold_ids)
    replacements_by_item = {relation.affected_item: relation for relation in replacements}
    dispositions_by_item = {
        (disposition.affected_item, disposition.relation_revision): disposition for disposition in dispositions
    }
    result: dict[str, bool] = {}
    for item_id in items_hold_ids:
        unsatisfied = tuple(
            row.target
            for row in dependency_rows.get(item_id, ())
            if not work_models.dependency_satisfied(
                row.target,
                stored_state.prerequisite_outcome(row.state) == work_models.PrerequisiteOutcome.FINISHED,
                accepted_checkpoints,
            )
        )
        relation = replacements_by_item.get(item_id)
        disposition = None if relation is None else dispositions_by_item.get((item_id, relation.relation_revision))
        replacement_resolved = relation is None or not work_models.replacement_withholds_resume(
            relation.status,
            relation.replacement_cost,
            None if disposition is None else disposition.accepted_cost,
        )
        result[str(item_id)] = work_models.resume_available(unsatisfied, replacement_resolved)
    return result


def read_overview_page(
    connection: sqlite3.Connection, after_position: int, max_items: int
) -> query_models.DiscoveryFacts:
    revision = decode_row(
        connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone(), _Revision
    ).revision
    rows = tuple(
        decode_row(row, _OverviewRow)
        for row in connection.execute(
            """
        WITH selected AS MATERIALIZED (
            SELECT item_id, state, queue_position FROM work_items
            WHERE queue_position > ? ORDER BY queue_position LIMIT ?
        )
        SELECT selected.item_id,
               CASE selected.state WHEN 'intake' THEN 'ready' ELSE selected.state END AS state,
               selected.queue_position AS position,
               substr(json_extract(definition.definition_json, '$.title'), 1, 160) AS label,
               attempt.attempt_id,
               (SELECT count(*) FROM item_dependencies AS dependency
                WHERE dependency.item_id = selected.item_id) AS dependency_count,
               (SELECT count(*) FROM item_dependencies AS dependency
                JOIN work_items AS prerequisite ON prerequisite.item_id = dependency.dependency_id
                WHERE dependency.item_id = selected.item_id
                  AND prerequisite.state NOT IN ('done', 'superseded')
                  AND (dependency.checkpoint_id IS NULL OR NOT EXISTS (
                      SELECT 1 FROM checkpoint_acceptances AS acceptance
                      WHERE acceptance.item_id = dependency.dependency_id
                        AND acceptance.checkpoint_id = dependency.checkpoint_id
                  ))) AS unsatisfied_dependency_count
        FROM selected
        LEFT JOIN work_item_definition_revisions AS definition
          ON definition.item_id = selected.item_id
         AND definition.definition_revision = (
             SELECT max(current.definition_revision) FROM work_item_definition_revisions AS current
             WHERE current.item_id = selected.item_id
         )
        LEFT JOIN attempts AS attempt ON attempt.item_id = selected.item_id AND attempt.state != 'done'
        ORDER BY selected.queue_position
        """,
            (after_position, max_items),
        ).fetchall()
    )
    resume_needed = _resume_needed_by_item(connection, rows)
    items = tuple(
        query_models.DiscoveryItem(
            row.item_id,
            row.state,
            row.position,
            row.label,
            row.attempt_id,
            row.dependency_count,
            row.unsatisfied_dependency_count,
            resume_needed.get(row.item_id, False),
        )
        for row in rows
    )
    next_row = connection.execute(
        """
        SELECT item.item_id FROM work_items AS item
        WHERE item.queue_position IS NOT NULL AND item.state IN ('intake', 'ready', 'blocked')
          AND NOT EXISTS (SELECT 1 FROM attempts WHERE attempts.item_id = item.item_id AND state != 'done')
        ORDER BY item.queue_position LIMIT 1
        """
    ).fetchone()
    roadmap_counts = decode_row(
        connection.execute(
            """SELECT
                   (SELECT count(*) FROM initiatives) AS initiative_count,
                   (SELECT count(*) FROM work_items AS item
                    WHERE item.queue_position IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM initiative_memberships AS member
                                      WHERE member.item_id = item.item_id AND member.removed_json IS NULL)
                      AND NOT EXISTS (SELECT 1 FROM item_off_roadmap_episodes AS episode
                                      WHERE episode.item_id = item.item_id AND episode.closed_json IS NULL)) AS unassigned_count,
                   (SELECT count(*) FROM item_off_roadmap_episodes WHERE closed_json IS NULL) AS off_roadmap_count"""
        ).fetchone(),
        _RoadmapCounts,
    )
    return query_models.DiscoveryFacts(
        str(revision),
        items,
        None if next_row is None else decode_row(next_row, query_models.DiscoveryNextUnstarted),
        decode_row(
            connection.execute("SELECT coalesce(max(queue_position), 0) AS total_items FROM work_items").fetchone(),
            _Count,
        ).total_items,
        roadmap_counts.initiative_count,
        roadmap_counts.unassigned_count,
        roadmap_counts.off_roadmap_count,
    )
