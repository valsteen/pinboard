"""Verify selected startup bytes and present one bounded page; never grant authority or write project state.

A recognized host's page size is its per-user host setting, seeded with the measured default on first use.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

import msgspec

from pinboard import __version__
from pinboard.adapters.files import host_settings
from pinboard.adapters.files import startup_text as files
from pinboard.adapters.files.setting_resolution import (
    EntryProblem,
    InvalidEntry,
    NotRegularFile,
    SettingEffects,
    SettingFailureCause,
    SettingResolutionError,
)
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
    resource: str, message: str, budget: int | None, page_bytes: int, profile: host_profiles.HostProfile
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
                    f'Use max_batch_bytes "host-profile" (this host allows {page_bytes}) '
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


def _correction(path: Path, key: str, cause: SettingFailureCause) -> tuple[str, str]:
    """The setting key a repair must touch and the repair for invalid stored content.

    An invalid entry names its own key, which may belong to another host or be unknown or duplicated, so
    that the repair clears the failure; other failures report the connected host's key, the setting this read
    could not verify.
    """
    if not isinstance(cause, InvalidEntry):
        return key, (
            f"Set {key} in {path} to a whole number of bytes from {host_settings.MIN_PAGE_BYTES} to "
            f"{host_settings.MAX_PAGE_BYTES}, or remove that key so the measured default is written again, then retry."
        )
    match cause.problem:
        case EntryProblem.INVALID_VALUE:
            repair = (
                f"Set {cause.key} in {path} to a whole number of bytes from {host_settings.MIN_PAGE_BYTES} to "
                f"{host_settings.MAX_PAGE_BYTES}, or remove that key so its host's measured default is written "
                "again when that host next reads startup text, then retry."
            )
        case EntryProblem.DUPLICATE_KEY:
            repair = f"Keep one {cause.key} entry in {path} and delete the others, then retry."
        case EntryProblem.UNKNOWN_KEY:
            repair = (
                f"Remove {cause.key} from {path}, or rename it to host.<host>.startup-page-bytes for the host it "
                "was meant for, then retry."
            )
        case _ as unreachable:
            assert_never(unreachable)
    return cause.key, repair


def _sentence(text: str) -> str:
    return text if text.endswith(".") else f"{text}."


def _host_setting_failure(
    path: Path | None, key: str, error: SettingResolutionError | RuntimeError, directory_created: bool
) -> execution.OperationResult:
    """Name the setting, the failed resource, this call's settings effects, retry and one safe next step."""
    if isinstance(error, RuntimeError):
        effects = SettingEffects("none", "none", "none")
        resource = "home directory"
        recovery = "Start the Pinboard MCP service with HOME set to your home directory, then retry the same request."
        retry = "retry-same-input"
        message = f"Host page-size setting {key} is unavailable: {_sentence(str(error))} No page was read."
    else:
        effects = error.effects
        key, correction = _correction(error.path, key, error.cause)
        resource, recovery, retry = execution.settings_recovery(error, "Host page-size settings", correction)
        if isinstance(error.cause, NotRegularFile):
            subject = f"Host page-size settings file {error.path} is unusable"
        elif isinstance(error.cause, InvalidEntry):
            subject = f"Host page-size setting {key} is invalid"
        else:
            subject = f"Host page-size setting {key} is unavailable"
        message = f"{subject}: {_sentence(str(error))} No page was read."
    confirmed = directory_created or effects.file_creation == "confirmed" or effects.key_write == "acknowledged"
    unconfirmed = effects.file_creation == "unconfirmed" or effects.key_write == "unconfirmed"
    result = contracts.StartupHostSettingFailed(
        "pinboard-startup-text-result/v1",
        "rejected",
        "HOST_PAGE_SETTING_UNAVAILABLE",
        str(path) if path is not None else "~/.config/pinboard/hosts.config",
        key,
        resource,
        message,
        recovery,
        "confirmed" if directory_created else "none",
        effects.file_creation,
        effects.key_write,
        None if unconfirmed else confirmed,
        "unconfirmed" if unconfirmed else "committed" if confirmed else "unchanged",
        retry,
        ("user-settings",) if confirmed else (),
    )
    return execution.OperationResult(msgspec.to_builtins(result), "rejected", None)


def _page_bytes(profile: host_profiles.HostProfile) -> int | execution.OperationResult:
    """The unmeasured size, or the recognized host's stored per-user setting seeded with its measured default."""
    host = host_profiles.setting_host(profile)
    if host is None:
        return host_profiles.measured_page_bytes(profile)
    key = host_settings.setting_key(host)
    try:
        path = host_settings.settings_path()
    except RuntimeError as error:
        return _host_setting_failure(None, key, error, False)
    directory_created = False
    try:
        path = host_settings.real_location(path)
        directory_created = host_settings.ensure_directory(path)
        return host_settings.resolve(path, host, host_profiles.measured_page_bytes(profile)).value
    except SettingResolutionError as error:
        return _host_setting_failure(path, key, error, directory_created)


def read(
    raw: Mapping[str, contracts.JsonValue], profile: host_profiles.HostProfile, token: execution.CancellationToken
) -> execution.OperationResult:
    token.checkpoint()
    try:
        request = msgspec.convert(raw, type=contracts.StartupTextEnvelope, strict=True).request
        msgspec.json.encode(request)
    except (msgspec.ValidationError, ValueError) as error:
        return _rejected("request", str(error), None, host_profiles.measured_page_bytes(profile), profile)
    page_bytes = _page_bytes(profile)
    if isinstance(page_bytes, execution.OperationResult):
        return page_bytes
    budget = host_profiles.effective_budget(request.max_batch_bytes, page_bytes)
    return _read_selected(request, budget, page_bytes, profile, token)


def _read_selected(
    request: contracts.StartupArtifactStart | contracts.StartupFileStart | contracts.StartupContinue,
    budget: int,
    page_bytes: int,
    profile: host_profiles.HostProfile,
    token: execution.CancellationToken,
) -> execution.OperationResult:
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
                            expected.selector, str(verified.content.get("message")), budget, page_bytes, profile
                        )
                    path = Path(request.work_root) / expected.selector
                case contracts.StartupFileSource():
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
    if isinstance(request, contracts.StartupContinue) and start >= len(selected.text):
        return _rejected(str(path), "Continuation is outside this exact source.", budget, page_bytes, profile)
    source = (
        expected
        if expected is not None
        else contracts.StartupFileSource(str(path), selected.sha256, selected.size_bytes)
    )
    token.checkpoint()
    return _bounded_page(request, budget, page_bytes, profile, source, selected.text, start, token)


def _bounded_page(
    request: contracts.StartupArtifactStart | contracts.StartupFileStart | contracts.StartupContinue,
    budget: int,
    page_bytes: int,
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
            "max_batch_bytes",
            "The budget cannot fit source identity, continuation and one character.",
            budget,
            page_bytes,
            profile,
        )
    token.checkpoint()
    return execution.OperationResult(_page(request, budget, source, text, start, low), "read", None)
