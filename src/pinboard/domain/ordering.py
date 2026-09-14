"""Decide an exact replacement of the current live item order."""

from dataclasses import dataclass
from typing import Annotated, Literal

import msgspec

from pinboard.domain.errors import DecisionFailure, DecisionFailureCode, DecisionResult
from pinboard.domain.identifiers import ItemId

type OrderItemId = Annotated[ItemId, msgspec.Meta(pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z")]


class OrderRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-live-order/v1"]
    expected_order: tuple[OrderItemId, ...]
    requested_order: tuple[OrderItemId, ...]

    def __post_init__(self) -> None:
        for values in (self.expected_order, self.requested_order):
            if len(set(values)) != len(values):
                raise ValueError("Order entries must be unique.")


@dataclass(frozen=True, slots=True)
class OrderChange:
    before: tuple[ItemId, ...]
    after: tuple[ItemId, ...]

    @property
    def changed_positions(self) -> tuple[tuple[int, ItemId], ...]:
        return tuple(
            (position, item)
            for position, (before, item) in enumerate(zip(self.before, self.after, strict=True), 1)
            if before != item
        )


def decide_order(
    current: tuple[ItemId, ...], expected: tuple[ItemId, ...], requested: tuple[ItemId, ...]
) -> DecisionResult[OrderChange]:
    if current != expected:
        return DecisionFailure(
            DecisionFailureCode.ACTION_NOT_AVAILABLE,
            "The expected live order is stale; read the current overview before requesting another order.",
            None,
        )
    if len(requested) != len(current) or set(requested) != set(current):
        return DecisionFailure(
            DecisionFailureCode.TRANSITION_INPUT_INVALID,
            "The requested order must contain every current live item exactly once.",
            None,
        )
    return OrderChange(current, requested)
