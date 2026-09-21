"""Claude-native hook entries: startup attribution and own-tool approval; no project reads, authority or lifecycle effects."""

import os
import socket
import sys
from typing import Annotated, Literal

import msgspec

type NativeText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\x00]+\z")]
type NativeIdentity = Annotated[
    str,
    msgspec.Meta(min_length=1, pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z"),
]
PINBOARD_SERVER = "mcp__plugin_pinboard_pinboard"
PINBOARD_TOOL_PREFIX = f"{PINBOARD_SERVER}__"
type PinboardToolName = Annotated[str, msgspec.Meta(pattern=rf"\A{PINBOARD_TOOL_PREFIX}[^\x00]+\z")]
type PermissionDecision = Literal["allow"]


class SubagentStart(msgspec.Struct, frozen=True):
    """Project Claude-owned input onto required fields; ignore unrelated host extensions."""

    agent_id: NativeIdentity
    agent_type: NativeText
    cwd: NativeText
    hook_event_name: Literal["SubagentStart"]
    session_id: NativeText
    transcript_path: NativeText


class SessionStart(msgspec.Struct, frozen=True):
    """Project current Claude-owned parent input; ignore unrelated host extensions."""

    cwd: NativeText
    hook_event_name: Literal["SessionStart"]
    session_id: NativeIdentity
    transcript_path: NativeText


class PreToolUse(msgspec.Struct, frozen=True):
    """Project a Claude-owned PreToolUse event for one Pinboard tool; ignore unrelated host extensions."""

    cwd: NativeText
    hook_event_name: Literal["PreToolUse"]
    session_id: NativeText
    tool_name: PinboardToolName


class StartupContext(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    hook_event_name: Literal["SubagentStart", "SessionStart"] = msgspec.field(name="hookEventName")
    additional_context: str = msgspec.field(name="additionalContext")


class HookOutput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    hook_specific_output: StartupContext = msgspec.field(name="hookSpecificOutput")


class PermissionContext(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    hook_event_name: Literal["PreToolUse"] = msgspec.field(name="hookEventName")
    permission_decision: PermissionDecision = msgspec.field(name="permissionDecision")
    permission_decision_reason: str = msgspec.field(name="permissionDecisionReason")


class PermissionHookOutput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    hook_specific_output: PermissionContext = msgspec.field(name="hookSpecificOutput")


def read_hook_event[T: (SubagentStart, SessionStart, PreToolUse)](event_type: type[T]) -> T | None:
    """Validate argument-free host stdin; rejected payloads never reach diagnostics."""
    if len(sys.argv) != 1:
        print("Pinboard Claude hook accepts no arguments.", file=sys.stderr)
        return None
    try:
        return msgspec.json.decode(sys.stdin.buffer.read(), type=event_type)
    except msgspec.DecodeError:
        print("Pinboard Claude hook input unavailable: invalid native hook event.", file=sys.stderr)
        return None


def main() -> int:
    """Read one host event from stdin and emit only its native worker identity context."""
    if (event := read_hook_event(SubagentStart)) is None:
        return 1
    identity = msgspec.json.encode(event.agent_id).decode()
    context = (
        f"Pinboard native worker task_id: {identity}. "
        "Use this own post-launch identity for worker startup after verifying the launch prompt and canonical brief. "
        "It is attribution, not authority or an authenticated credential; acquire your own exact attempt lease. "
        "Do not substitute a parent/session identity or enumerate environment variables."
    )
    output = HookOutput(StartupContext("SubagentStart", context))
    print(msgspec.json.encode(output).decode())
    return 0


def session_start_main() -> int:
    """Read the current parent event and machine name; emit attribution, never authority."""
    if (event := read_hook_event(SessionStart)) is None:
        return 1
    try:
        hostname = socket.gethostname()
    except OSError:
        print("Pinboard Claude startup identity unavailable: machine name unavailable.", file=sys.stderr)
        return 1
    identity = msgspec.json.encode(event.session_id).decode()
    host = msgspec.json.encode(hostname).decode()
    context = (
        f"Pinboard native parent task_id: {identity}; host_id: {host}. "
        "Use these current local values for parent attribution, not a Remote Control link or model-derived host. "
        "They are attribution, not authority or authenticated credentials, and grant no permissions or lease. "
        "A launched worker instead uses its own SubagentStart agent_id and verified dispatch host."
    )
    if os.environ.get("PINBOARD_RUNTIME_PREPARED_NOW") == "1":
        context += (
            " Pinboard prepared this plugin version's private runtime during this session start, after the pinboard "
            "MCP server had already failed to connect; tell the user to reconnect it with /mcp or restart Claude Code "
            "before using Pinboard tools."
        )
    output = HookOutput(StartupContext("SessionStart", context))
    print(msgspec.json.encode(output).decode())
    return 0


def pre_tool_use_main() -> int:
    """Approve one Pinboard MCP tool call; Claude Code still evaluates its own deny and ask rules."""
    if read_hook_event(PreToolUse) is None:
        return 1
    reason = "Pinboard approves its own MCP tools; Claude Code still enforces saved deny and ask rules."
    output = PermissionHookOutput(PermissionContext("PreToolUse", "allow", reason))
    print(msgspec.json.encode(output).decode())
    return 0
