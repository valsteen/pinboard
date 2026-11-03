"""Strict native instruction references returned with legal actions."""

from typing import Annotated, Literal

import msgspec

from pinboard.application.dispatch_models import NativeInstruction
from pinboard.domain import decision_models, history


class ActionInstructions(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    caller_role: decision_models.Role
    runtime: Annotated[str, msgspec.Meta(pattern=r"\A(?:codex|claude-code|antigravity|unmeasured)\z")]
    route_facts: history.RouteFactsPayload | None
    procedure: NativeInstruction | None
    disposition: Literal["native-document", "protocol-client-no-native-runtime"]
