"""Antigravity-native hook entries: startup attribution and own-tool approval; no project reads, authority or lifecycle effects."""

import os
import socket
import sys
from typing import Annotated, Literal

import msgspec

from pinboard.hook_values import NativeIdentity, NativeText

# Antigravity routes every MCP call through call_mcp_tool and names a plugin server <plugin directory>_<server>.
PINBOARD_SERVER = "pinboard_pinboard"
CLOSE_TOOL = "pinboard_close"
type PermissionDecision = Literal["allow", "ask", "force_ask"]


class SessionStart(msgspec.Struct, frozen=True, rename="camel"):
    """Project Antigravity-owned SessionStart input onto required fields; ignore unrelated host extensions."""

    conversation_id: NativeIdentity
    workspace_paths: tuple[NativeText, ...]
    transcript_path: NativeText
    artifact_directory_path: NativeText
    model_name: NativeText


class McpToolArguments(msgspec.Struct, frozen=True, rename="pascal"):
    """Project the server and tool named by one call_mcp_tool step; its forwarded arguments stay unread."""

    server_name: NativeText
    tool_name: NativeText


class McpToolCall(msgspec.Struct, frozen=True):
    name: Literal["call_mcp_tool"]
    args: McpToolArguments


class PreToolUse(msgspec.Struct, frozen=True, rename="camel"):
    """Project an Antigravity-owned PreToolUse event onto the MCP call it decides; ignore every other field."""

    tool_call: McpToolCall


class SystemMessage(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    system_message: str = msgspec.field(name="systemMessage")


class InjectedStep(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    system_message: SystemMessage = msgspec.field(name="systemMessage")


class StartupOutput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    inject_steps: Annotated[tuple[InjectedStep, ...], msgspec.Meta(min_length=1, max_length=1)] = msgspec.field(
        name="injectSteps"
    )


class PermissionOutput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    decision: PermissionDecision
    reason: str


def read_hook_event[T: (SessionStart, PreToolUse)](event_type: type[T]) -> T | None:
    """Validate argument-free host stdin; rejected payloads never reach diagnostics."""
    if len(sys.argv) != 1:
        print("Pinboard Antigravity hook accepts no arguments.", file=sys.stderr)
        return None
    try:
        return msgspec.json.decode(sys.stdin.buffer.read(), type=event_type)
    except msgspec.DecodeError:
        print("Pinboard Antigravity hook input unavailable: invalid native hook event.", file=sys.stderr)
        return None


def session_start_main() -> int:
    """Read one conversation start and the machine name; emit attribution and skill-loading context, never authority."""
    if (event := read_hook_event(SessionStart)) is None:
        return 1
    try:
        hostname = socket.gethostname()
    except OSError:
        print("Pinboard Antigravity startup identity unavailable: machine name unavailable.", file=sys.stderr)
        return 1
    identity = msgspec.json.encode(event.conversation_id).decode()
    host = msgspec.json.encode(hostname).decode()
    context = (
        f"Pinboard native task_id: {identity}; host_id: {host}. "
        "A parent session uses both values for attribution. A worker or reviewer launched through invoke_subagent "
        "uses this task_id as its own post-launch identity together with the host named in its verified dispatch "
        "prompt, not this host_id. They are attribution, not authority or authenticated credentials, and grant no "
        "permissions or lease. Do not substitute another conversation's identity or enumerate environment variables. "
        "Antigravity injects only the first 24000 bytes of a skill body, so before following the main pinboard skill "
        "or any reference, read the complete file through pinboard_startup_text file-start paging with its actual "
        'resolved absolute path and max_batch_bytes "host-profile", following each continuation until it is null. '
        "Antigravity procedures for Pinboard are in the pinboard skill's references/runtime-antigravity.md."
    )
    if os.environ.get("PINBOARD_RUNTIME_PREPARED_NOW") == "1":
        context += (
            " Pinboard prepared this plugin version's private runtime during this session start. MCP startup can "
            "also prepare and start the server automatically. Check whether Pinboard tools are available; if the "
            "connection is unavailable, tell the user to start a new agy session so Antigravity reconnects the "
            "plugin's MCP server."
        )
    output = StartupOutput((InjectedStep(SystemMessage(context)),))
    print(msgspec.json.encode(output).decode())
    return 0


def pre_tool_use_main() -> int:
    """Decide one MCP call: force a human prompt for pinboard_close and allow other Pinboard tools."""
    if (event := read_hook_event(PreToolUse)) is None:
        return 1
    call = event.tool_call.args
    if call.server_name != PINBOARD_SERVER:
        output = PermissionOutput("ask", "Not a Pinboard tool; Antigravity's own approval settings decide.")
    elif call.tool_name == CLOSE_TOOL:
        output = PermissionOutput("force_ask", "Closing a Pinboard item always needs the human's confirmation.")
    else:
        output = PermissionOutput("allow", "Pinboard approves its own MCP tools.")
    print(msgspec.json.encode(output).decode())
    return 0
