"""Read only selected definitions and exact origin revisions in bounded keyed batches."""

import sqlite3
from itertools import batched

from pinboard.adapters.sqlite.decision_reads import read_current_replacements
from pinboard.adapters.sqlite.errors import StorageError, StorageErrorCode
from pinboard.adapters.sqlite.lifecycle import decode_definition_revision, read_current_definitions
from pinboard.application import obligation_coverage as coverage
from pinboard.domain import work_models
from pinboard.domain.errors import DecisionFailure
from pinboard.domain.identifiers import WorkItemId


def read_origins(
    connection: sqlite3.Connection, origins: tuple[work_models.ObligationOrigin, ...]
) -> frozenset[work_models.ObligationOrigin]:
    keys = tuple(dict.fromkeys((value.item_id, value.definition_revision) for value in origins))
    retained = set()
    for group in batched(keys, 200, strict=False):
        slots = ",".join("(?,?)" for _ in group)
        rows = connection.execute(
            f"""WITH selected(item_id, revision) AS (VALUES {slots})
            SELECT definition.item_id, definition_revision AS revision, definition_digest AS digest,
                   definition_json, reason, source_task_id, before_digest, after_digest,
                   accepted_project_revision, accepted_at
            FROM selected JOIN work_item_definition_revisions AS definition
            ON definition.item_id = selected.item_id AND definition.definition_revision = selected.revision""",
            tuple(value for key in group for value in key),
        )
        for row in rows:
            definition = decode_definition_revision(row)
            if isinstance(definition.definition, work_models.WorkItemDefinition):
                retained.update(
                    work_models.ObligationOrigin(definition.item_id, definition.revision, value.obligation_id)
                    for value in definition.definition.obligations
                )
    return frozenset(value for value in origins if value in retained)


def read_facts(
    connection: sqlite3.Connection, source_item: WorkItemId, replacement_item: WorkItemId | None
) -> coverage.CoverageFacts:
    relation_revision = None
    relations, _ = read_current_replacements(connection, (source_item,))
    relation = next(
        (value for value in relations if value.status == work_models.PlannedReplacementStatus.CURRENT), None
    )
    if relation is not None:
        if replacement_item is None:
            replacement_item = relation.replacement_item
        if replacement_item == relation.replacement_item:
            relation_revision = relation.relation_revision
    definitions = read_current_definitions(
        connection, (source_item,) if replacement_item is None else (source_item, replacement_item)
    )
    return coverage.CoverageFacts(
        source_item,
        replacement_item,
        definitions.get(source_item),
        None if replacement_item is None else definitions.get(replacement_item),
        relation_revision,
    )


def planned_report(connection: sqlite3.Connection, item_id: WorkItemId) -> coverage.CoverageReport | None:
    facts = read_facts(connection, item_id, None)
    if facts.replacement_item is None:
        return None
    result = coverage.report(facts)
    if isinstance(result, DecisionFailure):
        # A recorded relation must retain its definitions; corruption is an invariant defect.
        raise StorageError(StorageErrorCode.INVALID_STATE, result.message)
    return result
