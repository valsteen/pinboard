"""Retained checkpoint v1 input; current acceptance publishes v2 only."""

from typing import Literal

import msgspec

from pinboard.application import work_brief_models


class CheckpointReviewPackage(
    msgspec.Struct,
    tag="pinboard-checkpoint-review-package/v1",
    tag_field="schema",
    frozen=True,
    forbid_unknown_fields=True,
):
    attempt_id: work_brief_models.KebabId
    item_id: work_brief_models.KebabId
    candidate: work_brief_models.NonEmptyLine
    acceptance_evidence: work_brief_models.NonEmptyLine
    accepted_scope: work_brief_models.AcceptedScope
    checkpoint: work_brief_models.CheckpointIdentity
    accepted_brief: work_brief_models.PortableArtifactIdentity
    result: work_brief_models.PortableArtifactIdentity
    implementation_review: work_brief_models.PortableArtifactIdentity
    verdict: Literal["ready"]
    review_basis: work_brief_models.ReviewBasis

    def __post_init__(self) -> None:
        identities = (self.accepted_brief, self.result, self.implementation_review)
        expected = (
            ("accepted-brief", "brief"),
            ("result", "result"),
            ("implementation-review", "evidence"),
        )
        if tuple((value.role, value.kind) for value in identities) != expected:
            raise ValueError("Checkpoint package artifact roles and kinds do not match their bindings.")
        work_brief_models.validate_checkpoint_review_basis(self.review_basis, self.checkpoint.sha256, identities)
