"""Identify the connected MCP host and select its startup-text page profile.

The MCP initialize clientInfo is an accepted externally owned boundary: only its name and version are
decoded and validated, and every other field is ignored. A recognized host is identified by name for every
version, because Antigravity reports a constant version and the user owns each recognized host's page size in
the per-user host settings file. Each recognized profile names the measured default seeded into that file and
which result representation the host passes to the model. Claude Code and Codex defaults sit with margin under
the host's default delivery limit, so each page arrives whole. Antigravity saves any text block above about
4 KB to a step file, so its default instead makes each page spill into one file that a single native read_file
call reads whole, which takes fewer calls than pages small enough to stay inline. Any other client or a missing
clientInfo selects the unmeasured profile, which has no setting.
"""

from enum import Enum
from typing import Annotated, assert_never

import msgspec
from mcp.types import InitializeRequestParams

from pinboard.mcp import contracts


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
    """Select the profile for this client name; a valid clientInfo must also carry a nonempty version."""
    if client_params is None:
        return HostProfile.UNMEASURED
    try:
        client = msgspec.convert(client_params.client_info, ClientInfo, from_attributes=True)
    except msgspec.ValidationError:
        return HostProfile.UNMEASURED
    match client.name:
        case "claude-code":
            return HostProfile.CLAUDE_CODE
        case "codex-mcp-client":
            return HostProfile.CODEX
        case "antigravity-client":
            return HostProfile.ANTIGRAVITY
        case _:
            return HostProfile.UNMEASURED


def measured_page_bytes(profile: HostProfile) -> int:
    """The measured default page size: the seed for a recognized host's setting, or the unmeasured budget."""
    # Measured on Claude Code 2.1.293, Codex 0.164.0 and agy 1.4.8 (antigravity-client) at their defaults.
    match profile:
        case HostProfile.CLAUDE_CODE:
            # MAX_MCP_OUTPUT_TOKENS (default 25000) moves larger results to a file; 32 KB pages arrived whole.
            return 32000
        case HostProfile.CODEX:
            # tool_output_token_limit (default 10000 tokens) elides the middle of larger results.
            return 20000
        case HostProfile.ANTIGRAVITY:
            # Text blocks above about 4 KB (4,080 bytes shown inline, 4,110 saved) go to a step file instead of
            # being shown inline. A page carrying both representations has a text block under half its budget,
            # so 92000 keeps every spilled page within one 46,080-byte native read_file call.
            return 92000
        case HostProfile.UNMEASURED:
            return 8000
        case _ as unreachable:
            assert_never(unreachable)


def setting_host(profile: HostProfile) -> str | None:
    """The host name keying this profile's per-user page size; the unmeasured profile has no setting."""
    match profile:
        case HostProfile.CLAUDE_CODE:
            return "claude-code"
        case HostProfile.CODEX:
            return "codex"
        case HostProfile.ANTIGRAVITY:
            return "antigravity"
        case HostProfile.UNMEASURED:
            return None
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


def effective_budget(requested: contracts.StartupBudget, page_bytes: int) -> int:
    """An integer request is a ceiling; the selected profile's page size bounds every request."""
    match requested:
        case "host-profile":
            return page_bytes
        case int():
            return min(requested, page_bytes)
        case _ as unreachable:
            assert_never(unreachable)
