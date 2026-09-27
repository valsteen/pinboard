from dataclasses import dataclass

from pinboard.domain.identifiers import AttemptId, HistoryId, WorkItemId


@dataclass(frozen=True, slots=True)
class ViewWarning:
    message: str
    repair: str


@dataclass(frozen=True, slots=True)
class ViewRefreshResult:
    database_revision: int
    warning: ViewWarning | None


@dataclass(frozen=True, slots=True)
class AffectedViews:
    items: tuple[WorkItemId, ...]
    attempts: tuple[AttemptId, ...]
    history_receipts: tuple[HistoryId, ...]
