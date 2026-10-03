"""Generate negotiated MCP schemas and validate emitted transport results."""

from typing import get_args

import msgspec

from pinboard.adapters import dispatch_operations
from pinboard.application import action_models, brief_source_models, dispatch_models, query_models, work_brief_contract
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.mcp.contracts import (
    _ACTIVE_CONTINUATION_ACTION_KINDS,
    _BLOCKED_CONTINUATION_ACTION_KINDS,
    _PAUSED_CONTINUATION_ACTION_KINDS,
    _REVIEW_CONTINUATION_ACTION_KINDS,
    ActionsEnvelope,
    ActionsInvalid,
    ActionsSuccess,
    ActionUnavailable,
    ArtifactBytesInvalid,
    ArtifactReferenceMismatch,
    ArtifactVerificationInvalid,
    ArtifactVerified,
    AttemptActionUnavailable,
    AttemptAuthorityCommitted,
    AttemptAuthorityEnvelope,
    AttemptAuthorityRejected,
    AttemptAuthorityStatusAbsent,
    AttemptAuthorityStatusPresent,
    AttemptBriefInvalid,
    AttemptInspectInvalid,
    AttemptLeaseRequired,
    AttemptNotFound,
    AttemptReceiptDamaged,
    BranchOwnerNotFound,
    BriefArchitectureImpactRejected,
    BriefArtifactCommitted,
    BriefArtifactCommittedWithWarning,
    BriefCommitted,
    BriefCommittedWithWarning,
    BriefContractRejected,
    BriefPublicationInfrastructureFailure,
    BriefPublicationInfrastructureUnchangedFailure,
    BriefPublishedRejection,
    BriefReferenceCommitted,
    BriefReferenceCommittedWithWarning,
    BriefRejected,
    BriefReviewCommitted,
    BriefReviewInfrastructureFailure,
    BriefReviewInfrastructureUnchangedFailure,
    BriefReviewNeedsCorrection,
    BriefReviewNoEvidence,
    BriefReviewPublishedRejection,
    BriefReviewRejected,
    BriefReviewUnchanged,
    BriefSourceBatchResult,
    BriefSourcePlanOutputResult,
    BriefSourcesPublishedFailure,
    BriefSourcesRejected,
    BriefUnchanged,
    BriefUnchangedWithWarning,
    CandidateObservationRejected,
    CandidateObserved,
    CandidateRestoreFailed,
    CandidateRestoreInvalid,
    CandidateRestoreReady,
    CandidateRestoreRejected,
    CandidateReviewRecorded,
    CompletionActionsSuccess,
    CorrectionContextReady,
    CorrectionContextRejected,
    DispatchFailedAfterPublication,
    DispatchInfrastructureFailure,
    DispatchInvalid,
    DispatchReady,
    DispatchRejected,
    ExecutorBusyResult,
    IntegrationStatusRejected,
    ItemDefinitionRejected,
    ItemStatusInconsistent,
    ItemStatusInvalid,
    ItemStatusReceiptDamaged,
    ItemStatusUnavailable,
    JsonSchemaValue,
    JsonValue,
    LegacyBriefReviewNeedsCorrection,
    LegacyBriefReviewNoEvidence,
    NonterminalAttemptInspectionSuccess,
    OrderCommitted,
    OrderRejected,
    OverviewRejected,
    ParallelPreviewRejected,
    ParallelPreviewSuccess,
    PreparationAuthorityCommitted,
    PreparationAuthorityEnvelope,
    PreparationAuthorityRejected,
    PreparationAuthorityStatusAbsent,
    PreparationAuthorityStatusPresent,
    ProposalCommitted,
    ProposalCommittedWithWarning,
    ProposalDuplicate,
    ProposalRejected,
    PrReviewRejected,
    PrReviewSuccess,
    RequestBoundary,
    ResultBoundary,
    RetainedV3BriefReviewNeedsCorrection,
    RetainedV3BriefReviewNoEvidence,
    ReviewJobCandidateRequired,
    ReviewJobFailedAfterPublication,
    ReviewJobInfrastructureFailure,
    ReviewJobInvalid,
    ReviewJobReady,
    ReviewJobRejected,
    TerminalAttemptInspectionSuccess,
    TracePreflightResult,
    TransitionCommitted,
    TransitionFailedAfterPublication,
    TransitionRejected,
    TransitionRequest,
    _committed_transition_surfaces,
    _fixed_state_changed,
)


