"""Verify selected evidence bytes and present one bounded page; never grant authority or write state."""

from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard.adapters.files import evidence_read as files
from pinboard.mcp import contracts, execution, host_profiles, read_operations


def _rejected(
    resource: str, message: str, budget: int | None, page_bytes: int, profile: host_profiles.HostProfile
) -> execution.TypedOperationResult[contracts.EvidenceReadResult]:
    result = contracts.EvidenceReadRejected(
        "pinboard-evidence-read-result/v1",
        "rejected",
        "EVIDENCE_READ_INVALID",
        resource,
        message,
        "Correct the named source or request using the producing response; restart with artifact-start or file-start. Do not acquire authority from incomplete text.",
        False,
        "unchanged",
        "correct-input",
        (),
    )
    representation = host_profiles.representation(profile)
    if budget is not None and host_profiles.envelope_size(msgspec.to_builtins(result), representation) > budget:
        result = msgspec.structs.replace(result, message="Selected source or continuation is invalid.")
        if host_profiles.envelope_size(msgspec.to_builtins(result), representation) > budget:
            result = msgspec.structs.replace(
                result,
                resource="max_batch_bytes",
                message="The budget cannot fit source metadata or its diagnosis.",
                recovery=(
                    f'Use max_batch_bytes "host-profile" (this host allows {page_bytes}) '
                    "or shorten selected paths, then restart the exact source."
                ),
            )
    return execution.TypedOperationResult(result, "rejected", None)


def _page(
    request: contracts.EvidenceReadArtifactStart | contracts.EvidenceReadFileStart | contracts.EvidenceReadContinue,
    budget: int,
    source: contracts.EvidenceReadSource,
    text: str,
    start: int,
    end: int,
) -> contracts.EvidenceReadPage:
    continuation = (
        contracts.EvidenceReadContinue(request.project_root, request.work_root, source, end, budget)
        if end < len(text)
        else None
    )
    return contracts.EvidenceReadPage(
        "pinboard-evidence-read-page/v1",
        "ok",
        source,
        start,
        end,
        text[start:end],
        budget,
        continuation,
        "delivery-only",
        False,
        "unchanged",
        "safe-to-repeat",
        (),
    )


def read(
    raw: Mapping[str, contracts.JsonValue], token: execution.CancellationToken, profile: host_profiles.HostProfile
) -> execution.TypedOperationResult[contracts.EvidenceReadResult]:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.EvidenceReadEnvelope, strict=True).request
        msgspec.json.encode(request)
    except (msgspec.ValidationError, ValueError) as error:
        return _rejected("request", str(error), None, host_profiles.measured_page_bytes(profile), profile)
    page_bytes = host_profiles.measured_page_bytes(profile)
    budget = host_profiles.effective_budget(request.max_batch_bytes, page_bytes)
    return _read_selected(request, budget, page_bytes, profile, token)


def _read_selected(
    request: contracts.EvidenceReadArtifactStart | contracts.EvidenceReadFileStart | contracts.EvidenceReadContinue,
    budget: int,
    page_bytes: int,
    profile: host_profiles.HostProfile,
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.EvidenceReadResult]:
    start = request.start_character if isinstance(request, contracts.EvidenceReadContinue) else 0
    expected: contracts.EvidenceReadSource | None
    match request:
        case contracts.EvidenceReadFileStart():
            expected = None
            path = Path(request.path)
        case contracts.EvidenceReadArtifactStart() | contracts.EvidenceReadContinue():
            expected = request.source
            match expected:
                case contracts.EvidenceReadArtifactSource():
                    verified = read_operations._verify_artifact(
                        {
                            "project_root": request.project_root,
                            "work_root": request.work_root,
                            "artifact_ref_id": expected.artifact_ref_id,
                            "selector": expected.selector,
                            "sha256": expected.sha256,
                            "size_bytes": expected.size_bytes,
                        },
                        token,
                    )
                    if not isinstance(verified.record, contracts.ArtifactVerified):
                        return _rejected(expected.selector, verified.record.message, budget, page_bytes, profile)
                    path = Path(request.work_root) / expected.selector
                case contracts.EvidenceReadFileSource():
                    path = Path(expected.path)
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)
    if not path.is_absolute():
        return _rejected(str(path), "Select an explicit absolute resolved path.", budget, page_bytes, profile)
    # Recheck the exact presented bytes after accepted-reference verification; EOF confers no authority.
    selected = files.read(path)
    if isinstance(selected, files.TextUnavailable):
        return _rejected(selected.resource, selected.message, budget, page_bytes, profile)
    if expected is not None and (selected.sha256 != expected.sha256 or selected.size_bytes != expected.size_bytes):
        return _rejected(
            str(path), "Source bytes changed; continuation cannot combine identities.", budget, page_bytes, profile
        )
    if isinstance(request, contracts.EvidenceReadContinue) and start >= len(selected.text):
        return _rejected(str(path), "Continuation is outside this exact source.", budget, page_bytes, profile)
    source = (
        expected
        if expected is not None
        else contracts.EvidenceReadFileSource(str(path), selected.sha256, selected.size_bytes)
    )
    token.checkpoint()
    return _bounded_page(request, budget, page_bytes, profile, source, selected.text, start, token)


def _bounded_page(
    request: contracts.EvidenceReadArtifactStart | contracts.EvidenceReadFileStart | contracts.EvidenceReadContinue,
    budget: int,
    page_bytes: int,
    profile: host_profiles.HostProfile,
    source: contracts.EvidenceReadSource,
    text: str,
    start: int,
    token: execution.CancellationToken,
) -> execution.TypedOperationResult[contracts.EvidenceReadResult]:
    representation = host_profiles.representation(profile)
    final = _page(request, budget, source, text, start, len(text))
    if host_profiles.envelope_size(msgspec.to_builtins(final), representation) <= budget:
        return execution.TypedOperationResult(final, "read", None)
    low, high = start, min(len(text) - 1, start + budget)
    while low < high:
        end = (low + high + 1) // 2
        candidate = _page(request, budget, source, text, start, end)
        if host_profiles.envelope_size(msgspec.to_builtins(candidate), representation) <= budget:
            low = end
        else:
            high = end - 1
    if low == start:
        return _rejected(
            "max_batch_bytes",
            "The budget cannot fit source identity, continuation and one character.",
            budget,
            page_bytes,
            profile,
        )
    token.checkpoint()
    return execution.TypedOperationResult(_page(request, budget, source, text, start, low), "read", None)
