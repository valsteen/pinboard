"""Retained patch-only v1 working-tree snapshots with known preimages."""

import hashlib
from typing import Annotated, Literal

import msgspec

type NonEmptyLine = Annotated[str, msgspec.Meta(min_length=1, pattern=r"\A[^\r\n]+\z")]


class WorkingTreeCandidateSnapshot(
    msgspec.Struct, tag="working-tree", tag_field="candidate_kind", frozen=True, forbid_unknown_fields=True
):
    schema: Literal["pinboard-candidate-snapshot/v1"]
    attempt_id: NonEmptyLine
    item_id: NonEmptyLine
    candidate: Annotated[str, msgspec.Meta(pattern=r"\Aworking-tree-sha256:[0-9a-f]{64}\z")]
    branch: NonEmptyLine
    preimage_revision: NonEmptyLine
    accepted_base_revision: NonEmptyLine
    recorded_at: NonEmptyLine
    diff: bytes

    def __post_init__(self) -> None:
        if self.candidate != f"working-tree-sha256:{hashlib.sha256(self.diff).hexdigest()}":
            raise ValueError("working-tree candidate identity must match the binary diff")
