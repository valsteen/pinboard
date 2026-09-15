"""Strict request and correlated result contracts for the MCP transport."""

from typing import Annotated, Literal

import msgspec

from pinboard.application import proposal_models, query_models, work_brief_models

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonSchemaValue = JsonScalar | list[JsonSchemaValue] | dict[str, JsonSchemaValue]
type NonEmptyText = Annotated[str, msgspec.Meta(min_length=1)]
type RootPath = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\x00]+\z")]
type PositiveInt = Annotated[int, msgspec.Meta(ge=1)]
type Sha256 = Annotated[str, msgspec.Meta(pattern=r"\A[0-9a-f]{64}\z")]
type PathComponent = Annotated[
    str,
    msgspec.Meta(min_length=1, pattern=r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z"),
]
type RuntimeIdentity = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z",
    ),
]
type Empty = tuple[()]
type LedgerSurface = tuple[Literal["ledger"]]
type ArtifactSurface = tuple[Literal["immutable-artifact"]]
type ReferenceSurfaces = tuple[Literal["accepted-artifact-reference"], Literal["ledger"]]
type PublicationSurfaces = tuple[
    Literal["immutable-artifact"],
    Literal["accepted-artifact-reference"],
    Literal["ledger"],
]


def _require_state_changed(actual: bool, expected: bool) -> None:
    if actual is not expected:
        raise ValueError(f"state_changed must be {str(expected).lower()} for this result.")


class ItemStatusRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    item_id: PathComponent


class ProposalCreateRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    proposal: proposal_models.Proposal
    actor_task_id: RuntimeIdentity
    actor_host_id: RuntimeIdentity


class BriefPublishRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    project_root: RootPath
    work_root: RootPath
    brief: work_brief_models.WorkBrief


