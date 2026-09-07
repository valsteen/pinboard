from pathlib import Path

from .model import Box, Connector, Diagram, Guide, Note, Section

LAYER_DIRECTORIES: dict[str, str] = {
    "interfaces": "Turn outside input and optional setup context into exact commands and thematic use cases.",
    "application": "Request exact operation facts or complete state, then project accepted decisions into targeted mutations.",
    "domain": "Decide what is legal without reading files, issuing SQL, or presenting commands.",
    "adapters": "Store and recover accepted facts without deciding workflow policy.",
}

REQUIRED_ARCHITECTURE_HEADINGS = (
    "## Dependency direction",
    "### Domain",
    "### Application",
    "### Adapters",
    "### Interfaces",
)


def validate(root: Path) -> None:
    package = root / "src" / "pinboard"
    missing = tuple(name for name in LAYER_DIRECTORIES if not package.joinpath(name).is_dir())
    if missing:
        raise ValueError(f"layer visual references missing package directories: {', '.join(missing)}")
    architecture = root.joinpath("ARCHITECTURE.md").read_text(encoding="utf-8")
    missing_headings = tuple(heading for heading in REQUIRED_ARCHITECTURE_HEADINGS if heading not in architecture)
    if missing_headings:
        raise ValueError(f"layer visual lost its architecture authority: {', '.join(missing_headings)}")


DIAGRAM = Diagram(
    slug="layers",
    title="Four package layers with distinct responsibilities",
    description=(
        "A small interface root routes exact commands to thematic composition modules, application services coordinate "
        "operations, adapters implement capabilities, and domain code owns pure decisions. Every arrow is package "
        "dependency direction."
    ),
    width=1200,
    height=760,
    sections=(
        Section("Package dependency direction", "every arrow means: may depend on", 28, 42),
        Section("Decision center", "policy remains independent\nof interfaces and storage", 824, 330),
    ),
    guides=(
        Guide((250, 38), (1172, 38)),
        Guide((796, 326), (1172, 326)),
    ),
    connectors=(
        Connector(((360, 165), (450, 165)), "interfaces", "application", "operations", (405, 151)),
        Connector(((750, 165), (840, 165)), "application", "domain", "decisions", (795, 151)),
        Connector(
            ((160, 220), (160, 470), (450, 470)),
            "interfaces",
            "adapters",
            "composition",
            (305, 456),
        ),
        Connector(((600, 410), (600, 220)), "adapters", "application", "ports", (626, 322)),
        Connector(
            ((750, 470), (990, 470), (990, 220)),
            "adapters",
            "domain",
            "values",
            (870, 456),
        ),
    ),
    boxes=(
        Box(
            "interfaces",
            "Interfaces",
            "Make the request exact",
            ("small exhaustive route", "thematic composition · setup context"),
            ("src/pinboard/interfaces",),
            60,
            100,
            300,
            120,
        ),
        Box(
            "application",
            "Application",
            "Make the operation coherent",
            ("exact facts or full state · mutations", "own storage capability ports"),
            ("src/pinboard/application",),
            450,
            100,
            300,
            120,
        ),
        Box(
            "domain",
            "Domain",
            "Make the change legal",
            ("pure values and decisions", "accepted change or rejection"),
            ("src/pinboard/domain",),
            840,
            100,
            300,
            120,
        ),
        Box(
            "adapters",
            "Adapters",
            "Persist accepted facts",
            ("SQLite and filesystem mechanics", "implement application ports"),
            ("src/pinboard/adapters",),
            450,
            410,
            300,
            120,
        ),
    ),
    notes=(
        Note(
            "Runtime data flow appears in the next view. The interface root only routes; thematic interface modules own concrete composition.",
            28,
            690,
            12,
        ),
    ),
)
