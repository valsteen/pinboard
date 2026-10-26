"""Verify selected startup bytes and present one bounded page; never grant authority or write state."""

from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard import __version__
from pinboard.adapters.files import startup_text as files
from pinboard.mcp import contracts, execution, host_profiles, read_operations


def envelope_size(content: dict[str, contracts.JsonValue], representation: host_profiles.Representation) -> int:
    """Price the compact SDK tool result exactly as the selected representation sends it."""
    companion: list[dict[str, str]]
    match representation:
        case host_profiles.Representation.STRUCTURED_AND_TEXT:
            companion = [{"type": "text", "text": msgspec.json.encode(content).decode()}]
        case host_profiles.Representation.STRUCTURED_ONLY:
            companion = []
        case _ as unreachable:
            assert_never(unreachable)
    return len(
        msgspec.json.encode(
            {
                "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "pinboard", "version": __version__}},
                "content": companion,
                "structuredContent": content,
                "isError": False,
                "resultType": "complete",
            }
        )
    )


def _rejected(
    resource: str, message: str, budget: int | None, profile: host_profiles.HostProfile
) -> execution.OperationResult:
    result = contracts.StartupTextRejected(
        "pinboard-startup-text-result/v1",
        "rejected",
        "STARTUP_TEXT_INVALID",
        resource,
        message,
        "Correct the named source or request using the producing response; restart with artifact-start or file-start. Do not acquire authority from incomplete text.",
        False,
        "unchanged",
        "correct-input",
        (),
    )
    content: dict[str, contracts.JsonValue] = msgspec.to_builtins(result)
    representation = host_profiles.representation(profile)
    if budget is not None and envelope_size(content, representation) > budget:
        result = msgspec.structs.replace(result, message="Selected source or continuation is invalid.")
        content = msgspec.to_builtins(result)
        if envelope_size(content, representation) > budget:
            result = msgspec.structs.replace(
                result,
                resource="max_batch_bytes",
                message="The budget cannot fit source metadata or its diagnosis.",
                recovery=(
                    f'Use max_batch_bytes "host-profile" (this host allows {host_profiles.page_bytes(profile)}) '
                    "or shorten selected paths, then restart the exact source."
                ),
            )
            content = msgspec.to_builtins(result)
    return execution.OperationResult(content, "rejected", None)


def _page(
    request: contracts.StartupArtifactStart | contracts.StartupFileStart | contracts.StartupContinue,
    budget: int,
    source: contracts.StartupSource,
    text: str,
    start: int,
    end: int,
) -> dict[str, contracts.JsonValue]:
    continuation = (
        contracts.StartupContinue(request.project_root, request.work_root, source, end, budget)
        if end < len(text)
        else None
    )
    result = contracts.StartupTextPage(
        "pinboard-startup-text-page/v1",
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
    content: dict[str, contracts.JsonValue] = msgspec.to_builtins(result)
    return content


def read(
    raw: Mapping[str, contracts.JsonValue], profile: host_profiles.HostProfile, token: execution.CancellationToken
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.StartupTextEnvelope, strict=True).request
        msgspec.json.encode(request)
    except (msgspec.ValidationError, ValueError) as error:
        return _rejected("request", str(error), None, profile)
    budget = host_profiles.effective_budget(request.max_batch_bytes, profile)
    start = request.start_character if isinstance(request, contracts.StartupContinue) else 0
    expected: contracts.StartupSource | None
    match request:
        case contracts.StartupFileStart():
            expected = None
            path = Path(request.path)
        case contracts.StartupArtifactStart() | contracts.StartupContinue():
            expected = request.source
            match expected:
                case contracts.StartupArtifactSource():
                    verified = read_operations._verify_artifact(
                        request.project_root,
                        request.work_root,
                        expected.artifact_ref_id,
                        expected.selector,
                        expected.sha256,
                        expected.size_bytes,
                        token,
                    )
                    if verified.content.get("verified") is not True:
                        return _rejected(expected.selector, str(verified.content.get("message")), budget, profile)
                    path = Path(request.work_root) / expected.selector
                case contracts.StartupFileSource():
                    path = Path(expected.path)
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)
    if not path.is_absolute():
        return _rejected(str(path), "Select an explicit absolute resolved path.", budget, profile)
    # Recheck the exact presented bytes after accepted-reference verification; EOF confers no authority.
    selected = files.read(path)
    if isinstance(selected, files.TextUnavailable):
        return _rejected(selected.resource, selected.message, budget, profile)
    if expected is not None and (selected.sha256 != expected.sha256 or selected.size_bytes != expected.size_bytes):
        return _rejected(str(path), "Source bytes changed; continuation cannot combine identities.", budget, profile)
    if isinstance(request, contracts.StartupContinue) and start >= len(selected.text):
        return _rejected(str(path), "Continuation is outside this exact source.", budget, profile)
    source = (
        expected
        if expected is not None
        else contracts.StartupFileSource(str(path), selected.sha256, selected.size_bytes)
    )
    token.checkpoint()
    return _bounded_page(request, budget, profile, source, selected.text, start, token)


def _bounded_page(
    request: contracts.StartupArtifactStart | contracts.StartupFileStart | contracts.StartupContinue,
    budget: int,
    profile: host_profiles.HostProfile,
    source: contracts.StartupSource,
    text: str,
    start: int,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    representation = host_profiles.representation(profile)
    final = _page(request, budget, source, text, start, len(text))
    if envelope_size(final, representation) <= budget:
        return execution.OperationResult(final, "read", None)
    low, high = start, min(len(text) - 1, start + budget)
    while low < high:
        end = (low + high + 1) // 2
        candidate = _page(request, budget, source, text, start, end)
        if envelope_size(candidate, representation) <= budget:
            low = end
        else:
            high = end - 1
    if low == start:
        return _rejected(
            "max_batch_bytes", "The budget cannot fit source identity, continuation and one character.", budget, profile
        )
    token.checkpoint()
    return execution.OperationResult(_page(request, budget, source, text, start, low), "read", None)
