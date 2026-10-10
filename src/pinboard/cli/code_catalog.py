"""State-independent lookup of codes declared by installed result and emitter owners."""

from types import UnionType
from typing import Annotated, Literal, TypeAliasType, get_args, get_origin

import msgspec

from pinboard import __version__, diagnostic_codes
from pinboard.adapters.dispatch_operations import DispatchErrorCode
from pinboard.adapters.files.errors import ArtifactErrorCode, FileIOErrorCode, RootErrorCode
from pinboard.adapters.sqlite.errors import StorageErrorCode
from pinboard.application import query_models, service, work_brief_models
from pinboard.application.brief_source_models import BriefSourceErrorCode
from pinboard.application.dispatch_models import DispatchRejectionCode
from pinboard.cli import cli_commands
from pinboard.cli.cli_output import write_json
from pinboard.cli.errors import CliErrorCode, CommandFailure, CommandResult
from pinboard.cli.work_state_models import ValidationDiagnosticCode
from pinboard.domain import decision_models
from pinboard.domain.errors import (
    CodeMeanings,
    DecisionFailureCode,
    DescribedCode,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)

_FAILURE_ENUMS: tuple[type[DescribedCode], ...] = (
    DecisionFailureCode,
    BriefSourceErrorCode,
    work_brief_models.WorkBriefErrorCode,
    DispatchRejectionCode,
    query_models.IntegrationUnavailableReason,
    query_models.ParallelReasonCode,
    ArtifactErrorCode,
    FileIOErrorCode,
    RootErrorCode,
    StorageErrorCode,
    DispatchErrorCode,
    ValidationDiagnosticCode,
    CliErrorCode,
    diagnostic_codes.ProducerOnlyCode,
)

_FAILURE_RECOVERY = (
    "Use the returned resource, recovery, effect, retry, and exact next step. "
    "If an effect committed or is uncertain, inspect current state before any mutation; do not replay from this catalog."
)


