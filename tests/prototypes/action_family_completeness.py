"""Probe local completeness diagnostics for a deferred action-family design.

This module is excluded from the installed package. It tests whether one real
owner can make a missing closed-family decision cheap to recover without
creating a registry that owns decisions from other layers.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import NewType

ActionName = NewType("ActionName", str)


@dataclass(frozen=True, slots=True)
class ProjectionOwner:
    decision: str
    production_path: str
    production_symbol: str
    declaration_path: str
    declaration_symbol: str
    allowed_values: tuple[str, ...]
    accepted_facts: str


def require_dispositions(
    owner: ProjectionOwner,
    supported_actions: tuple[ActionName, ...],
    dispositions: Mapping[ActionName, str],
    rerun: str,
) -> None:
    missing = tuple(action for action in supported_actions if action not in dispositions)
    unknown = tuple(action for action in dispositions if action not in supported_actions)
    invalid = tuple((action, value) for action, value in dispositions.items() if value not in owner.allowed_values)
    if not missing and not unknown and not invalid:
        return

    problem_lines = [
        *(f"missing action '{action}'" for action in missing),
        *(f"unknown action '{action}'" for action in unknown),
        *(f"invalid action '{action}' value '{value}'" for action, value in invalid),
    ]
    raise AssertionError(
        "action-family disposition check failed\n"
        f"decision: {owner.decision}\n"
        f"production owner: {owner.production_path}:{owner.production_symbol}\n"
        f"prototype declaration: {owner.declaration_path}:{owner.declaration_symbol}\n"
        f"problem: {'; '.join(problem_lines)}\n"
        f"allowed values: {' | '.join(owner.allowed_values)}\n"
        f"accepted facts: {owner.accepted_facts}\n"
        f"next edit: add an explicit entry for every missing action to {owner.declaration_symbol}\n"
        f"next check: {rerun}"
    )
