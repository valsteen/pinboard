"""Select compact discovery facts inside the caller's read transaction.

Queue selection precedes definition projection. Dependency counts are aggregate facts, not
truncated lists; selected item reads own their complete targets and reasons.
"""

import sqlite3

import msgspec

from pinboard.adapters.sqlite.database import decode_row
from pinboard.application import query_models


class _Revision(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int


class _Count(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    total_items: int


def read_overview_page(
    connection: sqlite3.Connection, after_position: int, max_items: int
) -> query_models.DiscoveryFacts:
    revision = decode_row(
        connection.execute("SELECT revision FROM project_meta WHERE singleton = 1").fetchone(), _Revision
    ).revision
    rows = connection.execute(
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
    items = tuple(decode_row(row, query_models.DiscoveryItem) for row in rows)
    next_row = connection.execute(
        """
        SELECT item.item_id FROM work_items AS item
        WHERE item.queue_position IS NOT NULL AND item.state IN ('intake', 'ready', 'blocked', 'deferred')
          AND NOT EXISTS (SELECT 1 FROM attempts WHERE attempts.item_id = item.item_id AND state != 'done')
        ORDER BY item.queue_position LIMIT 1
        """
    ).fetchone()
    return query_models.DiscoveryFacts(
        str(revision),
        items,
        None if next_row is None else decode_row(next_row, query_models.DiscoveryNextUnstarted),
        decode_row(
            connection.execute("SELECT coalesce(max(queue_position), 0) AS total_items FROM work_items").fetchone(),
            _Count,
        ).total_items,
    )
