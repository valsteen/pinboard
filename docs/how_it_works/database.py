import sqlite3
from pathlib import Path

from .model import Box, Connector, Diagram, Guide, Note, Section

TABLE_GROUPS: dict[str, str] = {
    "work_items": "current work",
    "work_item_state_counts": "current work",
    "attempts": "current work",
    "work_item_definition_revisions": "definitions and relationships",
    "item_dependencies": "scope and relationships",
    "planned_replacements": "scope and relationships",
    "replacement_dispositions": "scope and relationships",
    "proposals": "discovery",
    "proposal_evidence": "discovery",
    "proposal_freshness": "discovery",
    "artifact_refs": "durable knowledge",
    "attempt_lease_counters": "authority",
    "attempt_lease_generations": "authority",
    "attempt_leases": "authority",
    "preparation_lease_counters": "authority",
    "preparation_lease_generations": "authority",
    "preparation_leases": "authority",
    "project_meta": "integrity and time",
    "transition_history": "integrity and time",
}

RELATION_ROLES: dict[tuple[str, str], str] = {
    ("attempt_lease_counters", "attempts"): "authority belongs to an attempt",
    ("attempt_lease_generations", "attempt_lease_counters"): "generations never move backwards",
    ("attempt_leases", "attempt_lease_counters"): "one current lease per attempt",
    ("attempt_leases", "attempt_lease_generations"): "lease identity is fenced by generation",
    ("attempts", "artifact_refs"): "brief and result evidence",
    ("attempts", "work_items"): "attempt executes one item",
    ("preparation_lease_counters", "work_items"): "preparation belongs to one ready item",
    ("preparation_lease_generations", "preparation_lease_counters"): "preparation generations never move backwards",
    ("preparation_leases", "preparation_lease_counters"): "one retained preparation claim per item",
    ("preparation_leases", "preparation_lease_generations"): "preparation identity is fenced by generation",
    ("preparation_leases", "work_item_definition_revisions"): "preparation pins one accepted definition",
    ("item_dependencies", "work_items"): "items form a dependency graph",
    ("planned_replacements", "work_items"): "a replacement explicitly links two exact items",
    ("replacement_dispositions", "planned_replacements"): "temporary retention pins one relation revision",
    ("work_item_definition_revisions", "work_items"): "definition history belongs to an item",
    ("proposal_evidence", "proposals"): "discovery retains its evidence",
    ("proposal_freshness", "proposals"): "discovery retains assumptions",
    ("proposals", "work_items"): "proposal relation or disposition targets work",
    ("transition_history", "artifact_refs"): "history may retain accepted evidence",
}


