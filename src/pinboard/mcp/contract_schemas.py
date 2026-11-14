"""Generate negotiated MCP request and result schemas."""

from types import UnionType
from typing import TypeAliasType, assert_never, get_args

import msgspec
from typing_extensions import TypeForm

from pinboard.adapters import dispatch_operations
from pinboard.application import action_models, dispatch_models
from pinboard.domain import decision_models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.mcp.contracts import (
    _ACTIVE_CONTINUATION_ACTION_KINDS,
    _BLOCKED_CONTINUATION_ACTION_KINDS,
    _PAUSED_CONTINUATION_ACTION_KINDS,
    _REVIEW_CONTINUATION_ACTION_KINDS,
    ActionsEnvelope,
    DispatchRequest,
    ExecutorBusyResult,
    JsonSchemaValue,
    SchemaUnsupportedResult,
    TracePreflightResult,
    TransitionRequest,
    _committed_transition_surfaces,
    _fixed_state_changed,
    action_invocation_tool,
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
    # Advertised patterns carry these code points as literal characters, never as \u escapes, which RE2 rejects.
    python_space = "\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"
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
    return projected, r"[\r\n" "\u2028\u2029]$"


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
                    if "not" in value:
                        raise ValueError(f"MCP pattern already has composed constraints: {pattern}")
                    value["not"] = {"pattern": terminator}
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)


def schema_for(boundary_type: type[msgspec.Struct]) -> dict[str, JsonSchemaValue]:
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


def dispatch_request_schema(request: type[DispatchRequest]) -> dict[str, JsonSchemaValue]:
    """Inline dispatch records so host reference-expansion limits retain every choice."""

    schema = schema_for(request)
    definitions = schema.pop("$defs")
    assert isinstance(definitions, dict)

    def inline(value: JsonSchemaValue) -> JsonSchemaValue:
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                if set(value) != {"$ref"} or not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                    raise ValueError("Dispatch schema requires standalone local definition references.")
                return inline(definitions[reference.removeprefix("#/$defs/")])
            return {
                key: child if key in {"const", "enum", "default", "examples"} else inline(child)
                for key, child in value.items()
                if key != "discriminator"
            }
        if isinstance(value, list):
            return [inline(child) for child in value]
        return value

    result = inline(schema)
    assert isinstance(result, dict)
    return result


def actions_request_schema() -> dict[str, JsonSchemaValue]:
    """Return the exact role-discriminated action request schema."""

    return schema_for(ActionsEnvelope)


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
    definitions: dict[str, JsonSchemaValue], boundary_types: tuple[type[msgspec.Struct], ...]
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
    input_contract = definitions["InputContractView"]
    assert isinstance(input_contract, dict)
    required = input_contract["required"]
    assert isinstance(required, list)
    input_contract["required"] = [field for field in required if field != "payload_schema"]
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
                "subject_revision": {"type": ["string", "null"]},
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
    authority_correlations: list[JsonSchemaValue] = [{"oneOf": authority_shapes}]
    for role, authority in role_authorities.items():
        authority_correlations.append(
            {
                "if": {"properties": {"authorization": {"const": authority}}},
                "then": {
                    "properties": {
                        "invocation": {
                            "if": {"type": "object", "properties": {"tool": {"const": "pinboard_transition"}}},
                            "then": {
                                "properties": {
                                    "arguments": {
                                        "properties": {"request": {"properties": {"role": {"const": role.value}}}}
                                    }
                                }
                            },
                        }
                    }
                },
            }
        )
    correlations = list(authority_correlations)
    for kind in decision_models.ActionKind:
        semantics = decision_models.action_semantics(kind)
        semantic_constraint = _action_semantics_constraint(kind)
        payload_schema = action_models.action_payload_schema(kind)
        payload_constraint: JsonSchemaValue = (
            {"not": {"required": ["payload_schema"]}}
            if payload_schema is None
            else {"required": ["payload_schema"], "properties": {"payload_schema": {"const": payload_schema}}}
        )
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
                        "subject_revision": {
                            "type": ["string", "null"]
                            if semantics.lifecycle_effect == decision_models.LifecycleEffect.NO_LIFECYCLE_CHANGE
                            else "string"
                        },
                        "semantics": semantic_constraint,
                        "invocation": _action_invocation_constraint(kind),
                        "input_contract": {
                            "type": "object",
                            "properties": {
                                "action_kind": {"const": kind.value},
                                "semantics": semantic_constraint,
                            },
                            "allOf": [payload_constraint],
                        },
                    }
                },
            }
        )
    definition["allOf"] = correlations
    completion = definitions.get("CompletionActionView")
    if isinstance(completion, dict):
        kind = decision_models.ActionKind.COMPLETE
        semantics = decision_models.action_semantics(kind)
        semantic_constraint = _action_semantics_constraint(kind)
        completion["allOf"] = [
            *authority_correlations,
            {
                "properties": {
                    "action_id": {"properties": {"kind": {"const": kind.value}}},
                    "authorization": {"enum": [role_authorities[role] for role in semantics.permitted_roles]},
                    "semantics": semantic_constraint,
                    "invocation": _action_invocation_constraint(kind),
                    "input_contract": {
                        "properties": {
                            "action_kind": {"const": kind.value},
                            "semantics": semantic_constraint,
                            "payload_schema": {
                                "const": msgspec.json.schema(action_models.ReviewedCompleteInputPayload)
                            },
                        }
                    },
                }
            },
        ]
    close = definitions.get("CloseActionView")
    if isinstance(close, dict):
        close["allOf"] = correlations