def _portable_pattern(pattern: str) -> tuple[str, str | None]:
    if not pattern.startswith(r"\A") or not pattern.endswith(r"\z"):
        raise ValueError(f"Unsupported canonical MCP pattern: {pattern}")
    component = r"(?:[^./\r\n\x00]|\.[^./\r\n\x00]|\.\.[^/\r\n\x00])[^/\r\n\x00]*"
    trimmed_component = (
        r"(?:[^.\s/\r\n\x00](?:[^/\r\n\x00]*[^\s/\r\n\x00])?"
        r"|\.(?:[^.\s/\r\n\x00]|[^./\r\n\x00][^/\r\n\x00]*[^\s/\r\n\x00]"
        r"|\.[^/\r\n\x00]*[^\s/\r\n\x00]))"
    )
    special = {
        r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z": f"^{component}$",
        r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z": f"^{trimmed_component}$",
    }
    python_space = r"\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
    projected = special.get(pattern, f"^{pattern[2:-2]}$")
    projected = projected.replace(r"\S", f"[^{python_space}]").replace(r"\s", python_space)
    if any(marker in projected for marker in (r"\A", r"\z", "(?=", "(?!", "(?<=", "(?<!")):
        raise ValueError(f"Unsupported canonical MCP pattern: {pattern}")
    if pattern == r"\A[^\x00]+\z":
        return projected, None
    if pattern == r"\A[^\n]+\z":
        return projected, r"\n$"
    if pattern == r"\A(?!\.{1,2}\z)[^/\r\n\x00]+\z":
        return projected, r"[\r\n]$"
    return projected, r"[\r\n\u2028\u2029]$"


def project_regex_patterns(schema: dict[str, JsonSchemaValue]) -> None:
    """Expose canonical field formats with ECMAScript-compatible patterns."""

    seen: set[int] = set()

    def visit(value: JsonSchemaValue) -> None:
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                return
            seen.add(id(value))
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key not in {"const", "enum", "default", "examples"}:
                    visit(child)
            pattern = value.get("pattern")
            if isinstance(pattern, str):
                projected, terminator = _portable_pattern(pattern)
                value["pattern"] = projected
                if terminator is not None:
                    # ECMAScript $ also matches just before a final line terminator.
                    if "allOf" in value:
                        raise ValueError(f"MCP pattern already has composed constraints: {pattern}")
                    value["allOf"] = [{"not": {"pattern": terminator}}]
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)


def schema_for(boundary_type: RequestBoundary) -> dict[str, JsonSchemaValue]:
    """Return one host-facing MCP request schema from a strict msgspec record."""
    schema: dict[str, JsonSchemaValue] = msgspec.json.schema(
        boundary_type, schema_hook=dispatch_models.dispatch_environment_schema_hook
    )
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


def actions_request_schema() -> dict[str, JsonSchemaValue]:
    """Return the exact role-discriminated action request schema."""

    return schema_for(ActionsEnvelope)


def preparation_authority_request_schema() -> dict[str, JsonSchemaValue]:
    return schema_for(PreparationAuthorityEnvelope)


def attempt_authority_request_schema() -> dict[str, JsonSchemaValue]:
    return schema_for(AttemptAuthorityEnvelope)


def transition_request_schema() -> dict[str, JsonSchemaValue]:
    """Advertise exactly the leaves of the union that the transition decoder returns."""

    schemas, definitions = msgspec.json.schema_components(get_args(TransitionRequest.__value__))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["request"],
        "properties": {"request": {"oneOf": list[JsonSchemaValue](schemas)}},
        "$defs": definitions,
    }


def _apply_result_state_constraints(
    definitions: dict[str, JsonSchemaValue], boundary_types: tuple[ResultBoundary, ...]
) -> None:
    for boundary_type in boundary_types:
        state_changed = _fixed_state_changed(boundary_type)
        if state_changed is None:
            continue
        name = boundary_type.__name__
        definition = definitions.get(name)
        if not isinstance(definition, dict):
            raise TypeError(f"MCP result definition '{name}' must be an object.")
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            raise TypeError(f"MCP result definition '{name}' must declare properties.")
        properties["state_changed"] = {"type": "boolean", "const": state_changed}
    review = definitions.get("BriefReviewCommitted")
    if isinstance(review, dict) and isinstance(properties := review.get("properties"), dict):
        properties["changed_surfaces"] = {
            "enum": [
                ["immutable-artifact"],
                ["accepted-artifact-reference", "ledger"],
                ["immutable-artifact", "accepted-artifact-reference", "ledger"],
            ],
        }
    artifact = definitions.get("ArtifactVerified")
    if isinstance(artifact, dict) and isinstance(properties := artifact.get("properties"), dict):
        properties["verified"] = {"type": "boolean", "const": True}
    order = definitions.get("OrderCommitted")
    if isinstance(order, dict):
        order["oneOf"] = [
            {"properties": {"status": {"const": "committed"}, "warning": {"type": "null"}}},
            {
                "properties": {
                    "status": {"const": "committed-with-warning"},
                    "warning": {"$ref": "#/$defs/WarningResult"},
                }
            },
        ]


