"""CLI compatibility exports for application-owned transition decoding."""

from pinboard.application.transition_input import (
    INPUT_CONTRACT_ACTION_KINDS,
    ParsedTransitionInput,
    TransitionInputFailure,
    TransitionInputResult,
    encoded_transition_input_schema,
    parse_item_revision_input,
    parse_transition_input,
)

__all__ = (
    "INPUT_CONTRACT_ACTION_KINDS",
    "ParsedTransitionInput",
    "TransitionInputFailure",
    "TransitionInputResult",
    "encoded_transition_input_schema",
    "parse_item_revision_input",
    "parse_transition_input",
)
