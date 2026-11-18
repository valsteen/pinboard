"""Select current consolidation relationships and their prerequisite closure; no retained-body scan."""

import sqlite3
from datetime import datetime

from pinboard.adapters.sqlite import initiatives as initiative_storage
from pinboard.adapters.sqlite.database import decode_row
from pinboard.adapters.sqlite.decision_reads import read_selected_decision_facts
from pinboard.adapters.sqlite.models import ItemIdRow
from pinboard.application import consolidation_models as models
from pinboard.application import query_models
from pinboard.domain import consolidation, initiatives, work_models


def read_facts(connection: sqlite3.Connection, plan: consolidation.Plan, now: datetime) -> models.Facts:
    selected = (*tuple(value.item_id for value in plan.sources), plan.campaign_id)
    sources = tuple(value.item_id for value in plan.sources)
    first = read_selected_decision_facts(
        connection, query_models.DecisionScope(selected, (), (), sources, (), (), (), (), (), ()), now
    ).snapshot
    primary = tuple(dict.fromkeys((*selected, *(item.work_item_id for item in first.items))))
    roots = tuple(
        dict.fromkeys((*primary, *(work_models.dependency_item(value) for value in plan.definition.dependencies)))
    )
    facts = read_selected_decision_facts(
        connection,
        query_models.DecisionScope(
            primary,
            (),
            roots,
            sources,
            (),
            (),
            (),
            (),
            (),
            tuple(
                dict.fromkeys(
                    (
                        *(
                            value
                            for value in plan.definition.dependencies
                            if isinstance(value, work_models.CheckpointDependency)
                        ),
                        *(
                            work_models.CheckpointDependency(plan.campaign_id, value.checkpoint)
                            for value in plan.checkpoint_targets
                        ),
                    )
                )
            ),
        ),
        now,
    )
    order = tuple(
        decode_row(row, ItemIdRow).item_id
        for row in connection.execute(
            "SELECT item_id FROM work_items WHERE queue_position IS NOT NULL ORDER BY queue_position"
        )
    )
    dispositions = tuple(
        initiative_storage.read_disposition(connection, item_id) for item_id in (*sources, plan.campaign_id)
    )
    return models.Facts(
        facts.snapshot,
        order,
        initiative_storage.read_membership_intent_facts(connection, initiatives.NoRelatedDisposition()),
        dispositions,
    )
