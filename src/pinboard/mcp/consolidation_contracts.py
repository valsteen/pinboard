"""Strict native consolidation leaves and their correlated presentation contracts."""

from typing import Literal

import msgspec

from pinboard.application import consolidation_models as models
from pinboard.domain.errors import DecisionFailureCode
from pinboard.domain.history import RecordText
from pinboard.mcp import contracts


class PreviewRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="preview", tag_field="operation"):
    project_root: contracts.RootPath
    work_root: contracts.RootPath
    selection: models.Selection
    actor_task_id: contracts.RuntimeIdentity


class ConfirmRequest(msgspec.Struct, frozen=True, forbid_unknown_fields=True, tag="confirm", tag_field="operation"):
    project_root: contracts.RootPath
    work_root: contracts.RootPath
    selection: models.Selection
    binding: models.Binding
    human_confirmation: RecordText
    actor_task_id: contracts.RuntimeIdentity
    actor_host_id: contracts.RuntimeIdentity

    def __post_init__(self) -> None:
        if not self.human_confirmation.strip():
            raise ValueError("human_confirmation must contain the human's separate explicit confirmation")


class Envelope(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    request: PreviewRequest | ConfirmRequest


class PreviewResult(contracts._UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-consolidation-preview-result/v1"]
    status: Literal["ok"]
    preview: models.Preview
    binding: models.Binding
    continuation: str
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["safe-to-repeat"]
    changed_surfaces: contracts.Empty


class Rejected(contracts._UnchangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-consolidation-result/v1"]
    status: Literal["rejected"]
    code: DecisionFailureCode
    message: str
    recovery: str
    state_changed: bool
    effect: Literal["unchanged"]
    retry: Literal["correct-input", "refresh-action"]
    changed_surfaces: contracts.Empty


class Committed(contracts._ChangedResult, msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: Literal["pinboard-consolidation-result/v1"]
    status: Literal["committed", "committed-with-warning"]
    campaign_id: str
    superseded_sources: tuple[str, ...]
    committed_revision: int
    history_id: int
    state_changed: bool
    effect: Literal["committed"]
    retry: Literal["do-not-retry"]
    changed_surfaces: contracts.LedgerSurface
    warning: contracts.WarningResult | None


RESULT_TYPES = (PreviewResult, Rejected, Committed, contracts.ExecutorBusyResult)
