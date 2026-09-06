"""Installed read-only work-inspection presentation records."""

from typing import Literal

import msgspec

from pinboard.application import query_models


class AttemptView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    continuation: query_models.AttemptContinuation


class TransitionView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    action_id: str
    committed_revision: str
    continuation: query_models.AttemptContinuation | None


class ReviewJobView(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-review-job/v1"]
    attempt_id: str
    candidate_revision: str
    owner_task_id: str
    brief_path: str
    brief_sha256: str
    accepted_scope_revision: int
    accepted_scope_digest: str
    result_path: str
    result_sha256: str
    prompt: str
    return_contract: str


class StatusView(msgspec.Struct, frozen=True):
    stored_state_opened: bool = msgspec.field(name="valid")
    source_checkout_root: str
    shared_repository_root: str
    work_root: str
    revision: str
    active_attempts: tuple[str, ...]
    counts: dict[str, int]
    intake_item_count: int
    authority: str = "v2"


class ActionSemanticsView(msgspec.Struct, frozen=True):
    use_case: str
    effect: str
    permitted_roles: tuple[str, ...]
    subject_kind: str
    lifecycle_precondition: str
    practical_result: str


class InputContractView(msgspec.Struct, frozen=True):
    action_kind: str
    semantics: ActionSemanticsView
    payload_schema: msgspec.Raw | None


class ActionView(msgspec.Struct, frozen=True, omit_defaults=True):
    action_id: str
    kind: str
    subject: str
    label: str
    expected_revision: str
    subject_revision: str
    authorization: str
    lease_id: str
    generation: int
    semantics: ActionSemanticsView
    input_contract: InputContractView | None = None


class ActionsView(msgspec.Struct, frozen=True):
    actions: tuple[ActionView, ...]


class ParallelItemView(msgspec.Struct, frozen=True):
    item_id: str
    label: str
    state: str
    attempt_id: str | None
    outcome: str
    reasons: tuple[query_models.ParallelReason, ...]


class ParallelPreviewView(msgspec.Struct, frozen=True):
    schema: str
    revision: str
    selection: str
    safe: bool
    launchable: tuple[ParallelItemView, ...]
    excluded: tuple[ParallelItemView, ...]