def _action_semantics_constraint(kind: decision_models.ActionKind) -> dict[str, JsonSchemaValue]:
    semantics = decision_models.action_semantics(kind)
    roles = [role.value for role in semantics.permitted_roles]
    return {
        "type": "object",
        "properties": {
            "use_case": {"const": semantics.use_case},
            "effect": {"const": semantics.lifecycle_effect.value},
            "permitted_roles": {
                "type": "array",
                "prefixItems": [{"const": role} for role in roles],
                "minItems": len(roles),
                "maxItems": len(roles),
            },
            "subject_kind": {"const": semantics.subject_kind.value},
            "lifecycle_precondition": {"const": semantics.lifecycle_precondition.value},
            "practical_result": {"const": semantics.practical_result},
        },
    }


def _apply_action_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    definition = definitions.get("ActionView")
    if not isinstance(definition, dict):
        return
    authority_shapes: list[JsonSchemaValue] = [
        {
            "properties": {
                "authorization": {"const": "observer"},
                "subject_revision": {"type": "null"},
                "lease_id": {"type": "null"},
                "generation": {"type": "null"},
            }
        },
        {
            "properties": {
                "authorization": {"const": "project"},
                "subject_revision": {"type": "string"},
                "lease_id": {"type": "null"},
                "generation": {"type": "null"},
            }
        },
        {
            "properties": {
                "authorization": {"const": "attempt"},
                "subject_revision": {"type": "string"},
                "lease_id": {"type": "string"},
                "generation": {"type": "integer", "minimum": 1},
            }
        },
        {
            "properties": {
                "authorization": {"const": "preparation"},
                "subject_revision": {"type": "string"},
                "lease_id": {"type": "string"},
                "generation": {"type": "integer", "minimum": 1},
            }
        },
    ]
    role_authorities = {
        decision_models.Role.OBSERVER: "observer",
        decision_models.Role.PROJECT: "project",
        decision_models.Role.WORKER: "attempt",
        decision_models.Role.PREPARER: "preparation",
    }
    correlations: list[JsonSchemaValue] = [{"oneOf": authority_shapes}]
    for kind in decision_models.ActionKind:
        semantics = decision_models.action_semantics(kind)
        semantic_constraint = _action_semantics_constraint(kind)
        payload_schema = action_models.action_payload_schema(kind)
        correlations.append(
            {
                "if": {
                    "properties": {
                        "action_id": {"properties": {"kind": {"const": kind.value}}},
                    }
                },
                "then": {
                    "properties": {
                        "authorization": {"enum": [role_authorities[role] for role in semantics.permitted_roles]},
                        "semantics": semantic_constraint,
                        "input_contract": {
                            "type": "object",
                            "properties": {
                                "action_kind": {"const": kind.value},
                                "semantics": semantic_constraint,
                                "payload_schema": {"const": payload_schema},
                            },
                        },
                    }
                },
            }
        )
    definition["allOf"] = correlations


def _apply_transition_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    definition = definitions.get("TransitionCommitted")
    if not isinstance(definition, dict):
        return
    definition["oneOf"] = [
        {
            "properties": {
                "action_id": {"properties": {"kind": {"const": kind.value}}},
                "changed_surfaces": {
                    "type": "array",
                    "prefixItems": [{"const": surface} for surface in surfaces],
                    "minItems": len(surfaces),
                    "maxItems": len(surfaces),
                },
            }
        }
        for kind in decision_models.ActionKind
        for surfaces in _committed_transition_surfaces(kind)
    ]


def _relative_action(kind: decision_models.ActionKind) -> dict[str, JsonSchemaValue]:
    target = decision_models.action_semantics(kind).subject_kind.value
    if target not in {"attempt", "item"}:
        raise ValueError(f"Action kind '{kind.value}' cannot appear in an attempt continuation.")
    return {
        "allOf": [
            {"$ref": "#/$defs/RelativeActionIdentity"},
            {
                "properties": {
                    "target": {"const": target},
                    "action_kind": {"const": kind.value},
                }
            },
        ]
    }


