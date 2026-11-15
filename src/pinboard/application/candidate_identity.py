"""Raw candidate identity, its submission rule and exact patch comparison across Git object abbreviations."""

import hashlib
import io
import re
from dataclasses import dataclass
from enum import Enum
from itertools import zip_longest
from typing import assert_never

_INDEX_HEADER = re.compile(rb"index ([0-9a-f]{4,64})\.\.([0-9a-f]{4,64})( [0-7]{6})?\n")


def working_tree_identity(preimage_revision: str, diff: bytes) -> str:
    digest = hashlib.sha256(preimage_revision.encode() + b"\0" + diff).hexdigest()
    return f"working-tree-state-sha256:{digest}"


class CommitStep(Enum):
    """The commit route that replaces a working-tree identity observed past the accepted base."""

    SUBMIT_HEAD_COMMIT = "submit-head-commit"
    COMMIT_TRACKED_CHANGES = "commit-tracked-changes"


@dataclass(frozen=True, slots=True)
class WorkingTreeSubmittable:
    """The working tree sits on the accepted base, so its diff starts there."""


@dataclass(frozen=True, slots=True)
class WorkingTreeCommitRequired:
    """HEAD differs from the accepted base, so a working-tree diff covers none of the commits between them."""

    head_revision: str
    accepted_base_revision: str
    step: CommitStep


type WorkingTreeSubmission = WorkingTreeSubmittable | WorkingTreeCommitRequired


def working_tree_submission(accepted_base_revision: str, preimage_revision: str, diff: bytes) -> WorkingTreeSubmission:
    """Accept a working-tree identity only at the accepted base; otherwise name the commit to submit."""

    if preimage_revision == accepted_base_revision:
        return WorkingTreeSubmittable()
    step = CommitStep.COMMIT_TRACKED_CHANGES if diff else CommitStep.SUBMIT_HEAD_COMMIT
    return WorkingTreeCommitRequired(preimage_revision, accepted_base_revision, step)


def commit_required_message(required: WorkingTreeCommitRequired) -> str:
    """State why the working-tree identity is refused and the exact commit step that replaces it."""

    reason = (
        f"This working-tree identity cannot be submitted for review: HEAD {required.head_revision} differs from "
        f"the accepted base {required.accepted_base_revision}, so a working-tree diff covers none of the commits "
        "between them."
    )
    match required.step:
        case CommitStep.SUBMIT_HEAD_COMMIT:
            step = (
                f"The tracked diff is empty: submit HEAD {required.head_revision} as a commit candidate, declaring "
                "any Git-visible untracked paths it omits."
            )
        case CommitStep.COMMIT_TRACKED_CHANGES:
            step = (
                "Commit the tracked changes on the attempt branch, committing or declaring any Git-visible "
                "untracked paths so the new HEAD is clean, call pinboard_candidate_observe again and submit the "
                "new clean HEAD commit."
            )
        case _ as unreachable:
            assert_never(unreachable)
    return f"{reason} {step} A commit candidate records its diff from the accepted base."


def candidate_diffs_match(accepted: bytes, observed: bytes) -> bool:
    """Compare original patches, allowing only compatible index-header object prefixes.

    This does not produce canonical bytes or an identity: accepted evidence always
    retains its original bytes. File headers, modes, hunks and binary payload stay exact.
    """
    if accepted == observed:
        return True
    file_header = False
    for original, current in zip_longest(io.BytesIO(accepted), io.BytesIO(observed)):
        if original is None or current is None:
            return False
        if original == current:
            if original.startswith(b"diff --git "):
                file_header = True
            elif original.startswith((b"index ", b"--- ", b"GIT binary patch", b"Binary files ")):
                file_header = False
            continue
        if not file_header:
            return False
        original_index = _INDEX_HEADER.fullmatch(original)
        current_index = _INDEX_HEADER.fullmatch(current)
        if original_index is None or current_index is None or original_index[3] != current_index[3]:
            return False
        for original_id, current_id in zip(original_index.group(1, 2), current_index.group(1, 2), strict=True):
            if not (original_id.startswith(current_id) or current_id.startswith(original_id)):
                return False
        file_header = False
    return True
