from pinboard.application import project_export as application_project_export
from pinboard.application import stored_state
from pinboard.cli import cli_commands, project_export

from .model import Box, Connector, Diagram, Guide, Note, Section

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "StoredWorkState": stored_state.StoredWorkState.__name__,
    "ExportCommand": cli_commands.ExportCommand.__name__,
    "export_project": project_export.export_project.__name__,
    "ProjectExport": application_project_export.ProjectExport.__name__,
    "project_export_from_state": application_project_export.project_export_from_state.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name)
    if renamed:
        raise ValueError(f"project-export visual references renamed source symbols: {', '.join(renamed)}")


DIAGRAM = Diagram(
    slug="project-export",
    title="One project-facts package crosses a read-only boundary",
    description=(
        "The export command captures one SQLite revision, projects the supported project facts, verifies every "
        "referenced accepted artifact, validates checkpoint and covered-completion provenance, and emits one "
        "revision-stamped portable JSON package with typed reusable evidence. Live preparation and "
        "attempt authority stay local. A human or another tool decides how to use the package; export changes no "
        "Pinboard state and writes to no receiving system."
    ),
    width=1200,
    height=560,
    sections=(
        Section("Pinboard authority", "project facts and accepted evidence", 28, 54),
        Section("Read-only project export", "complete before any output", 420, 54),
        Section("Portable boundary", "the recipient owns the next step", 760, 54),
    ),
    guides=(
        Guide((390, 42), (390, 470)),
        Guide((730, 42), (730, 470)),
    ),
    connectors=(
        Connector(((300, 200), (420, 200)), "ledger", "materialize", "read once", (360, 186)),
        Connector(((300, 390), (350, 390), (350, 280), (420, 280)), "artifacts", "materialize", "verify", (330, 338)),
        Connector(((670, 235), (740, 235)), "materialize", "package", "then emit", (705, 221)),
        Connector(((970, 235), (1010, 235)), "package", "consumer", "use", (990, 221)),
    ),
    boxes=(
        Box(
            "ledger",
            "One stored revision",
            "SQLite ledger",
            ("work · proposals · history", "replacements · dispositions"),
            ("StoredWorkState",),
            50,
            120,
            250,
            120,
        ),
        Box(
            "artifacts",
            "Accepted evidence",
            "Immutable artifacts",
            ("briefs · results · accepted packages",),
            ("verified exact bytes",),
            50,
            340,
            250,
            100,
        ),
        Box(
            "materialize",
            "Read-only command",
            "Build exported package",
            ("project facts from revision", "verify every evidence closure"),
            ("pinboard export --json",),
            420,
            170,
            250,
            130,
        ),
        Box(
            "package",
            "Portable output",
            "One JSON package",
            ("revision-stamped", "typed accepted evidence"),
            ("pinboard-project-export/v1",),
            740,
            170,
            230,
            130,
        ),
        Box(
            "consumer",
            "User-selected",
            "Team tool",
            ("human or LLM maps", "the exported package"),
            (),
            1010,
            170,
            160,
            130,
            "muted",
        ),
    ),
    notes=(
        Note(
            "LIVE AUTHORITY NOT EXPORTED · NO LIFECYCLE CHANGE · NO REMOTE WRITE · NO TOOL CHOSEN BY PINBOARD",
            600,
            505,
            12,
            "middle",
            True,
        ),
    ),
)