def _apply_relative_action_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    definition = definitions.get("RelativeActionIdentity")
    if not isinstance(definition, dict):
        return
    definition["oneOf"] = [
        {
            "properties": {
                "target": {"const": semantics.subject_kind.value},
                "action_kind": {"const": kind.value},
            }
        }
        for kind in decision_models.ActionKind
        if (semantics := decision_models.action_semantics(kind)).subject_kind
        in {decision_models.ActionSubjectKind.ATTEMPT, decision_models.ActionSubjectKind.ITEM}
    ]


def _action_continuation(kind: decision_models.ActionKind) -> dict[str, JsonSchemaValue]:
    return {
        "allOf": [
            {"$ref": "#/$defs/ContinuationAction"},
            {
                "properties": {
                    "action": _relative_action(kind),
                }
            },
        ]
    }


def _forbidden_routes_constraint() -> dict[str, JsonSchemaValue]:
    values = ("create-user-task", "wake-user-task", "return-ownership-to-parent")
    return {
        "type": "array",
        "prefixItems": [{"const": value} for value in values],
        "minItems": len(values),
        "maxItems": len(values),
    }


def _apply_attempt_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    terminal = definitions.get("TerminalAttemptContinuation")
    if isinstance(terminal, dict) and isinstance(terminal.get("properties"), dict):
        terminal["properties"]["state"] = {"const": "done"}
        terminal["properties"]["terminal"] = {"type": "boolean", "const": True}
        terminal["properties"]["user_input_required"] = {"type": "boolean", "const": False}
        terminal["properties"]["forbidden_routes"] = _forbidden_routes_constraint()
    continuation_constraints: tuple[
        tuple[
            str,
            tuple[dict[str, JsonSchemaValue], ...],
            tuple[decision_models.ActionKind, ...],
        ],
        ...,
    ] = (
        (
            "ActiveAttemptContinuation",
            (
                _action_continuation(decision_models.ActionKind.CONTINUE),
                _action_continuation(decision_models.ActionKind.REBIND_ATTEMPT),
                _action_continuation(decision_models.ActionKind.PAUSE),
                _action_continuation(decision_models.ActionKind.COMPLETE),
            ),
            _ACTIVE_CONTINUATION_ACTION_KINDS,
        ),
        (
            "ReviewAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationReview"},
                {"$ref": "#/$defs/ContinuationReconcileRepository"},
                {"$ref": "#/$defs/ContinuationRefreshTarget"},
                {"$ref": "#/$defs/ContinuationPermissionRecovery"},
                {"$ref": "#/$defs/ContinuationRepositoryDisposition"},
                {"$ref": "#/$defs/ContinuationCommitThenReinspect"},
                {"$ref": "#/$defs/ContinuationRepositoryCleanup"},
                _action_continuation(decision_models.ActionKind.COMPLETE),
                _action_continuation(decision_models.ActionKind.RETURN_FOR_CORRECTION),
            ),
            _REVIEW_CONTINUATION_ACTION_KINDS,
        ),
        (
            "PausedAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationDependencies"},
                _action_continuation(decision_models.ActionKind.RESUME),
            ),
            _PAUSED_CONTINUATION_ACTION_KINDS,
        ),
        (
            "BlockedAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationDependencies"},
                _action_continuation(decision_models.ActionKind.RESUME),
            ),
            _BLOCKED_CONTINUATION_ACTION_KINDS,
        ),
    )
    for definition_name, next_operations, legal_action_kinds in continuation_constraints:
        definition = definitions.get(definition_name)
        if not isinstance(definition, dict):
            continue
        properties = definition.get("properties")
        if isinstance(properties, dict):
            properties["terminal"] = {"type": "boolean", "const": False}
            properties["user_input_required"] = {"type": "boolean", "const": False}
            properties["owner_task_id"] = {"type": "string", "minLength": 1}
            properties["legal_actions"] = {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"anyOf": [_relative_action(kind) for kind in legal_action_kinds]},
            }
            properties["forbidden_routes"] = _forbidden_routes_constraint()
            properties["next_operation"] = {"anyOf": list(next_operations)}