def _action_invocation_constraint(kind: decision_models.ActionKind) -> JsonSchemaValue:
    tool = action_invocation_tool(kind)
    if tool is None:
        return {"type": "null"}
    receipt: dict[str, JsonSchemaValue] = {
        "properties": {"receipt": {"properties": {"action_id": {"properties": {"kind": {"const": kind.value}}}}}}
    }
    match tool:
        case "pinboard_transition":
            arguments = dict[str, JsonSchemaValue](properties={"request": receipt})
        case "pinboard_dispatch":
            arguments = dict[str, JsonSchemaValue](properties={"dispatch": receipt})
        case "pinboard_close":
            arguments = receipt
        case _ as unreachable:
            assert_never(unreachable)
    properties = dict[str, JsonSchemaValue](tool={"const": tool}, arguments=arguments)
    return {"type": "object", "properties": properties}


def _apply_transition_constraints(definitions: dict[str, JsonSchemaValue]) -> None:
    alternatives: list[JsonSchemaValue] = [
        {
            "properties": {
                "action_id": {"properties": {"kind": {"const": kind.value}}},
                "changed_surfaces": {
                    "type": "array",
                    "prefixItems": [{"const": surface} for surface in surfaces],
                    "minItems": len(surfaces),
                    "maxItems": len(surfaces),
                },
                **({"local_cleanup": False} if kind != decision_models.ActionKind.COMPLETE else {}),
            }
        }
        for kind in decision_models.ActionKind
        for surfaces in _committed_transition_surfaces(kind)
    ]
    for name in ("TransitionCommitted", "TransitionCommittedStatusUnavailable"):
        definition = definitions.get(name)
        if isinstance(definition, dict):
            definition["oneOf"] = alternatives
            required = definition.get("required")
            if isinstance(required, list):
                required.remove("local_cleanup")


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
    for name in ("TerminalAttemptContinuation", "CancelledAttemptContinuation", "SupersededAttemptContinuation"):
        terminal = definitions.get(name)
        terminal_properties = terminal.get("properties") if isinstance(terminal, dict) else None
        if isinstance(terminal_properties, dict):
            terminal_properties["terminal"] = {"type": "boolean", "const": True}
            terminal_properties["user_input_required"] = {"type": "boolean", "const": False}
            terminal_properties["forbidden_routes"] = _forbidden_routes_constraint()
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
                _action_continuation(decision_models.ActionKind.REVIEW_CANDIDATE),
            ),
            _REVIEW_CONTINUATION_ACTION_KINDS,
        ),
        (
            "PausedAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationDependencies"},
                _action_continuation(decision_models.ActionKind.RESUME),
                _action_continuation(decision_models.ActionKind.CLOSE),
            ),
            _PAUSED_CONTINUATION_ACTION_KINDS,
        ),
        (
            "BlockedAttemptContinuation",
            (
                {"$ref": "#/$defs/ContinuationDependencies"},
                _action_continuation(decision_models.ActionKind.RESUME),
                _action_continuation(decision_models.ActionKind.CLOSE),
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
        "ReplacementReadinessReady",
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
            if not failed and name not in ("CandidateReviewRecorded", "ReplacementReadinessReady"):
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


def union_schema_for(boundary_types: tuple[type[msgspec.Struct], ...]) -> dict[str, JsonSchemaValue]:
    """Return one closed MCP result schema from separate correlated records."""
    if ExecutorBusyResult in boundary_types:
        boundary_types = (*boundary_types, TracePreflightResult, SchemaUnsupportedResult)
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
    for name in ("CorrectionContextReady", "CorrectionReviewEvidence"):
        correction = definitions.get(name)
        if isinstance(correction, dict):
            correction["anyOf"] = [
                {"properties": {"reuse_eligible": {"const": True}, "reuse_blockers": {"maxItems": 0}}},
                {"properties": {"reuse_eligible": {"const": False}, "reuse_blockers": {"minItems": 1}}},
            ]
    return {"type": "object", "anyOf": list[JsonSchemaValue](schemas), "$defs": definitions}


def typed_result_schema(results: TypeForm[msgspec.Struct]) -> dict[str, JsonSchemaValue]:
    """Advertise a typed tool's result union, declared as a `type` alias, followed by the shared envelopes."""
    if not isinstance(results, TypeAliasType):
        raise TypeError("A typed tool's results must be the `type` alias that names its result union.")
    value = results.__value__
    members = get_args(value) if isinstance(value, UnionType) else (value,)
    return union_schema_for((*members, ExecutorBusyResult))
