"""Retained patch-only checkpoint v2 input; current acceptance publishes v3."""

from typing import Literal

import msgspec

from pinboard.application import work_brief_models


class CheckpointReviewPackageV2(
    msgspec.Struct,
    tag="pinboard-checkpoint-review-package/v2",
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
    candidate_snapshot: work_brief_models.PortableArtifactIdentity
    accepted_brief: work_brief_models.PortableArtifactIdentity
    result: work_brief_models.PortableArtifactIdentity
    implementation_review: work_brief_models.PortableArtifactIdentity
    verdict: Literal["ready"]
    review_basis: work_brief_models.ReviewBasis

    def __post_init__(self) -> None:
        identities = work_brief_models.validate_checkpoint_artifact_roles(
            self.candidate_snapshot,
            self.accepted_brief,
            self.result,
            self.implementation_review,
        )
        if self.candidate.startswith("working-tree-sha256:"):
            if self.candidate != f"working-tree-sha256:{self.candidate_snapshot.content_sha256}":
                raise ValueError("Working-tree checkpoint candidate must match its portable snapshot digest.")
        elif work_brief_models.GIT_COMMIT_REVISION.fullmatch(self.candidate) is None:
            raise ValueError("Checkpoint candidate must be a working-tree digest or full Git commit revision.")
        work_brief_models.validate_checkpoint_review_basis(self.review_basis, self.checkpoint.sha256, identities)
