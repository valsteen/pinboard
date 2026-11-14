"""Compose exact initiative leaves, transactional changes and replaceable views."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never
from urllib.parse import urlsplit

import msgspec

from pinboard.adapters.files.errors import RootError
from pinboard.adapters.files.file_io import DurableRoots
from pinboard.adapters.files.models import AffectedViews, ViewWarning
from pinboard.application import initiative_records, initiatives, query_models, roadmap
from pinboard.application.ports import GeneratedViewReader
from pinboard.domain import history
from pinboard.domain import initiatives as models
from pinboard.domain.identifiers import HostId, TaskId, WorkItemId
from pinboard.mcp import common, execution
from pinboard.mcp import initiative_contracts as contracts
from pinboard.mcp.contracts import JsonValue


def rejected(code: models.RejectionCode, message: str) -> execution.TypedOperationResult[contracts.Result]:
    value = contracts.Rejected(
        "pinboard-initiative-result/v1",
        "rejected",
        code,
        message,
        "Correct the named condition; read this initiative or the roadmap with the same project and work roots before another change.",
        False,
        "unchanged",
        "correct-input",
        (),
    )
    return execution.TypedOperationResult(value, "rejected", None)


def operate(  # noqa: C901, PLR0912 - exact leaf conversion and outer effects
    arguments: Mapping[str, JsonValue], token: execution.CancellationToken
) -> execution.TypedOperationResult[contracts.Result]:
    try:
        request = msgspec.convert(arguments, type=contracts.Envelope, strict=True).request
    except (msgspec.ValidationError, ValueError) as error:
        return rejected(models.RejectionCode.INITIATIVE_INPUT_INVALID, f"Cannot decode initiative request: {error}")
    token.checkpoint()
    try:
        source_checkout, durable = common._resolve_source_and_durable(request.project_root, request.work_root)
    except (ValueError, OSError, RootError) as error:
        return rejected(
            models.RejectionCode.INITIATIVE_ROOT_INVALID,
            f"Cannot resolve initiative project '{request.project_root}' and work root '{request.work_root}': {error}",
        )
    store = common.compose_store(durable)
    if isinstance(request, contracts.Roadmap):
        return _roadmap_result(store.read_roadmap())
    if isinstance(request, contracts.Read):
        initiative_id = models.InitiativeId(request.initiative_id)
        value = store.read_initiative(initiative_id)
        if value is None:
            return rejected(models.RejectionCode.INITIATIVE_NOT_FOUND, f"Initiative '{initiative_id}' is absent.")
        return execution.TypedOperationResult(
            contracts.ReadResult(
                "pinboard-initiative-result/v1",
                "ok",
                initiative_records.initiative_record(value),
                False,
                "unchanged",
                "safe-to-repeat",
                (),
            ),
            "ok",
            None,
        )
    token.checkpoint()
    now = datetime.now(UTC)
    reason = models.HumanReason(request.human, request.date, request.reason)
    actor_task_id = TaskId(request.actor_task_id)
    actor_host_id = HostId(request.actor_host_id)
    if isinstance(request, (contracts.MarkOffRoadmap, contracts.ClearOffRoadmap)):
        disposition: models.DispositionChange
        match request:
            case contracts.MarkOffRoadmap():
                disposition = models.MarkOffRoadmap(request.expected_disposition_revision, WorkItemId(request.item_id))
            case contracts.ClearOffRoadmap():
                disposition = models.ClearOffRoadmap(
                    request.expected_disposition_revision,
                    request.expected_episode,
                    WorkItemId(request.item_id),
                )
        result = initiatives.change_disposition(store, disposition, reason, actor_task_id, actor_host_id, now)
        if isinstance(outcome := _mutation_outcome(durable, store, result, now), models.Rejected):
            return rejected(outcome.code, outcome.message)
        result, warning, result_status, execution_status = outcome
        value = contracts.DispositionCommitted(
            "pinboard-initiative-result/v1",
            result_status,
            request.item_id,
            "mark-off-roadmap" if isinstance(request, contracts.MarkOffRoadmap) else "clear-off-roadmap",
            result,
            True,
            "committed",
            "do-not-retry",
            ("ledger",),
            None if warning is None else contracts.Warning(warning.message, warning.repair),
        )
        return execution.TypedOperationResult(value, execution_status, str(result))
    initiative_id = models.InitiativeId(request.initiative_id)
    change: models.Change
    match request:
        case contracts.Create():
            change = models.Create(
                models.Definition(
                    request.title,
                    request.goal,
                    request.rationale,
                    request.success_signal,
                    _project_references(source_checkout, request.project_references),
                )
            )
        case contracts.Revise():
            change = models.Revise(
                request.expected_revision,
                models.Definition(
                    request.title,
                    request.goal,
                    request.rationale,
                    request.success_signal,
                    _project_references(source_checkout, request.project_references),
                ),
            )
        case contracts.AddMember():
            change = models.AddMember(request.expected_revision, WorkItemId(request.item_id))
        case contracts.RemoveMember():
            change = models.RemoveMember(request.expected_revision, WorkItemId(request.item_id))
        case contracts.Decision():
            change = models.RecordDecision(request.expected_revision, request.decision, request.supporting_links)
        case contracts.Assess():
            change = models.Assess(
                request.expected_revision, request.evaluation_context, request.judgment, request.supporting_links
            )
        case _ as unreachable:
            assert_never(unreachable)
    result = initiatives.change(store, initiative_id, change, reason, actor_task_id, actor_host_id, now)
    if isinstance(outcome := _mutation_outcome(durable, store, result, now), models.Rejected):
        return rejected(outcome.code, outcome.message)
    result, warning, result_status, execution_status = outcome
    value = contracts.Committed(
        "pinboard-initiative-result/v1",
        result_status,
        initiative_id,
        result,
        True,
        "committed",
        "do-not-retry",
        ("ledger",),
        None if warning is None else contracts.Warning(warning.message, warning.repair),
    )
    return execution.TypedOperationResult(value, execution_status, str(result))


type MutationOutcome = tuple[
    int,
    ViewWarning | None,
    Literal["committed", "committed-with-warning"],
    Literal["committed", "committed-warning"],
]


def _mutation_outcome(
    durable: DurableRoots,
    store: GeneratedViewReader,
    result: int | models.Rejected,
    now: datetime,
) -> MutationOutcome | models.Rejected:
    if isinstance(result, models.Rejected):
        return result
    warning = common._refresh_affected_views(durable, store, AffectedViews((), (), ()), now).warning
    if warning is None:
        return result, None, "committed", "committed"
    return result, warning, "committed-with-warning", "committed-warning"


def _project_references(project_root: Path, references: tuple[str, ...]) -> tuple[str, ...]:
    resolved: list[str] = []
    for reference in references:
        try:
            scheme = urlsplit(reference).scheme
        except ValueError:
            resolved.append(reference)
            continue
        resolved.append(
            str(project_root / reference) if not scheme and not Path(reference).is_absolute() else reference
        )
    return tuple(resolved)


def _roadmap_result(value: roadmap.Projection) -> execution.TypedOperationResult[contracts.Result]:
    items: list[contracts.RoadmapItem] = []
    for item in value.facts.items:
        placement: contracts.Wave | contracts.Blocked | contracts.Unknown | contracts.Terminal
        match value.placements.get(item.item_id):
            case roadmap.Wave(number=number):
                placement = contracts.Wave(number)
            case roadmap.Blocked(path=path):
                placement = contracts.Blocked(path)
            case roadmap.Unknown(condition=condition):
                placement = contracts.Unknown(condition)
            case None:
                placement = contracts.Terminal()
            case _ as unreachable:
                assert_never(unreachable)
        items.append(
            contracts.RoadmapItem(
                item.item_id,
                item.title,
                item.state,
                tuple(history.dependency_payload(target) for target in item.dependencies),
                item.queue_position,
                placement,
                tuple(
                    query_models.DependencyStatus(
                        history.dependency_payload(condition.target),
                        condition.satisfied,
                        condition.target in value.facts.accepted_checkpoints,
                    )
                    for condition in value.dependency_conditions[item.item_id]
                ),
            )
        )
    result = contracts.RoadmapResult(
        "pinboard-roadmap-result/v1",
        "ok",
        value.facts.project_revision,
        tuple(initiative_records.initiative_record(initiative) for initiative in value.facts.initiatives),
        tuple(items),
        value.unassigned,
        tuple(initiative_records.off_roadmap_episode_record(episode) for episode in value.facts.off_roadmap_episodes),
        value.external_prerequisites,
        msgspec.convert(value.totals, type=contracts.Counts, from_attributes=True),
        "Distinct current initiative members, unassigned live items and off-roadmap history",
        "Read-only snapshot; reload generated roadmap.html after refresh. Waves grant no execution authority.",
        False,
        "unchanged",
        "safe-to-repeat",
        (),
    )
    return execution.TypedOperationResult(result, "ok", None)
