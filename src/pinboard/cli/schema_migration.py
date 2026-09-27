"""Present the explicit, quiescent SQLite v6-to-v7 migration."""

from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.sqlite.database import migrate_v6_database
from pinboard.cli.cli_output import write_json
from pinboard.cli.work_state_models import SchemaMigrationView


def migrate_schema(roots: DurableRoots) -> int:
    changed = migrate_v6_database(roots.database_path)
    write_json(
        SchemaMigrationView(
            "pinboard-schema-migration/v1",
            "migrated" if changed else "unchanged",
            str(roots.database_path),
            "sqlite-v7",
            changed,
            "committed" if changed else "unchanged",
            "do-not-retry" if changed else "safe-to-repeat",
            ("ledger",) if changed else (),
        )
    )
    return 0
