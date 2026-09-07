from pinboard.adapters.sqlite.store import SQLiteWorkStore
from pinboard.application import service
from pinboard.application.mutation_models import PreparationAuthorityMutation
from pinboard.domain import authority_models
from pinboard.domain.authority_decisions import decide_preparation_authority
from pinboard.interfaces import cli_commands, preparation_authority, work_views

from .model import Box, Connector, Diagram, Guide, Note, Section

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "PreparationStartCommand": cli_commands.PreparationStartCommand.__name__,
    "start_preparation": preparation_authority.start_preparation.__name__,
    "AcquireInitialPreparationAuthority": authority_models.AcquireInitialPreparationAuthority.__name__,
    "TransferPreparationAuthority": authority_models.TransferPreparationAuthority.__name__,
    "decide_preparation_authority": decide_preparation_authority.__name__,
    "PreparationAuthorityMutation": PreparationAuthorityMutation.__name__,
    "SQLiteWorkStore": SQLiteWorkStore.__name__,
    "write": SQLiteWorkStore.write.__name__,
    "refresh": work_views.refresh.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name)
    if service.start_preparation.__name__ != "start_preparation":
        renamed += ("application.start_preparation",)
    if renamed:
        raise ValueError(f"journey visual references renamed source symbols: {', '.join(renamed)}")


DIAGRAM = Diagram(
    slug="journey",
    title="One change tells one ordered story",
    description=(
        "An ordinary preparation start is decoded, samples its operation time, selects the current definition and "
        "initial acquisition or inactive transfer against locked state, commits the targeted mutation atomically, "
        "then refreshes repairable views and presents the exact committed claim."
    ),
    width=1400,
    height=820,
    sections=(
        Section("Interface", "decode / sample time\npresent", 28, 118),
        Section("Application", "lock / select / project", 28, 298),
        Section("Domain", "decide / reject", 28, 478),
        Section("Adapter", "commit / write projections", 28, 658),
    ),
    guides=(
        Guide((150, 48), (150, 764)),
        Guide((24, 218), (1376, 218)),
        Guide((24, 398), (1376, 398)),
        Guide((24, 578), (1376, 578)),
    ),
    connectors=(
        Connector(((320, 130), (350, 130)), "request", "command", "decode", (335, 116)),
        Connector(((520, 130), (550, 130)), "command", "observed", "sample", (535, 116)),
        Connector(((730, 130), (760, 130)), "observed", "requested", "parameters", (745, 116)),
        Connector(
            ((855, 172), (855, 214), (670, 214), (670, 244)), "requested", "locked", "open transaction", (786, 202)
        ),
        Connector(((670, 346), (670, 370), (620, 370), (620, 430)), "locked", "decision", "select current", (662, 358)),
        Connector(((520, 472), (430, 472)), "decision", "rejection", "rejected", (475, 458)),
        Connector(((620, 430), (620, 388), (920, 388), (920, 346)), "decision", "mutation", "accepted", (760, 376)),
        Connector(((920, 346), (920, 578), (870, 578), (870, 620)), "mutation", "transaction", "commit", (950, 510)),
        Connector(
            ((870, 722), (870, 750), (470, 750), (470, 481), (430, 481)),
            "transaction",
            "rejection",
            "stale",
            (650, 738),
        ),
        Connector(((965, 671), (1000, 671)), "transaction", "views", "interface refresh", (982, 659)),
        Connector(((1090, 620), (1090, 172)), "views", "latest", "keep receipt", (1125, 410)),
        Connector(((1190, 130), (1210, 130)), "latest", "result", "present", (1200, 116)),
    ),
    boxes=(
        Box("request", "Request", "start claim", (), ("CLI / JSON",), 170, 88, 150, 84, "muted"),
        Box(
            "command",
            "Exact command",
            "Decoded leaf",
            (),
            ("exact CLI leaf",),
            350,
            88,
            170,
            84,
        ),
        Box("observed", "Operation time", "Boundary sample", (), ("expiry semantics",), 550, 88, 180, 84),
        Box(
            "requested",
            "Start input",
            "Item · owner · TTL",
            (),
            ("no stale receipt",),
            760,
            88,
            190,
            84,
        ),
        Box("latest", "Committed claim", "Transaction result", (), ("authoritative",), 990, 88, 200, 84),
        Box("result", "Presented result", "Return status", (), ("exact claim",), 1210, 88, 170, 84),
        Box(
            "locked",
            "Application use case",
            "Read and select",
            ("definition + claim operation",),
            ("authoritative",),
            560,
            244,
            210,
            102,
        ),
        Box(
            "mutation",
            "Targeted mutation",
            "Project accepted facts",
            ("receipt + authority delta",),
            ("targeted delta",),
            800,
            244,
            240,
            102,
        ),
        Box(
            "rejection",
            "Expected rejection",
            "No stored change",
            ("code · facts · retry",),
            ("fresh alternatives",),
            220,
            430,
            210,
            102,
            "muted",
        ),
        Box(
            "decision",
            "Decide legality",
            "Use selected change",
            ("accept or reject",),
            ("pure decision",),
            520,
            430,
            200,
            102,
        ),
        Box(
            "transaction",
            "Guarded commit",
            "Persist mutation",
            ("commit or roll back",),
            ("SQLite write",),
            775,
            620,
            190,
            102,
        ),
        Box(
            "views",
            "File adapter",
            "Write projections",
            ("warning is repairable",),
            ("replaceable",),
            1000,
            620,
            180,
            102,
            "muted",
        ),
    ),
    notes=(
        Note(
            "Locked selection rejects with fresh alternatives. Accepted SQLite changes precede repairable view refresh and exact presentation.",
            190,
            783,
            12,
        ),
    ),
)
