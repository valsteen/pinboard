from dataclasses import fields

from pinboard.application import service
from pinboard.application.mutation_models import CommittedEffect, PreparationStart
from pinboard.domain import authority_models
from pinboard.domain.authority_decisions import decide_preparation_authority
from pinboard.domain.errors import DecisionFailure, FailureDetails
from pinboard.interfaces import cli_commands, preparation_authority
from pinboard.interfaces.cli_output import RejectedOperationView, write_operation_rejection
from pinboard.interfaces.errors import storage_failure_details

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

DECISION_FAILURE_FIELDS = frozenset({"code", "message", "details"})
PREPARATION_START_FIELDS = frozenset({"effect", "authority"})
COMMITTED_EFFECT_FIELDS = frozenset({"receipt", "item_ids", "attempt_ids", "continuation_attempt_id"})

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
    "PreparationAuthorityDecision": authority_models.PreparationAuthorityDecision.__name__,
    "PreparationStart": PreparationStart.__name__,
    "CommittedEffect": CommittedEffect.__name__,
    "DecisionFailure": DecisionFailure.__name__,
    "FailureDetails": FailureDetails.__name__,
    "RejectedOperationView": RejectedOperationView.__name__,
    "write_operation_rejection": write_operation_rejection.__name__,
    "storage_failure_details": storage_failure_details.__name__,
}


def validate() -> None:
    renamed = tuple(name for name, actual_name in SOURCE_SYMBOL_NAMES.items() if actual_name != name.split(".")[-1])
    if renamed:
        raise ValueError(f"outcome visual references renamed source symbols: {', '.join(renamed)}")
    failure_fields = frozenset(field.name for field in fields(FailureDetails))
    decision_failure_fields = frozenset(field.name for field in fields(DecisionFailure))
    preparation_start_fields = frozenset(field.name for field in fields(PreparationStart))
    committed_effect_fields = frozenset(field.name for field in fields(CommittedEffect))
    rejected_fields = frozenset(RejectedOperationView.__struct_fields__)
    if (
        failure_fields != FAILURE_DETAIL_FIELDS
        or decision_failure_fields != DECISION_FAILURE_FIELDS
        or preparation_start_fields != PREPARATION_START_FIELDS
        or committed_effect_fields != COMMITTED_EFFECT_FIELDS
        or rejected_fields != REJECTED_OPERATION_FIELDS
    ):
        drifted = sorted(
            failure_fields ^ FAILURE_DETAIL_FIELDS
            | decision_failure_fields ^ DECISION_FAILURE_FIELDS
            | preparation_start_fields ^ PREPARATION_START_FIELDS
            | committed_effect_fields ^ COMMITTED_EFFECT_FIELDS
            | rejected_fields ^ REJECTED_OPERATION_FIELDS
        )
        raise ValueError(f"outcome visual and structured failure contract differ: {', '.join(drifted)}")


DIAGRAM = Diagram(
    slug="outcomes",
    title="Precise outcomes cross intact layer boundaries",
    description=(
        "A preparation start crosses stable interface, application, domain, and adapter boundaries. Accepted "
        "decisions, expected rejections, attempted effects, infrastructure failures, and later view warnings keep "
        "their distinct facts. The interface alone turns those facts into presentation and bounded follow-ups."
    ),
    width=1400,
    height=820,
    sections=(
        Section("Stable ownership boundaries", "each layer contributes facts without taking over presentation", 28, 42),
        Section("Distinct typed outcomes", "accepted decision, expected rejection, and attempted effect", 28, 330),
        Section("One presentation owner", "structured facts become truthful guidance and bounded actions", 28, 630),
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
            ((700, 540), (700, 590), (790, 590), (790, 650)),
            "rejection",
            "guidance",
            "explain",
            (745, 577),
        ),
        Connector(
            ((1205, 540), (1205, 610), (1010, 610), (1010, 650)),
            "effect-outcome",
            "guidance",
            "reconcile",
            (1108, 597),
        ),
    ),
    boxes=(
        Box(
            "request",
            "INTERFACE INPUT",
            "Decode an exact request",
            ("item · task · host · TTL",),
            ("PreparationStartCommand",),
            160,
            90,
            250,
            110,
        ),
        Box(
            "state",
            "APPLICATION",
            "Select current state",
            ("definition · claim · dependencies",),
            ("start_preparation",),
            470,
            90,
            270,
            110,
        ),
        Box(
            "decision",
            "DOMAIN",
            "Decide legality",
            ("accepted authority or rejection",),
            ("decide_preparation_authority",),
            810,
            90,
            230,
            110,
        ),
        Box(
            "effect",
            "ADAPTER EFFECT",
            "Attempt the accepted mutation",
            ("commit, stale guard, or fault",),
            ("WorkTransaction.commit",),
            1110,
            90,
            260,
            110,
        ),
        Box(
            "rejection",
            "DOMAIN OUTCOME",
            "Return an expected rejection",
            ("code · message", "optional FailureDetails", "this path: details = None"),
            ("DecisionFailure",),
            520,
            360,
            360,
            180,
            "muted",
        ),
        Box(
            "effect-outcome",
            "ATTEMPTED EFFECT",
            "Report the actual outcome",
            (
                "commit → CommittedEffect",
                "stale guard → DecisionFailure",
                "fault → infrastructure exception",
            ),
            ("view warning remains separate",),
            1040,
            360,
            330,
            180,
            "muted",
        ),
        Box(
            "guidance",
            "INTERFACE PRESENTATION",
            "Translate facts into guidance",
            (
                "facts → observations · mismatches",
                "effect → state · surfaces · retry",
                "alternatives → bounded actions",
            ),
            ("RejectedOperationView · lease output",),
            645,
            650,
            510,
            140,
        ),
    ),
    notes=(
        Note(
            "No lower layer chooses wording or invents a follow-up.",
            700,
            805,
            12,
            "middle",
        ),
    ),
)