def _apply_job_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    publication_sets = (
        (),
        ("immutable-artifact",),
        ("accepted-artifact-reference", "ledger"),
        ("immutable-artifact", "accepted-artifact-reference", "ledger"),
    )
    review_failure = definitions.get("ReviewJobFailedAfterPublication")
    if isinstance(review_failure, dict) and isinstance(review_properties := review_failure.get("properties"), dict):
        review_properties["code"] = {
            "type": "string",
            "enum": [
                "ARTIFACT_ACCEPTANCE_FAILED",
                "ARTIFACT_PUBLICATION_FAILED",
                "REVIEW_JOB_PREPARATION_FAILED",
                "REVIEW_PROMPT_PUBLICATION_FAILED",
                *(code.value for code in DecisionFailureCode),
            ],
        }
    for name in (
        "DispatchReady",
        "ReviewJobReady",
        "CandidateReviewRecorded",
        "DispatchFailedAfterPublication",
        "ReviewJobFailedAfterPublication",
    ):
        definition = definitions.get(name)
        if not isinstance(definition, dict):
            continue
        failed = name.endswith("FailedAfterPublication")
        supported_publications = (
            ((), ("accepted-artifact-reference", "ledger"), publication_sets[-1])
            if name == "CandidateReviewRecorded"
            else publication_sets
        )
        alternatives: list[JsonSchemaValue] = []
        for surfaces in supported_publications:
            if failed and not surfaces:
                continue
            surface_constraint: dict[str, JsonSchemaValue] = {
                "type": "array",
                "minItems": len(surfaces),
                "maxItems": len(surfaces),
            }
            if surfaces:
                surface_constraint["prefixItems"] = [{"const": surface} for surface in surfaces]
            properties: dict[str, JsonSchemaValue] = {
                "changed_surfaces": surface_constraint,
                "state_changed": {"const": bool(surfaces)},
                "effect": {"const": "committed" if surfaces else "unchanged"},
                "retry": {"const": "do-not-retry" if surfaces else "safe-to-repeat"},
            }
            if not failed and name != "CandidateReviewRecorded":
                reference_properties: dict[str, JsonSchemaValue] = {}
                if "immutable-artifact" not in surfaces:
                    reference_properties["artifact_created"] = {"const": False}
                if "ledger" not in surfaces:
                    reference_properties["ledger_changed"] = {"const": False}
                properties["prompt_reference"] = {"properties": reference_properties}
            alternatives.append({"properties": properties})
        definition["oneOf"] = alternatives
    rejected = definitions.get("DispatchRejected")
    if isinstance(rejected, dict):
        rejected_properties = rejected.get("properties")
        if isinstance(rejected_properties, dict):
            rejected_properties["code"] = {
                "type": "string",
                "enum": list[JsonSchemaValue](
                    sorted(
                        {
                            *(code.value for code in dispatch_operations.DispatchErrorCode),
                            *(code.value for code in DecisionFailureCode),
                        }
                    )
                ),
            }


def union_schema_for(boundary_types: tuple[ResultBoundary, ...]) -> dict[str, JsonSchemaValue]:
    """Return one closed MCP result schema from separate correlated records."""
    if ExecutorBusyResult in boundary_types:
        boundary_types = (*boundary_types, TracePreflightResult)
    components = msgspec.json.schema_components(boundary_types)
    schemas: tuple[dict[str, JsonSchemaValue], ...] = components[0]
    definitions: dict[str, JsonSchemaValue] = components[1]
    _apply_result_state_constraints(definitions, boundary_types)
    _apply_action_constraints(definitions)
    _apply_transition_constraints(definitions)
    _apply_relative_action_constraints(definitions)
    _apply_attempt_constraints(definitions)
    _apply_job_constraints(definitions)
    restore = definitions.get("CandidateRestoreReady")
    if isinstance(restore, dict):
        restore["anyOf"] = [
            {
                "properties": {
                    "state_changed": {"const": changed},
                    "effect": {"const": "committed" if changed else "unchanged"},
                    "retry": {"const": "do-not-retry" if changed else "safe-to-repeat"},
                    "changed_surfaces": {"const": list[JsonSchemaValue](("source-checkout",) if changed else ())},
                }
            }
            for changed in (False, True)
        ]
    preflight = definitions.get("TracePreflightResult")
    if isinstance(preflight, dict):
        properties = preflight.get("properties")
        if isinstance(properties, dict):
            properties["target_ran"] = {"type": "boolean", "const": False}
        preflight["anyOf"] = [
            {
                "properties": {
                    "state_changed": {"const": changed},
                    "effect": {"const": effect},
                    "changed_surfaces": {"const": list[JsonSchemaValue](surfaces)},
                }
            }
            for changed, effect, surfaces in (
                (False, "unchanged", ()),
                (True, "committed", ("work-root",)),
                (None, "unconfirmed", ()),
                (None, "unconfirmed", ("work-root",)),
            )
        ]
    correction = definitions.get("CorrectionContextReady")
    if isinstance(correction, dict):
        correction["anyOf"] = [
            {"properties": {"reuse_eligible": {"const": True}, "reuse_blockers": {"maxItems": 0}}},
            {"properties": {"reuse_eligible": {"const": False}, "reuse_blockers": {"minItems": 1}}},
        ]
    return {"type": "object", "anyOf": list[JsonSchemaValue](schemas), "$defs": definitions}


