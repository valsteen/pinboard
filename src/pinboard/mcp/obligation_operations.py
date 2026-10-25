"""Native read-only exact-pair and current planned-replacement obligation coverage."""

from typing import assert_never

import msgspec

from pinboard.adapters.files.errors import RootError
from pinboard.application import obligation_coverage
from pinboard.domain.errors import DecisionFailure, DecisionFailureCode
from pinboard.domain.identifiers import WorkItemId
from pinboard.mcp import common, contracts, execution
from pinboard.mcp.contracts import JsonValue


def read(arguments: dict[str, JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(arguments, type=contracts.ObligationCoverageEnvelope, strict=True).request
        durable = common._resolve_durable(request.project_root, request.work_root)
    except (msgspec.ValidationError, ValueError, OSError, RootError) as error:
        result = contracts.ObligationCoverageRejected(
            "pinboard-obligation-coverage-result/v1",
            "rejected",
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            f"Cannot read obligation coverage: {error}. Correct the request and exact initialized roots; nothing was changed.",
            False,
            "unchanged",
            "correct-input",
            (),
        )
        return execution.OperationResult(msgspec.to_builtins(result), "rejected", None)
    match request:
        case contracts.ObligationCoveragePairRequest():
            source = WorkItemId(request.source_item)
            replacement = WorkItemId(request.replacement_item)
        case contracts.ObligationCoveragePlannedRequest():
            source = WorkItemId(request.item_id)
            replacement = None
        case _ as unreachable:
            assert_never(unreachable)
    store = common.compose_store(durable)
    coverage = obligation_coverage.report(store.read_obligation_coverage(source, replacement))
    if isinstance(coverage, DecisionFailure):
        rejected = contracts.ObligationCoverageRejected(
            "pinboard-obligation-coverage-result/v1",
            "rejected",
            coverage.code,
            coverage.message,
            False,
            "unchanged",
            "correct-input",
            (),
        )
        return execution.OperationResult(msgspec.to_builtins(rejected), "rejected", None)
    success = contracts.ObligationCoverageSuccess(
        "pinboard-obligation-coverage-result/v1", "ok", coverage, False, "unchanged", "safe-to-repeat", ()
    )
    return execution.OperationResult(msgspec.to_builtins(success), "ok", None)
