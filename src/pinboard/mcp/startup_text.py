"""Verify selected startup bytes and present one bounded page; never grant authority or write state."""

from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard import __version__
from pinboard.adapters.files import startup_text as files
from pinboard.mcp import contracts, execution, read_operations


def envelope_size(content: dict[str, contracts.JsonValue]) -> int:
    """Price the compact SDK tool result, including its duplicated JSON companion."""
    return len(
        msgspec.json.encode(
            {
                "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "pinboard", "version": __version__}},
                "content": [{"type": "text", "text": msgspec.json.encode(content).decode()}],
                "structuredContent": content,
                "isError": False,
                "resultType": "complete",
            }
        )
    )


def _rejected(resource: str, message: str, budget: int | None) -> execution.OperationResult:
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
    if budget is not None and envelope_size(content) > budget:
        result = msgspec.structs.replace(result, message="Selected source or continuation is invalid.")
        content = msgspec.to_builtins(result)
        if envelope_size(content) > budget:
            result = msgspec.structs.replace(
                result,
                resource="max_batch_bytes",
                message="The budget cannot fit source metadata or its diagnosis.",
                recovery="Raise max_batch_bytes up to 8000 or shorten selected paths, then restart the exact source.",
            )
            content = msgspec.to_builtins(result)
    return execution.OperationResult(content, "rejected", None)


def _page(
    request: contracts.StartupArtifactStart | contracts.StartupFileStart | contracts.StartupContinue,
    source: contracts.StartupSource,
    text: str,
    start: int,
    end: int,
) -> dict[str, contracts.JsonValue]:
    continuation = (
        contracts.StartupContinue(request.project_root, request.work_root, source, end, request.max_batch_bytes)
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
        request.max_batch_bytes,
        continuation,
        "delivery-only",
        False,
        "unchanged",
        "safe-to-repeat",
        (),
    )
    content: dict[str, contracts.JsonValue] = msgspec.to_builtins(result)
    return content


def read(raw: Mapping[str, contracts.JsonValue], token: execution.CancellationToken) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.StartupTextEnvelope, strict=True).request
        msgspec.json.encode(request)
    except (msgspec.ValidationError, ValueError, UnicodeError) as error:
        return _rejected("request", str(error), None)
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
                        return _rejected(
                            expected.selector, str(verified.content.get("message")), request.max_batch_bytes
                        )
                    path = Path(request.work_root) / expected.selector
                case contracts.StartupFileSource():
                    path = Path(expected.path)
                case _ as unreachable:
                    assert_never(unreachable)
        case _ as unreachable:
            assert_never(unreachable)
    if not path.is_absolute():
        return _rejected(str(path), "Select an explicit absolute resolved path.", request.max_batch_bytes)
    # Recheck the exact presented bytes after accepted-reference verification; EOF confers no authority.
    selected = files.read(path)
    if isinstance(selected, files.TextUnavailable):
        return _rejected(selected.resource, selected.message, request.max_batch_bytes)
    if expected is not None and (selected.sha256 != expected.sha256 or selected.size_bytes != expected.size_bytes):
        return _rejected(
            str(path), "Source bytes changed; continuation cannot combine identities.", request.max_batch_bytes
        )
    if isinstance(request, contracts.StartupContinue) and start >= len(selected.text):
        return _rejected(str(path), "Continuation is outside this exact source.", request.max_batch_bytes)
    source = (
        expected
        if expected is not None
        else contracts.StartupFileSource(str(path), selected.sha256, selected.size_bytes)
    )
    token.checkpoint()
    return _bounded_page(request, source, selected.text, start, token)


def _bounded_page(
    request: contracts.StartupArtifactStart | contracts.StartupFileStart | contracts.StartupContinue,
    source: contracts.StartupSource,
    text: str,
    start: int,
    token: execution.CancellationToken,
) -> execution.OperationResult:
    final = _page(request, source, text, start, len(text))
    if envelope_size(final) <= request.max_batch_bytes:
        return execution.OperationResult(final, "read", None)
    low, high = start, min(len(text) - 1, start + request.max_batch_bytes)
    while low < high:
        end = (low + high + 1) // 2
        candidate = _page(request, source, text, start, end)
        if envelope_size(candidate) <= request.max_batch_bytes:
            low = end
        else:
            high = end - 1
    if low == start:
        return _rejected(
            "max_batch_bytes",
            "The budget cannot fit source identity, continuation and one character.",
            request.max_batch_bytes,
        )
    token.checkpoint()
    return execution.OperationResult(_page(request, source, text, start, low), "read", None)