BRIEF_CONTRACT_RESULT_TYPES = (
    work_brief_contract.WorkBriefContract,
    work_brief_contract.WorkBriefStarterContract,
    BriefContractRejected,
    ExecutorBusyResult,
)
BRIEF_SOURCES_RESULT_TYPES = (
    brief_source_models.BriefSourcePlanView,
    BriefSourcePlanOutputResult,
    BriefSourceBatchResult,
    BriefSourcesRejected,
    BriefSourcesPublishedFailure,
    ExecutorBusyResult,
)


def validate_brief_preparation_result(content: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Decode Raw-bearing construction outputs exactly; structured requests never take this route."""
    match content.get("schema"):
        case "pinboard-work-brief-contract/v1":
            msgspec.json.decode(msgspec.json.encode(content), type=work_brief_contract.WorkBriefContract)
        case "pinboard-work-brief-starter/v1":
            msgspec.json.decode(msgspec.json.encode(content), type=work_brief_contract.WorkBriefStarterContract)
        case "pinboard-brief-source-plan/v1":
            msgspec.convert(content, type=brief_source_models.BriefSourcePlanView, strict=True)
        case "pinboard-brief-source-plan-output/v1":
            msgspec.convert(content, type=BriefSourcePlanOutputResult, strict=True)
        case "pinboard-brief-source-batch/v1":
            msgspec.convert(content, type=BriefSourceBatchResult, strict=True)
        case "pinboard-mcp-brief-contract-result/v1":
            msgspec.convert(content, type=BriefContractRejected, strict=True)
        case "pinboard-mcp-brief-sources-result/v1":
            if content.get("status") == "committed-effect":
                msgspec.convert(content, type=BriefSourcesPublishedFailure, strict=True)
            else:
                msgspec.convert(content, type=BriefSourcesRejected, strict=True)
        case "pinboard-mcp-execution-result/v1":
            msgspec.convert(
                content,
                type=TracePreflightResult if content.get("status") == "rejected" else ExecutorBusyResult,
                strict=True,
            )
        case unexpected:
            raise ValueError(f"Unsupported brief preparation result schema: {unexpected}")
    return content


def validate_result(tool_name: str, content: dict[str, JsonValue]) -> dict[str, JsonValue]:  # noqa: C901, PLR0912, PLR0915
    """Validate one emitted result against the exact alternative it claims."""
    if tool_name in {"pinboard_brief_contract", "pinboard_brief_sources"}:
        return validate_brief_preparation_result(content)
    schema = content.get("schema")
    status = content.get("status")
    code = content.get("code")
    surfaces = content.get("changed_surfaces")
    if schema == "pinboard-mcp-execution-result/v1":
        msgspec.convert(content, type=TracePreflightResult if status == "rejected" else ExecutorBusyResult, strict=True)
    elif tool_name == "pinboard_order":
        msgspec.convert(content, type=OrderRejected if status == "rejected" else OrderCommitted, strict=True)
    elif tool_name == "pinboard_pr_review":
        msgspec.convert(content, type=PrReviewRejected if status == "rejected" else PrReviewSuccess, strict=True)
    elif tool_name == "pinboard_parallel_preview":
        msgspec.convert(
            content, type=ParallelPreviewRejected if status == "rejected" else ParallelPreviewSuccess, strict=True
        )
    elif tool_name == "pinboard_item_definition":
        if schema == "pinboard-item-definition/v1":
            msgspec.convert(content, type=query_models.ItemDefinition, strict=True)
        elif schema == "pinboard-item-definition-history/v1":
            msgspec.convert(content, type=query_models.ItemDefinitionHistory, strict=True)
        else:
            msgspec.convert(content, type=ItemDefinitionRejected, strict=True)
    elif tool_name == "pinboard_brief_review":
        brief = content.get("brief")
        brief_schema = brief.get("schema") if isinstance(brief, dict) else None
        if status == "no-needs-correction-evidence":
            result_type = (
                LegacyBriefReviewNoEvidence
                if brief_schema == "pinboard-work-brief/v2"
                else RetainedV3BriefReviewNoEvidence
                if brief_schema == "pinboard-work-brief/v3"
                else BriefReviewNoEvidence
            )
            msgspec.convert(
                content,
                type=result_type,
                strict=True,
            )
        elif status == "needs-correction":
            result_type = (
                LegacyBriefReviewNeedsCorrection
                if brief_schema == "pinboard-work-brief/v2"
                else RetainedV3BriefReviewNeedsCorrection
                if brief_schema == "pinboard-work-brief/v3"
                else BriefReviewNeedsCorrection
            )
            msgspec.convert(
                content,
                type=result_type,
                strict=True,
            )
        elif status == "committed":
            msgspec.convert(content, type=BriefReviewCommitted, strict=True)
        elif status == "unchanged":
            msgspec.convert(content, type=BriefReviewUnchanged, strict=True)
        elif status == "failed-after-publication":
            msgspec.convert(content, type=BriefReviewInfrastructureFailure, strict=True)
        elif status == "infrastructure-failure":
            msgspec.convert(content, type=BriefReviewInfrastructureUnchangedFailure, strict=True)
        elif surfaces == ["immutable-artifact"]:
            msgspec.convert(content, type=BriefReviewPublishedRejection, strict=True)
        else:
            msgspec.convert(content, type=BriefReviewRejected, strict=True)
    elif tool_name == "pinboard_dispatch":
        if status == "ready":
            result_type = DispatchReady
        elif status == "failed-after-publication":
            result_type = DispatchFailedAfterPublication
        elif status == "infrastructure-failure":
            result_type = DispatchInfrastructureFailure
        elif code == "DISPATCH_INVALID":
            result_type = DispatchInvalid
        else:
            result_type = DispatchRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_review_job":
        if status == "ready":
            result_type = ReviewJobReady
        elif status == "recorded":
            result_type = CandidateReviewRecorded
        elif status == "failed-after-publication":
            result_type = ReviewJobFailedAfterPublication
        elif status == "infrastructure-failure":
            result_type = ReviewJobInfrastructureFailure
        elif code == "REVIEW_JOB_INVALID":
            result_type = ReviewJobInvalid
        elif "recovery" in content:
            result_type = ReviewJobCandidateRequired
        else:
            result_type = ReviewJobRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_candidate_observe":
        msgspec.convert(
            content, type=CandidateObserved if status == "observed" else CandidateObservationRejected, strict=True
        )
    elif tool_name == "pinboard_candidate_restore":
        if status == "restored":
            result_type = CandidateRestoreReady
        elif status == "failed-after-mutation":
            result_type = CandidateRestoreFailed
        elif code == "CANDIDATE_RESTORE_INVALID":
            result_type = CandidateRestoreInvalid
        else:
            result_type = CandidateRestoreRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_transition":
        if status in {"committed", "committed-with-warning"}:
            result_type = TransitionCommitted
        elif status == "failed-after-publication":
            result_type = TransitionFailedAfterPublication
        else:
            result_type = TransitionRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_preparation_authority":
        if status == "present":
            result_type = PreparationAuthorityStatusPresent
        elif status == "absent":
            result_type = PreparationAuthorityStatusAbsent
        elif status in {"committed", "committed-with-warning"}:
            result_type = PreparationAuthorityCommitted
        else:
            result_type = PreparationAuthorityRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_attempt_authority":
        if status == "present":
            result_type = AttemptAuthorityStatusPresent
        elif status == "absent":
            result_type = AttemptAuthorityStatusAbsent
        elif status in {"committed", "committed-with-warning"}:
            result_type = AttemptAuthorityCommitted
        else:
            result_type = AttemptAuthorityRejected
        msgspec.convert(content, type=result_type, strict=True)
    elif schema == "pinboard-overview/v6" and tool_name == "pinboard_overview":
        msgspec.convert(content, type=query_models.WorkOverview, strict=True)
    elif tool_name == "pinboard_actions" and status == "ok":
        action_values = content.get("actions")
        focused_completion = (
            isinstance(action_values, list)
            and len(action_values) == 1
            and isinstance(action_values[0], dict)
            and isinstance(action_values[0].get("input_contract"), dict)
            and "checkpoint_packages" in action_values[0]["input_contract"]
        )
        if focused_completion:
            msgspec.convert(content, type=CompletionActionsSuccess, strict=True)
        else:
            msgspec.convert(content, type=ActionsSuccess, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and status == "ok":
        continuation = content.get("continuation")
        state = continuation.get("state") if isinstance(continuation, dict) else None
        result_type = TerminalAttemptInspectionSuccess if state == "done" else NonterminalAttemptInspectionSuccess
        msgspec.convert(content, type=result_type, strict=True)
    elif tool_name == "pinboard_correction_context":
        msgspec.convert(
            content, type=CorrectionContextReady if status == "ready" else CorrectionContextRejected, strict=True
        )
    elif tool_name == "pinboard_artifact_verify" and schema == "pinboard-verified-artifact-reference/v1":
        msgspec.convert(content, type=ArtifactVerified, strict=True)
    elif tool_name == "pinboard_overview":
        msgspec.convert(content, type=OverviewRejected, strict=True)
    elif tool_name == "pinboard_actions" and code == "ACTIONS_INVALID":
        msgspec.convert(content, type=ActionsInvalid, strict=True)
    elif tool_name == "pinboard_actions" and code == "ATTEMPT_LEASE_REQUIRED":
        msgspec.convert(content, type=AttemptLeaseRequired, strict=True)
    elif tool_name == "pinboard_actions":
        msgspec.convert(content, type=ActionUnavailable, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and code == "ATTEMPT_INSPECT_INVALID":
        msgspec.convert(content, type=AttemptInspectInvalid, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and code == "ATTEMPT_NOT_FOUND":
        msgspec.convert(content, type=AttemptNotFound, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and code == "ATTEMPT_BRIEF_INVALID":
        msgspec.convert(content, type=AttemptBriefInvalid, strict=True)
    elif tool_name == "pinboard_attempt_inspect" and code == "TRANSITION_RECEIPT_DAMAGED":
        msgspec.convert(content, type=AttemptReceiptDamaged, strict=True)
    elif tool_name == "pinboard_attempt_inspect":
        msgspec.convert(content, type=AttemptActionUnavailable, strict=True)
    elif tool_name == "pinboard_artifact_verify" and code == "ARTIFACT_VERIFY_INVALID":
        msgspec.convert(content, type=ArtifactVerificationInvalid, strict=True)
    elif tool_name == "pinboard_artifact_verify" and code == "ARTIFACT_REFERENCE_MISMATCH":
        msgspec.convert(content, type=ArtifactReferenceMismatch, strict=True)
    elif tool_name == "pinboard_artifact_verify":
        msgspec.convert(content, type=ArtifactBytesInvalid, strict=True)
    elif schema == "pinboard-item-status/v2" and tool_name == "pinboard_item_status":
        msgspec.convert(content, type=query_models.ItemStatus, strict=True)
    elif schema == "pinboard-branch-owners/v1" and tool_name == "pinboard_item_status":
        msgspec.convert(content, type=query_models.BranchOwners, strict=True)
    elif schema == "pinboard-item-integration/v1" and tool_name == "pinboard_item_status":
        msgspec.convert(content, type=query_models.ItemIntegration, strict=True)
    elif tool_name == "pinboard_item_status" and code == "ITEM_STATUS_INVALID":
        msgspec.convert(content, type=ItemStatusInvalid, strict=True)
    elif tool_name == "pinboard_item_status" and code in {"ITEM_NOT_FOUND", "ITEM_DEFINITION_INVALID"}:
        msgspec.convert(content, type=ItemStatusUnavailable, strict=True)
    elif tool_name == "pinboard_item_status" and code == "BRANCH_OWNER_NOT_FOUND":
        msgspec.convert(content, type=BranchOwnerNotFound, strict=True)
    elif tool_name == "pinboard_item_status" and code == "TRANSITION_RECEIPT_DAMAGED":
        msgspec.convert(content, type=ItemStatusReceiptDamaged, strict=True)
    elif tool_name == "pinboard_item_status" and code in {
        "INTEGRATION_TARGET_UNRESOLVED",
        "INTEGRATION_CANDIDATE_UNAVAILABLE",
        "INTEGRATION_CANDIDATE_EVIDENCE_INVALID",
        "PROJECT_GIT_CHECKOUT_UNAVAILABLE",
        "PROJECT_GIT_EXCLUDE_UNAVAILABLE",
        "PROJECT_GIT_LAYOUT_UNSUPPORTED",
        "PROJECT_GIT_ROOT_UNAVAILABLE",
    }:
        msgspec.convert(content, type=IntegrationStatusRejected, strict=True)
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
        msgspec.convert(content, type=BriefPublicationInfrastructureFailure, strict=True)
    elif status == "infrastructure-failure":
        msgspec.convert(content, type=BriefPublicationInfrastructureUnchangedFailure, strict=True)
    elif status == "rejected" and surfaces == ["immutable-artifact"]:
        msgspec.convert(content, type=BriefPublishedRejection, strict=True)
    elif status == "rejected":
        msgspec.convert(
            content,
            type=BriefArchitectureImpactRejected if "recovery" in content else BriefRejected,
            strict=True,
        )
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