class CodeEntry(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-code-entry/v1"]
    code: str
    kind: Literal["failure", "reason", "receipt-event", "trace-event"]
    owners: tuple[str, ...]
    meaning: str
    recovery: str


class CodeCatalogIndex(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-code-catalog/v1"]
    tool_version: str
    codes: tuple[CodeEntry, ...]


def _literal_values(annotation: type | TypeAliasType | UnionType) -> tuple[str, ...]:
    if isinstance(annotation, TypeAliasType):
        return _literal_values(annotation.__value__)
    if get_origin(annotation) is Annotated:
        return _literal_values(get_args(annotation)[0])
    if get_origin(annotation) is Literal:
        return tuple(value for value in get_args(annotation) if isinstance(value, str))
    if get_origin(annotation) is UnionType:
        return tuple(value for arg in get_args(annotation) for value in _literal_values(arg))
    return ()


def _annotated_meanings(annotation: type | TypeAliasType | UnionType) -> dict[str, str]:
    if isinstance(annotation, TypeAliasType):
        return _annotated_meanings(annotation.__value__)
    if get_origin(annotation) is Annotated:
        value, *metadata = get_args(annotation)
        codes = _literal_values(value)
        return {
            codes[index]: meaning
            for item in metadata
            if isinstance(item, CodeMeanings)
            for index, meaning in item.entries
            if index < len(codes)
        }
    if get_origin(annotation) is UnionType:
        return {code: meaning for arm in get_args(annotation) for code, meaning in _annotated_meanings(arm).items()}
    return {}


def _mcp_code_facts() -> tuple[set[str], dict[str, str]]:
    codes: set[str] = set()
    meanings: dict[str, str] = {}
    for annotation in vars(diagnostic_codes).values():
        if isinstance(annotation, TypeAliasType):
            codes.update(_literal_values(annotation))
            meanings.update(_annotated_meanings(annotation))
    return codes, meanings


def _receipt_meaning(code: str) -> str:
    if code == decision_models.ActionKind.INSPECT.value:
        return service.PROPOSAL_INTAKE_RECEIPT_MEANING
    if code == service.LIVE_ORDER_RECEIPT_ACTION_ID:
        return service.LIVE_ORDER_RECEIPT_MEANING
    return decision_models.action_semantics(decision_models.ActionKind(code)).practical_result


def installed_code_catalog() -> CodeCatalogIndex:
    owners: dict[str, set[str]] = {}
    kinds: dict[str, Literal["failure", "reason", "receipt-event", "trace-event"]] = {}
    meanings: dict[str, str] = {}

    def add(
        code: str,
        owner: str,
        kind: Literal["failure", "reason", "receipt-event", "trace-event"],
        meaning: str | None,
    ) -> None:
        owners.setdefault(code, set()).add(owner)
        kinds.setdefault(code, kind)
        if meaning is not None:
            previous = meanings.setdefault(code, meaning)
            if previous != meaning:
                raise ValueError(f"Conflicting catalog meanings for {code}")

    for code_type in _FAILURE_ENUMS:
        kind: Literal["failure", "reason", "receipt-event", "trace-event"] = (
            "reason"
            if code_type in (query_models.IntegrationUnavailableReason, query_models.ParallelReasonCode)
            else "failure"
        )
        for member in code_type:
            if not isinstance(member.value, str):
                raise TypeError(f"Catalog code is not a string: {code_type.__name__}.{member.name}")
            add(member.value, code_type.__name__, kind, member.meaning)
    mcp_codes, mcp_meanings = _mcp_code_facts()
    for code in mcp_codes:
        add(code, "MCP result contract", "failure", mcp_meanings.get(code))
    for event in diagnostic_codes.TraceEvent:
        add(event.value, "MCP trace emitter", "trace-event", event.meaning)
    for action in decision_models.ActionKind:
        if action not in (
            decision_models.ActionKind.DISPATCH,
            decision_models.ActionKind.REPORT_BLOCKER,
        ):
            add(action.value, "transition receipt", "receipt-event", _receipt_meaning(action.value))
    add(
        service.LIVE_ORDER_RECEIPT_ACTION_ID,
        "live-order receipt emitter",
        "receipt-event",
        _receipt_meaning(service.LIVE_ORDER_RECEIPT_ACTION_ID),
    )

    codes = tuple(
        CodeEntry(
            "pinboard-code-entry/v1",
            code,
            kinds[code],
            tuple(sorted(owners[code])),
            meanings.get(code, f"Pinboard returned {code}; inspect the exact result for its operation context."),
            (
                "Use the correlated request and result to diagnose the event; follow the result's exact effect and retry facts."
                if kinds[code] == "trace-event"
                else "Read the receipt's recorded revision and outcome before taking another action."
                if kinds[code] == "receipt-event"
                else "Use the returned reason and current item facts to choose the next read."
                if kinds[code] == "reason"
                else _FAILURE_RECOVERY
            ),
        )
        for code in sorted(owners)
    )
    return CodeCatalogIndex("pinboard-code-catalog/v1", __version__, codes)


def select_code_catalog(command: cli_commands.CodeCatalogCommand) -> CommandResult[CodeCatalogIndex | CodeEntry]:
    catalog = installed_code_catalog()
    if command.code is None:
        return catalog
    if selected := next((entry for entry in catalog.codes if entry.code == command.code), None):
        return selected
    return CommandFailure(
        DecisionFailureCode.TRANSITION_INPUT_INVALID,
        f"Unknown installed code: {command.code}",
        FailureDetails(
            observed=(FailureFact("code", command.code),),
            mismatches=(FailureMismatch("code", "code returned by pinboard code-catalog --json", command.code),),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def show_code_catalog(command: cli_commands.CodeCatalogCommand) -> CommandResult[int]:
    selected = select_code_catalog(command)
    if isinstance(selected, CommandFailure):
        return selected
    write_json(selected)
    return 0
