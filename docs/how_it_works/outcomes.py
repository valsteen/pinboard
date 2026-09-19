from dataclasses import fields

from pinboard.application import service
from pinboard.domain.authority_decisions import decide_preparation_authority
from pinboard.domain.errors import DecisionFailure, FailureDetails
from pinboard.interfaces import cli_commands, preparation_authority
from pinboard.interfaces.cli_output import RejectedOperationView, write_operation_rejection

from .model import Box, Connector, Diagram, Guide, Note, Section

FAILURE_DETAIL_FIELDS = frozenset(
    {
        "observed",
        "mismatches",
        "retry",
        "effect",
        "changed_surfaces",
        "alternatives",
    }
)

REJECTED_OPERATION_FIELDS = frozenset(
    {
        "schema",
        "status",
        "operation",
        "code",
        "message",
        "state_changed",
        "changed_surfaces",
        "observed",
        "mismatches",
        "retry",
        "next_actions",
    }
)

SOURCE_SYMBOL_NAMES: dict[str, str] = {
    "PreparationStartCommand": cli_commands.PreparationStartCommand.__name__,
    "start_preparation": preparation_authority.start_preparation.__name__,
    "application.start_preparation": service.start_preparation.__name__,
    "decide_preparation_authority": decide_preparation_authority.__name__,
    "DecisionFailure": DecisionFailure.__name__,
    "FailureDetails": FailureDetails.__name__,
    "RejectedOperationView": RejectedOperationView.__name__,
    "write_operation_rejection": write_operation_rejection.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name.split(".")[-1])
    if renamed:
        raise ValueError(f"outcome visual references renamed source symbols: {', '.join(renamed)}")
    failure_fields = frozenset(field.name for field in fields(FailureDetails))
    rejected_fields = frozenset(RejectedOperationView.__struct_fields__)
    if failure_fields != FAILURE_DETAIL_FIELDS or rejected_fields != REJECTED_OPERATION_FIELDS:
        drifted = sorted(failure_fields ^ FAILURE_DETAIL_FIELDS | rejected_fields ^ REJECTED_OPERATION_FIELDS)
        raise ValueError(f"outcome visual and structured failure contract differ: {', '.join(drifted)}")


DIAGRAM = Diagram(
    slug="outcomes",
    title="Exact operation outcomes become actionable guidance",
    description=(
        "A preparation start carries exact input and selected current state into a pure decision. An expected "
        "rejection preserves observations, mismatches, unchanged effect, retry disposition, and legal alternatives. "
        "An accepted decision attempts the SQLite effect and returns the committed claim or structured failure facts. "
        "The interface explains what happened, whether state changed, and the bounded next action."
    ),
    width=1400,
    height=820,
    sections=(
        Section("Preparation start", "one request moves from selected facts to an attempted effect", 28, 42),
        Section("Qualified outcomes", "decision-relevant facts survive the return path", 28, 330),
        Section("Response owner", "presentation policy stays at the interface", 28, 630),
    ),
    guides=(
        Guide((176, 38), (1372, 38)),
        Guide((170, 326), (1372, 326)),
        Guide((162, 626), (1372, 626)),
    ),
    connectors=(
        Connector(((410, 145), (470, 145)), "request", "state", "select", (440, 130)),
        Connector(((740, 145), (810, 145)), "state", "decision", "decide", (775, 130)),
        Connector(((1040, 145), (1110, 145)), "decision", "effect", "accepted", (1075, 130)),
        Connector(
            ((925, 200), (925, 290), (700, 290), (700, 360)),
            "decision",
            "rejection",
            "rejected",
            (812, 276),
        ),
        Connector(((1240, 200), (1240, 360)), "effect", "effect-outcome", "result", (1272, 282)),
        Connector(
            ((700, 540), (700, 590), (790, 590), (790, 660)),
            "rejection",
            "guidance",
            "explain",
            (745, 577),
        ),
        Connector(
            ((1205, 540), (1205, 610), (1010, 610), (1010, 660)),
            "effect-outcome",
            "guidance",
            "reconcile",
            (1108, 597),
        ),
    ),
    boxes=(
        Box(
            "request",
            "Exact input",
            "Preparation start",
            ("item · task · host · TTL",),
            ("PreparationStartCommand",),
            160,
            90,
            250,
            110,
        ),
        Box(
            "state",
            "Locked state",
            "Select current facts",
            ("definition · claim · dependencies",),
            ("application-owned read",),
            470,
            90,
            270,
            110,
        ),
        Box(
            "decision",
            "Pure decision",
            "Accept or reject",
            ("no I/O · no presentation",),
            ("DecisionResult[T]",),
            810,
            90,
            230,
            110,
        ),
        Box(
            "effect",
            "Attempted effect",
            "Commit accepted change",
            ("transaction or infrastructure stop",),
            ("SQLiteWorkStore.write",),
            1110,
            90,
            260,
            110,
        ),
        Box(
            "rejection",
            "Expected rejection",
            "Preserve decision facts",
            ("observations · mismatches", "effect: unchanged · retry", "bounded alternatives"),
            ("DecisionFailure · FailureDetails",),
            520,
            360,
            360,
            180,
            "muted",
        ),
        Box(
            "effect-outcome",
            "Effect outcome",
            "Preserve durable truth",
            ("committed claim on success", "or changed surfaces + retry", "view warning keeps the commit"),
            ("effect + changed_surfaces",),
            1040,
            360,
            330,
            180,
            "muted",
        ),
        Box(
            "guidance",
            "Interface owner",
            "Explain and choose the next move",
            ("what happened · whether state changed", "bounded command or legal action"),
            ("presentation policy stays here",),
            645,
            660,
            510,
            130,
        ),
    ),
    notes=(
        Note(
            "Layers narrow each decision without erasing facts that the next owner needs.",
            700,
            805,
            12,
            "middle",
        ),
    ),
)