class FailureObservation(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: NonEmptyText
    value: JsonScalar


class FailureMismatch(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    field: NonEmptyText
    expected: JsonScalar
    observed: JsonScalar


class ItemStatusInvalid(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v1"]
    status: Literal["rejected"]
    code: Literal["ITEM_STATUS_INVALID"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ItemStatusUnavailable(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v1"]
    status: Literal["rejected"]
    code: Literal["ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ItemStatusInconsistent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-item-status-result/v1"]
    status: Literal["rejected"]
    code: Literal["ITEM_STATUS_INCONSISTENT"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: Annotated[tuple[FailureMismatch, ...], msgspec.Meta(min_length=1)]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ExecutorBusyResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-execution-result/v1"]
    status: Literal["busy"]
    code: Literal["EXECUTOR_BUSY"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class WarningResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    message: NonEmptyText
    recovery: NonEmptyText


class ProposalCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["committed"]
    proposal_id: proposal_models.ProposalIdentity
    position: PositiveInt
    item_state: Literal["intake"]
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class ProposalCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["committed-with-warning"]
    proposal_id: proposal_models.ProposalIdentity
    position: PositiveInt
    item_state: Literal["intake"]
    committed_revision: PositiveInt
    history_id: PositiveInt
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: LedgerSurface
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class ProposalRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["rejected"]
    code: Literal[
        "PROPOSAL_INVALID",
        "ITEM_ALREADY_EXISTS",
        "ITEM_NOT_FOUND",
        "ACTION_NOT_AVAILABLE",
        "ITEM_DEFINITION_INVALID",
    ]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty
    recovery: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ProposalDuplicate(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-proposal-result/v1"]
    status: Literal["rejected"]
    code: Literal["PROPOSAL_ALREADY_EXISTS"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["do-not-retry"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty
    recovery: NonEmptyText

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class ArtifactReferenceResult(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    artifact_ref_id: PositiveInt
    kind: Literal["brief"]
    key: work_brief_models.KebabId
    revision: PositiveInt
    selector: NonEmptyText
    sha256: Sha256
    size_bytes: PositiveInt
    accepted_revision: PositiveInt


class BriefCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: PublicationSurfaces
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefReferenceCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ReferenceSurfaces
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefArtifactCommitted(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: PublicationSurfaces
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefReferenceCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ReferenceSurfaces
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefArtifactCommittedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["committed-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefUnchanged(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["unchanged"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    continuation: NonEmptyText
    warning: None

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefUnchangedWithWarning(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["unchanged-with-warning"]
    reference: ArtifactReferenceResult
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["retry-same-input"]
    changed_surfaces: Empty
    continuation: NonEmptyText
    warning: WarningResult

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefRejected(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["rejected"]
    code: Literal["WORK_BRIEF_INVALID", "ACTION_NOT_AVAILABLE"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input"]
    changed_surfaces: Empty
    observed: Empty
    mismatches: Empty

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, False)


class BriefPublishedRejection(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["rejected"]
    code: Literal["ACTION_NOT_AVAILABLE"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    observed: Annotated[tuple[FailureObservation, ...], msgspec.Meta(min_length=1)]
    mismatches: tuple[FailureMismatch, ...]

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


class BriefPublicationAcceptanceFailure(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-mcp-brief-publication-result/v1"]
    status: Literal["failed-after-publication"]
    code: Literal["ARTIFACT_ACCEPTANCE_FAILED"]
    message: NonEmptyText
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: ArtifactSurface
    observed: Empty
    mismatches: Empty
    published_selector: NonEmptyText
    recovery: NonEmptyText

    def __post_init__(self) -> None:
        _require_state_changed(self.state_changed, True)


ITEM_STATUS_RESULT_TYPES = (
    query_models.ItemStatus,
    ItemStatusInvalid,
    ItemStatusUnavailable,
    ItemStatusInconsistent,
    ExecutorBusyResult,
)
PROPOSAL_RESULT_TYPES = (
    ProposalCommitted,
    ProposalCommittedWithWarning,
    ProposalRejected,
    ProposalDuplicate,
    ExecutorBusyResult,
)
BRIEF_PUBLICATION_RESULT_TYPES = (
    BriefCommitted,
    BriefReferenceCommitted,
    BriefArtifactCommitted,
    BriefCommittedWithWarning,
    BriefReferenceCommittedWithWarning,
    BriefArtifactCommittedWithWarning,
    BriefUnchanged,
    BriefUnchangedWithWarning,
    BriefRejected,
    BriefPublishedRejection,
    BriefPublicationAcceptanceFailure,
    ExecutorBusyResult,
)
type RequestBoundary = type[ItemStatusRequest] | type[ProposalCreateRequest] | type[BriefPublishRequest]
type ResultBoundary = (
    type[query_models.ItemStatus]
    | type[ItemStatusInvalid]
    | type[ItemStatusUnavailable]
    | type[ItemStatusInconsistent]
    | type[ExecutorBusyResult]
    | type[ProposalCommitted]
    | type[ProposalCommittedWithWarning]
    | type[ProposalRejected]
    | type[ProposalDuplicate]
    | type[BriefCommitted]
    | type[BriefReferenceCommitted]
    | type[BriefArtifactCommitted]
    | type[BriefCommittedWithWarning]
    | type[BriefReferenceCommittedWithWarning]
    | type[BriefArtifactCommittedWithWarning]
    | type[BriefUnchanged]
    | type[BriefUnchangedWithWarning]
    | type[BriefRejected]
    | type[BriefPublishedRejection]
    | type[BriefPublicationAcceptanceFailure]
)


def schema_for(boundary_type: RequestBoundary) -> dict[str, JsonSchemaValue]:
    """Return the exact msgspec schema for one MCP request."""
    schema: dict[str, JsonSchemaValue] = msgspec.json.schema(boundary_type)
    reference = schema.pop("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        raise ValueError("The MCP request schema must have one named root record.")
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise TypeError("The MCP request schema must contain named definitions.")
    definition = definitions.get(reference.removeprefix("#/$defs/"))
    if not isinstance(definition, dict):
        raise TypeError("The MCP request schema root must be an object definition.")
    return {**definition, **schema}


def _apply_boolean_constants(definitions: dict[str, JsonSchemaValue]) -> None:
    changed_results = {
        "ProposalCommitted",
        "ProposalCommittedWithWarning",
        "BriefCommitted",
        "BriefReferenceCommitted",
        "BriefArtifactCommitted",
        "BriefCommittedWithWarning",
        "BriefReferenceCommittedWithWarning",
        "BriefArtifactCommittedWithWarning",
        "BriefPublishedRejection",
        "BriefPublicationAcceptanceFailure",
    }
    unchanged_results = {
        "ItemStatusInvalid",
        "ItemStatusUnavailable",
        "ItemStatusInconsistent",
        "ExecutorBusyResult",
        "ProposalRejected",
        "ProposalDuplicate",
        "BriefUnchanged",
        "BriefUnchangedWithWarning",
        "BriefRejected",
    }
    for name, definition in definitions.items():
        if name not in changed_results | unchanged_results:
            continue
        if not isinstance(definition, dict):
            raise TypeError(f"MCP result definition '{name}' must be an object.")
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            raise TypeError(f"MCP result definition '{name}' must declare properties.")
        properties["state_changed"] = {"type": "boolean", "const": name in changed_results}


def union_schema_for(boundary_types: tuple[ResultBoundary, ...]) -> dict[str, JsonSchemaValue]:
    """Return one closed MCP result schema from separate correlated records."""
    components = msgspec.json.schema_components(boundary_types)
    schemas: tuple[dict[str, JsonSchemaValue], ...] = components[0]
    definitions: dict[str, JsonSchemaValue] = components[1]
    _apply_boolean_constants(definitions)
    return {"type": "object", "anyOf": list[JsonSchemaValue](schemas), "$defs": definitions}


def validate_result(tool_name: str, content: dict[str, JsonValue]) -> dict[str, JsonValue]:  # noqa: C901, PLR0912
    """Validate one emitted result against the exact alternative it claims."""
    schema = content.get("schema")
    status = content.get("status")
    code = content.get("code")
    surfaces = content.get("changed_surfaces")
    if schema == "pinboard-item-status/v1" and tool_name == "pinboard_item_status":
        msgspec.convert(content, type=query_models.ItemStatus, strict=True)
    elif schema == "pinboard-mcp-execution-result/v1":
        msgspec.convert(content, type=ExecutorBusyResult, strict=True)
    elif tool_name == "pinboard_item_status" and code == "ITEM_STATUS_INVALID":
        msgspec.convert(content, type=ItemStatusInvalid, strict=True)
    elif tool_name == "pinboard_item_status" and code in {"ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"}:
        msgspec.convert(content, type=ItemStatusUnavailable, strict=True)
    elif tool_name == "pinboard_item_status":
        msgspec.convert(content, type=ItemStatusInconsistent, strict=True)
    elif tool_name == "pinboard_proposal_create" and status == "committed":
        msgspec.convert(content, type=ProposalCommitted, strict=True)
    elif tool_name == "pinboard_proposal_create" and status == "committed-with-warning":
        msgspec.convert(content, type=ProposalCommittedWithWarning, strict=True)
    elif tool_name == "pinboard_proposal_create" and code == "PROPOSAL_ALREADY_EXISTS":
        msgspec.convert(content, type=ProposalDuplicate, strict=True)
    elif tool_name == "pinboard_proposal_create":
        msgspec.convert(content, type=ProposalRejected, strict=True)
    elif status == "failed-after-publication":
        msgspec.convert(content, type=BriefPublicationAcceptanceFailure, strict=True)
    elif status == "rejected" and surfaces == ["immutable-artifact"]:
        msgspec.convert(content, type=BriefPublishedRejection, strict=True)
    elif status == "rejected":
        msgspec.convert(content, type=BriefRejected, strict=True)
    elif status == "unchanged":
        msgspec.convert(content, type=BriefUnchanged, strict=True)
    elif status == "unchanged-with-warning":
        msgspec.convert(content, type=BriefUnchangedWithWarning, strict=True)
    elif status == "committed" and surfaces == ["immutable-artifact"]:
        msgspec.convert(content, type=BriefArtifactCommitted, strict=True)
    elif status == "committed" and surfaces == ["accepted-artifact-reference", "ledger"]:
        msgspec.convert(content, type=BriefReferenceCommitted, strict=True)
    elif status == "committed":
        msgspec.convert(content, type=BriefCommitted, strict=True)
    elif status == "committed-with-warning" and surfaces == ["immutable-artifact"]:
        msgspec.convert(content, type=BriefArtifactCommittedWithWarning, strict=True)
    elif status == "committed-with-warning" and surfaces == ["accepted-artifact-reference", "ledger"]:
        msgspec.convert(content, type=BriefReferenceCommittedWithWarning, strict=True)
    else:
        msgspec.convert(content, type=BriefCommittedWithWarning, strict=True)
    return content
