"""Auto-allow read-only Pinboard CLI invocations in Claude Code."""

import json
import shlex
import sys
from typing import Final

DANGEROUS_SUBSTRINGS: Final = ("&", "||", ";", "|", "`", "$(", "\n", ">", "<")
ROOT_OPTIONS: Final = frozenset({"--project-root", "--work-root"})

SAFE_ROUTES: Final = (
    ("overview",),
    ("status",),
    ("root",),
    ("validate",),
    ("actions",),
    ("input-contract",),
    ("tool-contract",),
    ("attempt", "status"),
    ("attempt", "inspect"),
    ("preparation", "status"),
    ("item", "status"),
    ("item", "definition"),
    ("item", "definition-history"),
    ("parallel", "preview"),
    ("brief", "review-status"),
    ("artifact", "verify"),
)


def _allow() -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "Read-only Pinboard inspection command",
                }
            }
        )
    )


def _read_command() -> str | None:
    try:
        data: object = json.load(sys.stdin)
    except json.JSONDecodeError, OSError:
        return None

    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) and command.strip() else None


def _pinboard_arguments(tokens: list[str], launcher: str) -> list[str] | None:
    if tokens and tokens[0] == "PINBOARD_RUNTIME=claude":
        tokens = tokens[1:]
    if not tokens or tokens[0] != launcher:
        return None

    arguments = tokens[1:]
    while arguments and arguments[0] in ROOT_OPTIONS:
        if len(arguments) < 2:
            return None
        arguments = arguments[2:]
    return arguments


def _matches(arguments: list[str], route: tuple[str, ...]) -> bool:
    return tuple(arguments[: len(route)]) == route


def _selects_output_plan(argument: str) -> bool:
    option = argument.partition("=")[0]
    return len(option) > 2 and "--output-plan".startswith(option)


def _is_read_only(arguments: list[str]) -> bool:
    if any(_matches(arguments, route) for route in SAFE_ROUTES):
        return True
    return _matches(arguments, ("brief-sources",)) and not any(
        _selects_output_plan(argument) for argument in arguments[1:]
    )


def main() -> None:
    if len(sys.argv) != 2 or (command := _read_command()) is None:
        return
    if any(marker in command for marker in DANGEROUS_SUBSTRINGS):
        return

    try:
        tokens = shlex.split(command)
    except ValueError:
        return

    arguments = _pinboard_arguments(tokens, sys.argv[1])
    if arguments is not None and _is_read_only(arguments):
        _allow()


if __name__ == "__main__":
    main()
