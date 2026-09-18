"""Claude-native startup context only; no project reads, authority or lifecycle effects."""

import sys
from typing import Annotated, Literal

import msgspec

type NativeText = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\x00]+\z")]
type NativeAgentId = Annotated[
    str,
    msgspec.Meta(min_length=1, pattern=r"\A(?!\s)(?!\.{1,2}\z)[^/\r\n\x00]*[^\s/\r\n\x00]\z"),
]


class SubagentStart(msgspec.Struct, frozen=True):
    """Project Claude-owned input onto required fields; ignore unrelated host extensions."""

    agent_id: NativeAgentId
    agent_type: NativeText
    cwd: NativeText
    hook_event_name: Literal["SubagentStart"]
    session_id: NativeText
    transcript_path: NativeText


class StartupContext(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    hook_event_name: Literal["SubagentStart"] = msgspec.field(name="hookEventName")
    additional_context: str = msgspec.field(name="additionalContext")


class HookOutput(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    hook_specific_output: StartupContext = msgspec.field(name="hookSpecificOutput")


def main() -> int:
    """Read one host event from stdin and emit only its native worker identity context."""
    if len(sys.argv) != 1:
        print("Pinboard Claude startup hook accepts no arguments.", file=sys.stderr)
        return 1
    try:
        event = msgspec.json.decode(sys.stdin.buffer.read(), type=SubagentStart)
    except msgspec.DecodeError:
        print("Pinboard Claude startup identity unavailable: invalid SubagentStart event.", file=sys.stderr)
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
