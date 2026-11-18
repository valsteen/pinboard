"""Recorded route presence; absence is retained data, never an inferred classification."""

from dataclasses import dataclass
from typing import Literal, assert_never, final

from pinboard.domain import decision_models, work_models
from pinboard.domain.errors import (
    DecisionFailure,
    DecisionFailureCode,
    DecisionResult,
    EffectDisposition,
    FailureDetails,
    FailureFact,
    FailureMismatch,
    RetryDisposition,
)
from pinboard.domain.identifiers import AttemptId, WorkItemId
from pinboard.domain.ledger import LedgerSnapshot


@final
@dataclass(frozen=True, slots=True)
class RouteClassificationRequired:
    """A route-dependent action on a current definition that records no route facts.

    Its details always name the definition, so a consumer that projects them needs no absence check.
    """

    message: str
    details: FailureDetails

    def failure(self) -> DecisionFailure[Literal[DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED]]:
        return DecisionFailure(DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED, self.message, self.details)


def route_classification_required(anchor: work_models.DefinitionAnchor) -> RouteClassificationRequired:
    return RouteClassificationRequired(
        f"Item '{anchor.work_item_id}' definition revision {anchor.revision} ({anchor.digest}) has no recorded route facts. "
        "Revise its complete current definition with explicitly supplied size_class, architecture_impact, "
        "external_effects and declares_checkpoints; do not infer defaults. This grants no action availability. "
        "Classification changes the definition identity: an existing brief becomes stale and must follow ordinary rebind before reuse.",
        FailureDetails(
            observed=(
                FailureFact("item_id", str(anchor.work_item_id)),
                FailureFact("definition_revision", anchor.revision),
                FailureFact("definition_digest", anchor.digest),
            ),
            mismatches=(FailureMismatch("route_facts", "explicit recorded classification", None),),
            retry=RetryDisposition.CORRECT_INPUT,
            effect=EffectDisposition.UNCHANGED,
            changed_surfaces=(),
            alternatives=(),
        ),
    )


def require_route_facts(
    anchor: work_models.DefinitionAnchor,
) -> DecisionResult[work_models.RouteFacts, Literal[DecisionFailureCode.ROUTE_CLASSIFICATION_REQUIRED]]:
    route = anchor.definition.route_facts
    if route is not None:
        return route
    return route_classification_required(anchor).failure()


def action_definition(snapshot: LedgerSnapshot, action: decision_models.Action) -> work_models.DefinitionAnchor | None:
    semantics = decision_models.action_semantics(action.kind)
    match semantics.subject_kind:
        case decision_models.ActionSubjectKind.ITEM:
            return snapshot.definition(WorkItemId(str(action.capability.subject)))
        case decision_models.ActionSubjectKind.ATTEMPT:
            attempt = snapshot.attempt(AttemptId(str(action.capability.subject)))
            return None if attempt is None else snapshot.definition(attempt.work_item_id)
        case decision_models.ActionSubjectKind.LEDGER | decision_models.ActionSubjectKind.PROPOSAL:
            return None
        case _ as unreachable:
            assert_never(unreachable)


def validate_action_route(
    snapshot: LedgerSnapshot, action: decision_models.Action
) -> DecisionFailure[Literal[DecisionFailureCode.ACTION_NOT_AVAILABLE]] | RouteClassificationRequired | None:
    if not decision_models.action_semantics(action.kind).requires_recorded_route:
        return None
    anchor = action_definition(snapshot, action)
    if anchor is None:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            f"Action '{decision_models.action_id(action)}' has no current executable definition. Read the exact item and repair its definition through the supported revision action; do not reconstruct stored state.",
            None,
        )
    if (
        isinstance(action, decision_models.ResumeAction)
        and (item := snapshot.work_item(action.capability.subject)) is not None
        and item.attempt is None
    ):
        return None
    if anchor.definition.route_facts is None:
        return route_classification_required(anchor)
    return None
