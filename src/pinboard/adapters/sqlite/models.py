from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import msgspec

from pinboard.domain import work_models
from pinboard.domain.identifiers import AttemptId, HistoryId, ItemId


class OpenMode(Enum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


@dataclass(frozen=True, slots=True)
class InitReceipt:
    work_root: Path
    database_path: Path
    project_revision: int
    resumed: bool


class ItemIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    item_id: ItemId


class AttemptIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId


class HistoryIdRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    history_id: HistoryId


class DependencyViewRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    dependency_id: ItemId
    queue_position: int | None


class MutationAllocationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    next_history_id: int


class PersistedAllocationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int
    history_id: int


class ArtifactIdAllocationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    next_artifact_ref_id: int


class LiveItemCountRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    live_item_count: int


class GenerationRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    generation_high_water: int


class ProjectRevisionRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    revision: int


class StateCountRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    state: str
    item_count: int


class CandidateSnapshotAttemptRow(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    attempt_id: AttemptId
    item_id: ItemId
    state: work_models.AttemptState
    branch: str
    base_revision: str
    candidate_revision: str | None
    candidate_recorded_at: datetime | None
    subject_revision: int