def _schema_shape(root: Path) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        schema = root.joinpath("src/pinboard/adapters/sqlite/schema.sql").read_text(encoding="utf-8")
        connection.executescript(schema)
        tables = frozenset(
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        relations: set[tuple[str, str]] = set()
        for table in tables:
            quoted = table.replace('"', '""')
            for row in connection.execute(f'PRAGMA foreign_key_list("{quoted}")'):
                relations.add((table, str(row["table"])))
        return tables, frozenset(relations)
    finally:
        connection.close()


def validate(root: Path) -> None:
    tables, relations = _schema_shape(root)
    if set(TABLE_GROUPS) != tables:
        missing = sorted(tables - set(TABLE_GROUPS))
        removed = sorted(set(TABLE_GROUPS) - tables)
        raise ValueError(f"database visual table coverage changed; ungrouped={missing}, removed={removed}")
    if set(RELATION_ROLES) != relations:
        missing = sorted(relations - set(RELATION_ROLES))
        removed = sorted(set(RELATION_ROLES) - relations)
        raise ValueError(f"database visual relationship coverage changed; ungrouped={missing}, removed={removed}")


DIAGRAM = Diagram(
    slug="database",
    title="Six kinds of memory in one relational ledger",
    description=(
        "Nineteen SQLite tables preserve work identity, bounded status counts, definitions, explicit replacement decisions, proposals, artifacts, mutation ownership, and history. "
        "Relationship families are grouped for readability while the source seed accounts for every foreign key."
    ),
    width=1200,
    height=850,
    sections=(
        Section("Proposals", "original intake facts plus later disposition", 28, 42),
        Section("Current work", "identity that survives execution", 424, 42),
        Section("Definitions + relationships", "accepted intent and dependency", 824, 42),
        Section("Accepted files", "briefs · ready reviews · checkpoint evidence", 28, 432),
        Section("Mutation ownership", "ready preparation · active attempts", 424, 432),
        Section("Integrity + time", "current revision and committed receipts", 824, 432),
    ),
    guides=(
        Guide((400, 32), (400, 770)),
        Guide((800, 32), (800, 770)),
        Guide((24, 390), (1176, 390)),
    ),
    connectors=(
        Connector(((270, 155), (430, 155)), "proposals", "work-items", "relation / target", (350, 143)),
        Connector(((640, 155), (570, 155)), "attempts", "work-items", "executes", (605, 143)),
        Connector(((780, 155), (830, 155)), "attempts", "definitions", "definition", (805, 143)),
        Connector(((500, 110), (500, 80), (1100, 80), (1100, 110)), "work-items", "dependencies"),
        Connector(
            ((105, 260), (105, 230), (140, 230), (140, 200)), "proposal-evidence", "proposals", "supports", (72, 226)
        ),
        Connector(
            ((275, 260), (275, 230), (220, 230), (220, 200)), "proposal-freshness", "proposals", "recheck", (310, 226)
        ),
        Connector(
            ((695, 620), (695, 650)), "attempt-authority", "preparation-authority", "same fencing pattern", (715, 638)
        ),
        Connector(((975, 590), (975, 650)), "meta", "history", "each revision", (1017, 620)),
    ),
    boxes=(
        Box(
            "proposals",
            "",
            "Proposal facts",
            ("same-id intake work",),
            ("later disposition",),
            90,
            110,
            180,
            90,
        ),
        Box("proposal-evidence", "", "Evidence", ("why it was raised",), ("proposal_evidence",), 40, 260, 150, 90),
        Box("proposal-freshness", "", "Assumptions", ("facts to recheck",), ("proposal_freshness",), 210, 260, 170, 90),
        Box(
            "work-items",
            "",
            "Work items",
            ("identity + totals",),
            ("work_items",),
            430,
            110,
            140,
            90,
        ),
        Box("attempts", "", "Attempts", ("one execution",), ("attempts",), 640, 110, 140, 90),
        Box(
            "definitions",
            "",
            "Accepted versions",
            ("definition + relation",),
            ("versioned retention",),
            830,
            110,
            175,
            90,
        ),
        Box("dependencies", "", "Dependencies", ("item → prerequisite",), ("item_dependencies",), 1020, 110, 160, 90),
        Box(
            "artifact-refs",
            "",
            "Accepted artifacts",
            ("requirements · brief · result", "ready + checkpoint review evidence"),
            ("artifact_refs",),
            50,
            500,
            300,
            150,
        ),
        Box(
            "attempt-authority",
            "",
            "Attempt authority",
            ("worker owns attempt", "fenced generations"),
            ("attempt_lease_*",),
            600,
            500,
            190,
            120,
        ),
        Box(
            "preparation-authority",
            "",
            "Preparation authority",
            ("ready item + definition pin", "counter · identities · current lease"),
            ("preparation_lease_*",),
            410,
            650,
            380,
            100,
        ),
        Box("meta", "", "Project state", ("revision + host epoch",), ("project_meta",), 900, 500, 160, 90),
        Box(
            "history", "", "Committed history", ("input + outcome + actor",), ("transition_history",), 890, 650, 170, 90
        ),
    ),
    notes=(
        Note("SELECTED RELATIONSHIPS SHOWN · EVERY FOREIGN KEY STILL CHECKED BY THE SEED", 28, 790, 12, meta=True),
        Note(
            "Briefs and accepted results link to attempts. Ready-review evidence is reused by exact identity. Accepted checkpoint review evidence links to history.",
            28,
            816,
            12,
        ),
        Note(
            "Preparation and attempt authority are item-scoped; project actions rely on SQLite transactions.",
            28,
            834,
            12,
        ),
    ),
)
