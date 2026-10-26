"""Identify the connected MCP host and select its measured startup-text page profile.

The MCP initialize clientInfo is an accepted externally owned boundary: only its name and version are
decoded and validated, and every other field is ignored. Each measured profile names the page budget its
host delivers to the model whole, with margin under the host's default limit, and which result
representation that host passes to the model. A measurement applies only to the client version range it
was taken on; any other client, version or missing clientInfo selects the unmeasured profile.
"""

import re
from enum import Enum
from typing import Annotated, assert_never

import msgspec
from mcp.types import InitializeRequestParams

from pinboard.mcp import contracts

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class HostProfile(Enum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    UNMEASURED = "unmeasured"


class Representation(Enum):
    """Which tool-result representations a startup page carries."""

    STRUCTURED_AND_TEXT = "structured-and-text"
    STRUCTURED_ONLY = "structured-only"


class ClientInfo(msgspec.Struct, frozen=True):
    name: Annotated[str, msgspec.Meta(min_length=1)]
    version: Annotated[str, msgspec.Meta(min_length=1)]


def select(client_params: InitializeRequestParams | None) -> HostProfile:
    """Select the profile measured for this exact client name and version range."""
    if client_params is None:
        return HostProfile.UNMEASURED
    try:
        client = msgspec.convert(client_params.client_info, ClientInfo, from_attributes=True)
    except msgspec.ValidationError:
        return HostProfile.UNMEASURED
    matched = _VERSION.fullmatch(client.version)
    if matched is None:
        return HostProfile.UNMEASURED
    version = (int(matched[1]), int(matched[2]), int(matched[3]))
    # Measured on Claude Code 2.1.293, Codex 0.164.0 and agy 1.4.7, which reports antigravity-client v1.0.0.
    match client.name:
        case "claude-code" if (2, 1, 293) <= version < (2, 2, 0):
            return HostProfile.CLAUDE_CODE
        case "codex-mcp-client" if (0, 164, 0) <= version < (0, 165, 0):
            return HostProfile.CODEX
        case "antigravity-client" if version == (1, 0, 0):
            return HostProfile.ANTIGRAVITY
        case _:
            return HostProfile.UNMEASURED


def page_bytes(profile: HostProfile) -> int:
    """Largest priced startup page for the profile, sized under the host's default delivery limit."""
    match profile:
        case HostProfile.CLAUDE_CODE:
            # MAX_MCP_OUTPUT_TOKENS (default 25000) moves larger results to a file; 32 KB pages arrived whole.
            return 32000
        case HostProfile.CODEX:
            # tool_output_token_limit (default 10000 tokens) elides the middle of larger results.
            return 20000
        case HostProfile.ANTIGRAVITY:
            # Text blocks above about 10 KB are saved to a file instead of being shown inline.
            return 16000
        case HostProfile.UNMEASURED:
            return 8000
        case _ as unreachable:
            assert_never(unreachable)


def representation(profile: HostProfile) -> Representation:
    """Claude Code shows the model only structuredContent, so its pages omit the text companion.

    Antigravity shows only the text block, and Codex scripts receive the whole result; an advertised
    output schema requires structuredContent, so those pages keep both forms.
    """
    match profile:
        case HostProfile.CLAUDE_CODE:
            return Representation.STRUCTURED_ONLY
        case HostProfile.CODEX | HostProfile.ANTIGRAVITY | HostProfile.UNMEASURED:
            return Representation.STRUCTURED_AND_TEXT
        case _ as unreachable:
            assert_never(unreachable)


def effective_budget(requested: contracts.StartupBudget, profile: HostProfile) -> int:
    """An integer request is a ceiling; the profile's page size bounds every request."""
    match requested:
        case "host-profile":
            return page_bytes(profile)
        case int():
            return min(requested, page_bytes(profile))
        case _ as unreachable:
            assert_never(unreachable)
