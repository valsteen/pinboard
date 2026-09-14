"""PreToolUse hook: auto-allow read-only pinboard CLI invocations.

Confirmed read-only against src/pinboard/interfaces/cli_parser.py: each listed
command either only reads SQLite state, reads a static schema, or (validate,
artifact verify) explicitly does not mutate anything. Everything else is left
to the normal permission flow.
"""

import json
import shlex
import sys

# Any read-only pinboard invocation is a single, unadorned command. Reject
# outright if the raw string carries shell chaining, substitution, or
# redirection, so a mutating command cannot ride along after a safe prefix.
DANGEROUS_SUBSTRINGS = ("&&", "||", ";", "|", chr(96), "$(", "\n", ">", "<")

SAFE_PREFIXES = (
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


def allow(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def pass_through():
    print("{}")


def has_prefix(tokens, prefix):
    return tuple(tokens[: len(prefix)]) == prefix


def main():
    launcher = sys.argv[1]

    try:
        data = json.load(sys.stdin)
    except Exception:
        pass_through()
        return

    if data.get("tool_name") != "Bash":
        pass_through()
        return

    command = data.get("tool_input", {}).get("command", "")
    if not isinstance(command, str) or not command.strip():
        pass_through()
        return

    if any(marker in command for marker in DANGEROUS_SUBSTRINGS):
        pass_through()
        return

    try:
        tokens = shlex.split(command)
    except ValueError:
        pass_through()
        return

    if not tokens or tokens[0] != launcher:
        pass_through()
        return

    rest = tokens[1:]

    if any(has_prefix(rest, prefix) for prefix in SAFE_PREFIXES):
        allow("Read-only pinboard inspection command")
        return

    # brief-sources only reads and plans unless it is told to write the plan
    # to disk via --output-plan.
    if has_prefix(rest, ("brief-sources",)) and "--output-plan" not in rest:
        allow("Read-only pinboard inspection command")
        return

    pass_through()


if __name__ == "__main__":
    main()
